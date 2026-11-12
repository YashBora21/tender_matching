"""The LLM client (FR-22, FR-T7).

The LLM reads and extracts; it never decides. Every call asks for strict
JSON and the answer is validated by Pydantic before anything downstream sees
it, so a malformed or truncated answer is a failed extraction rather than a
requirement that was never stated.

The client speaks a compatible chat API configured entirely at runtime.
Embeddings use either local weights or the configured HTTP backend.
"""

from __future__ import annotations

import json
from hashlib import sha256
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
STRUCTURED_VALIDATION_VERSION = 'one-evidence-repair-feedback-1'

# A reasoning model may wrap its answer, or a chat model may fence it.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# How much of an error body is read looking for a wait: enough for an
# explanation and the field that follows it, bounded so a page of HTML from a
# proxy cannot be pulled into memory.
_LIMIT_BODY_BYTES = 65536


class LLMError(Exception):
    """The LLM could not be used. The reason is reported, not raised past."""


class LLMRateLimited(LLMError):
    """The provider's limit was hit and the caller has to stop.

    Separate from `LLMError` because a rate limit is not a bad answer: the
    passage was never read. An extraction layer that caught this as an
    ordinary failure would turn "we could not ask" into "the document says
    nothing", which is exactly the confusion the project forbids.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        # What the provider said to wait, when it said anything. Recorded so a
        # stopped run can say when it is worth trying again.
        self.retry_after = retry_after


class LLMBudgetExceeded(LLMError):
    """The spend this process was allowed is used up, so it stops on its own.

    The same shape as a provider's limit and a different cause: nobody refused
    a call, the operator's own budget ran out first. It is its own error
    because a report has to say which of the two stopped, and a budget is
    raised before a request is made rather than in answer to one.
    """


class _RateLimited(Exception):
    """The provider's limit was hit. Worth retrying.

    `wait` is the pause the provider asked for, read when the answer arrives
    and its body is still whole.
    """

    def __init__(self, message: str, *, wait: Optional[float] = None):
        super().__init__(message)
        self.wait = wait


class _TransientFailure(LLMError):
    """A temporary transport/provider failure may use the configured retries."""



NO_MODEL = "no LLM model is configured (set LLM_MODEL)"
NO_API_KEY = "no text API key is configured (set LLM_TEXT_API_KEY or LLM_API_KEY)"
NO_BASE_URL = "no LLM endpoint is configured (set LLM_BASE_URL)"


def _missing(model: Optional[str], api_key: Optional[str], base_url: str) -> Optional[str]:
    """The reason this configuration cannot be called, or None.

    One answer for both the preflight and the client, so they cannot drift
    into refusing for different words.
    """
    if not model:
        return NO_MODEL
    if not api_key:
        return NO_API_KEY
    if not base_url.strip():
        return NO_BASE_URL
    return None


def config_error() -> Optional[str]:
    """Why the LLM cannot be used right now, or None when it can.

    A caller about to start minutes of work asks this first, so an
    unconfigured model is refused at the request instead of discovered as a
    run that died. It names no provider: a hosted, a self-hosted and a local
    model are refused for the same reason - there is nothing to call.
    """
    settings = get_settings()
    return _missing(settings.llm_model, settings.text_api_key, settings.llm_base_url)


# A wait is told in one of three places, and none of them is a vendor's
# invention: the standard `Retry-After` header, a field of the JSON error body,
# or the prose the provider said it in. All three are read, so a provider this
# code has never met needs no code change (D-109).
_RETRY_HEADER = "retry-after"
_RETRY_FIELDS = frozenset(
    {"retryafter", "retry_after", "retrydelay", "retry_delay"}
)

# A bare number is seconds; a suffixed one says what it counts.
_DURATION = re.compile(
    r"^\s*([\d.]+)\s*(ms|msec|millisec(?:ond)?s?|s|sec|secs|seconds?"
    r"|m|mins?|minutes?)?\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "ms": 0.001,
    "msec": 0.001,
    "millisec": 0.001,
    "milliseccond": 0.001,
    "millisecconds": 0.001,
    "millisecond": 0.001,
    "milliseconds": 0.001,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
}

# "try again in 24.9s", "please retry after 30 seconds", "retry in 1 minute".
_PROSE_WAIT = re.compile(
    r"(?:try\s+again|retry|resend|wait)[^0-9]{0,40}?([\d.]+)\s*"
    r"(ms|milliseconds|s|sec|secs|seconds|minutes?)\b",
    re.IGNORECASE,
)


def _seconds(value: object) -> Optional[float]:
    """A wait written as a number of seconds, or as a duration string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = _DURATION.match(value)
    if not match:
        return None
    unit = (match.group(2) or "s").lower()
    return float(match.group(1)) * _UNIT_SECONDS.get(unit, 1.0)


def _walk_waits(node: object) -> Iterable[object]:
    """Every value a JSON error body names as a wait, however deep it sits."""
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower().replace("-", "_") in _RETRY_FIELDS:
                yield value
            yield from _walk_waits(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_waits(item)


def _header_value(headers: object) -> Optional[str]:
    """The `Retry-After` header, whatever case the transport kept it in.

    An HTTP message folds header names; a dict handed over by a test, a proxy
    or a library does not, so the name is matched case-insensitively rather
    than assumed.
    """
    if headers is None:
        return None
    try:
        value = headers.get(_RETRY_HEADER)
    except (AttributeError, TypeError):
        return None
    if value is not None:
        return value
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return None
    for key, item in items:
        if str(key).replace("_", "-").lower() == _RETRY_HEADER:
            return item
    return None


def _header_seconds(headers: object) -> Optional[float]:
    """The `Retry-After` header: seconds, or the date to wait until."""
    raw = _header_value(headers)
    if raw is None:
        return None
    seconds = _seconds(raw)
    if seconds is not None:
        return seconds
    # The header's other form is an HTTP date, and only the local clock knows
    # how far off it is.
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _body_seconds(text: str) -> Optional[float]:
    """A wait named as a field of the error body, when the body is JSON."""
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    for value in _walk_waits(decoded):
        seconds = _seconds(value)
        if seconds is not None:
            return seconds
    return None


def _prose_seconds(text: str) -> Optional[float]:
    """A wait named in the sentence the provider said it in."""
    match = _PROSE_WAIT.search(text)
    if not match:
        return None
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def _limit_seconds(headers: object, text: str) -> Optional[float]:
    """How long the provider asked for, from the first place it said it.

    The header is the standard channel, a named field is more exact than a
    sentence, and a sentence is what a provider falls back to.
    """
    for read in (_header_seconds(headers), _body_seconds(text), _prose_seconds(text)):
        if read is not None:
            return read
    return None


def _retry_after(error: Exception, fallback: float) -> float:
    """How long to wait before trying again.

    The provider knows when its limit resets, and a per-minute limit resets
    sooner than a fixed backoff assumes. A limit of a minute would be
    retried into with a five second wait, so the provider's own advice is
    used when it gives any.
    """
    stated = getattr(error, "wait", None)
    if stated is None:
        stated = _limit_seconds(getattr(error, "headers", None), str(error))
    if stated is None:
        return fallback
    return max(fallback, float(stated))


@dataclass
class LLMResult:
    """One answer from the LLM."""

    content: str
    # The model's own reasoning, when it is a reasoning model. Kept for the
    # log, never for the requirement.
    reasoning: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def parsed(self) -> Any:
        """The answer as JSON.

        Strict JSON is asked for, and the answer is validated. A model that
        wrapped its answer in a fence is still understood, because a fence is
        a formatting habit, not a different answer. A model that answered
        with prose is not: there is no safe way to read a requirement out of
        a sentence of explanation.
        """
        text = self.content.strip()
        if not text:
            raise LLMError("the model returned nothing")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        fenced = _FENCE.search(text)
        if fenced:
            try:
                return json.loads(fenced.group(1).strip())
            except json.JSONDecodeError as exc:
                raise LLMError(f"the model's answer was not JSON: {exc}") from exc
        raise LLMError("the model's answer was not JSON")


class LLMClient:
    """A client for an OpenAI-compatible chat API."""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None,
                 *, api_key: Optional[str] = None):
        settings = get_settings()
        self.model = model or settings.llm_model
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        if api_key is None and base_url is not None and self.base_url != settings.llm_base_url.rstrip('/'):
            raise LLMError('an explicit API key is required for an alternate endpoint')
        self.api_key = settings.text_api_key if api_key is None else api_key
        # The same question `config_error` answers, so a caller that checked
        # first and a client built without checking refuse for one reason.
        # The resolved values are checked, not the settings: a caller that
        # named a model on the call is configured even when the env is empty.
        reason = _missing(self.model, self.api_key, self.base_url)
        if reason:
            raise LLMError(reason)
        # What this client has spent. Kept here so a caller can report the
        # calls, tokens and retries a run cost (FR-34), instead of the numbers
        # disappearing into the log.
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.retries = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _check_budget(self) -> None:
        """Stop before a call this process was not allowed to make.

        The counters are the client's own, so the budget bounds one run: a
        caller that wants a cheaper run caps the candidates it reads, and a
        caller that wants a bounded one sets the budget. Zero is no ceiling,
        and a stop names what was already spent (D-110).
        """
        settings = get_settings()
        spent = f"{self.calls} calls, {self.tokens} tokens"
        if settings.llm_call_budget and self.calls >= settings.llm_call_budget:
            raise LLMBudgetExceeded(
                f"the call budget of {settings.llm_call_budget} is spent ({spent})"
            )
        if settings.llm_token_budget and self.tokens >= settings.llm_token_budget:
            raise LLMBudgetExceeded(
                f"the token budget of {settings.llm_token_budget} is spent ({spent})"
            )

    def chat(self, system: str, user: str, *, max_tokens: Optional[int] = None, _max_retries: Optional[int] = None) -> LLMResult:
        """Ask the model one question and get one answer.

        A rate-limited call is retried only while the retry budget allows it.
        When the budget is spent the limit is raised as `LLMRateLimited`, not
        folded into a generic failure: the caller must stop and record it,
        not carry on reading lines it cannot ask about.
        """
        settings = get_settings()
        retry_limit = settings.llm_max_retries if _max_retries is None else min(settings.llm_max_retries, _max_retries)
        last_error: Optional[str] = None
        budget = max_tokens or settings.llm_max_tokens
        for attempt in range(retry_limit + 1):
            self._check_budget()
            try:
                result = self._chat_once(system, user, max_tokens=budget)
            except _TransientFailure as exc:
                if attempt >= retry_limit:
                    raise LLMError(str(exc)) from None
                self.retries += 1
                logger.warning('temporary LLM failure, retrying (%d/%d)', attempt + 1, settings.llm_max_retries)
                time.sleep(settings.llm_retry_seconds * (attempt + 1))
                continue
            except _RateLimited as exc:
                last_error = str(exc)
                wait = _retry_after(exc, settings.llm_retry_seconds * (attempt + 1))
                if attempt >= retry_limit:
                    raise LLMRateLimited(last_error, retry_after=wait) from exc
                self.retries += 1
                logger.warning(
                    "the model is rate limited, retrying in %.0fs (%d/%d)",
                    wait,
                    attempt + 1,
                    settings.llm_max_retries,
                )
                time.sleep(wait)
                continue
            if not result.content.strip() and result.reasoning:
                # The model thought and never answered: it ran out of budget
                # before it got to the answer. A bigger budget is the fix.
                last_error = "the model spent its whole budget reasoning"
                if attempt >= retry_limit:
                    break
                self.retries += 1
                budget = int(budget * 2)
                logger.warning(
                    "the model answered nothing, retrying with %d tokens", budget
                )
                continue
            return result
        raise LLMError(last_error or "the model did not answer")

    def _chat_once(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> LLMResult:
        settings = get_settings()
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": settings.llm_temperature,
                "max_tokens": max_tokens or settings.llm_max_tokens,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Some providers refuse a request that does not look like a
                # browser, and the refusal is a 403 with no explanation.
                "User-Agent": _USER_AGENT,
            },
        )
        # Every HTTP attempt is a call, including one that comes back 429: the
        # account is charged for it, so the report has to count it.
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # The body is read past its first sentence: a wait can sit in a
            # field after a long explanation, and a truncated read loses the
            # one number that says how long to pause.
            body = exc.read(_LIMIT_BODY_BYTES).decode("utf-8", "replace")
            if exc.code == 429:
                raise _RateLimited(
                    "the model is rate limited (HTTP 429)",
                    wait=_limit_seconds(exc.headers, body),
                ) from None
            if exc.code in {408, 500, 502, 503, 504}:
                raise _TransientFailure(f'temporary LLM failure (HTTP {exc.code})') from None
            raise LLMError(
                f"the configured LLM returned HTTP {exc.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise _TransientFailure(f'LLM transport failed ({type(exc).__name__})') from None
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised past
            raise LLMError(f"LLM transport failed ({type(exc).__name__})") from None

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError("the model's answer had no message") from exc

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        return LLMResult(
            content=message.get("content") or "",
            reasoning=message.get("reasoning"),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def complete_json(self, system: str, user: str, *, max_tokens: Optional[int] = None, validate=None) -> Any:
        """Parse JSON; one evidence repair shares the transport/JSON retry budget."""
        retry_start = self.retries
        repaired = False
        request_user = user
        self.validation_attempts = getattr(self, 'validation_attempts', [])
        self.structured_requests = getattr(self, 'structured_requests', 0) + 1
        request_id = self.structured_requests
        source_hash = sha256(user.encode()).hexdigest()

        def record(attempt, outcome, error=None):
            if validate is not None:
                self.validation_attempts.append({'request_id': request_id, 'source_hash': source_hash,
                    'attempt': attempt + 1, 'call': self.calls, 'outcome': outcome, 'error': error})

        for attempt in range(get_settings().llm_max_retries + 1):
            remaining = get_settings().llm_max_retries - (self.retries - retry_start)
            result = self.chat(system, request_user, max_tokens=max_tokens, _max_retries=remaining)
            try:
                answer = result.parsed
            except LLMError:
                record(attempt, 'invalid_json', 'invalid_json')
                if repaired or self.retries - retry_start >= get_settings().llm_max_retries:
                    raise
                self.retries += 1
                logger.warning('invalid LLM JSON, retrying within configured budgets')
                continue
            if validate is not None:
                try:
                    validate(answer)
                except LLMError as exc:
                    safe_errors = {'invalid_source_quote', 'classification_requires_complete_source',
                        'invalid_classification_schema', 'source_quote_or_value_validation_failed',
                        'extraction_schema_validation_failed', 'invalid_batch_classification',
                        'batch_clause_id_mismatch', 'conflicting_clause_classification'}
                    error = str(exc) if str(exc) in safe_errors else 'source_validation_failed'
                    record(attempt, 'validation_failed', error)
                    if repaired or self.retries - retry_start >= get_settings().llm_max_retries:
                        raise
                    repaired = True
                    self.retries += 1
                    request_user = user + ('\n\nValidation feedback: ' + error +
                        '. Return a corrected response for the unchanged source above. '
                        'Keep source text, values and identifiers verbatim; do not relax validation.')
                    continue
            record(attempt, 'passed')
            return answer


# The browser signature some providers require before they answer.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


# Loaded model, keyed by name. `embed` is called once per batch of 32 chunks,
# and loading the weights on every batch would read the model from disk (and
# download it once) for no reason.
_LOCAL_MODELS: dict[str, Any] = {}


def _sentence_transformer(name: str):
    """The local model, loaded once and reused.

    Imported lazily: the HTTP backend does not need torch, and a machine that
    only talks to a hosted model should not carry it.
    """
    model = _LOCAL_MODELS.get(name)
    if model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("loading the configured embedding model")
        try:
            model = SentenceTransformer(name)
        except Exception as exc:
            raise LLMError(f"embedding model loading failed ({type(exc).__name__})") from None
        _LOCAL_MODELS[name] = model
    return model


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured embedding model.

    Two backends, one vector shape: `sentence-transformers` loads public
    weights in this process (no account, no credits), and `http` speaks the
    OpenAI embeddings shape, which is what a local Ollama serves. The choice
    is config, not code; the dimension is a property of the model and the
    migrated column (D-6).
    """
    if not texts:
        return []
    settings = get_settings()
    if not settings.embedding_model:
        raise LLMError("no embedding model is configured")

    backend = (settings.embedding_backend or "").strip().lower()
    if backend == "sentence-transformers":
        model = _sentence_transformer(settings.embedding_model)
        # bge models are trained for cosine similarity; normalising here makes
        # the vector's length irrelevant, which is what the cosine operator
        # assumes. The batch size keeps a large corpus within memory.
        try:
            vectors = model.encode(
                texts,
                batch_size=settings.embedding_batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise LLMError(f"embedding encoding failed ({type(exc).__name__})") from None
        return [[float(value) for value in vector] for vector in vectors]
    if backend != "http":
        raise LLMError(f"unknown embedding backend {settings.embedding_backend!r}")
    if not settings.embedding_base_url.strip():
        raise LLMError("no embedding endpoint is configured (set EMBEDDING_BASE_URL)")

    body = json.dumps({"model": settings.embedding_model, "input": texts}).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.embedding_base_url.rstrip('/')}/v1/embeddings",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"the embedding model returned HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"embedding transport failed ({type(exc).__name__})") from None

    items = data.get("data") or []
    # The order of the response is by index, not by position.
    items.sort(key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in items]
