"""The LLM client (FR-22, FR-T7).

The LLM reads and extracts; it never decides. Every call asks for strict
JSON and the answer is validated by Pydantic before anything downstream sees
it, so a malformed or truncated answer is a failed extraction rather than a
requirement that was never stated.

The client speaks the OpenAI-compatible API, so the provider is a base URL:
Groq serves gpt-oss. Embeddings are local by default (sentence-transformers,
public weights, no account); the alternative `http` backend speaks the same
OpenAI shape, which a local Ollama serves.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# A reasoning model may wrap its answer, or a chat model may fence it.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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


class _RateLimited(Exception):
    """The provider's limit was hit. Worth retrying."""


# The provider says how long to wait: "Please try again in 24.9s".
_RETRY_AFTER = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)


def _retry_after(error: Exception, fallback: float) -> float:
    """How long to wait before trying again.

    The provider knows when its limit resets, and a per-minute limit resets
    sooner than a fixed backoff assumes. A limit of a minute would be
    retried into with a five second wait, so the provider's own advice is
    used when it gives any.
    """
    match = _RETRY_AFTER.search(str(error))
    if match:
        try:
            return max(fallback, float(match.group(1)))
        except ValueError:
            pass
    return fallback


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

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        settings = get_settings()
        self.model = model or settings.llm_model
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = settings.llm_api_key
        if not self.model:
            raise LLMError("no LLM model is configured")
        if not self.api_key:
            raise LLMError("no LLM api key is configured")
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

    def chat(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> LLMResult:
        """Ask the model one question and get one answer.

        A rate-limited call is retried only while the retry budget allows it.
        When the budget is spent the limit is raised as `LLMRateLimited`, not
        folded into a generic failure: the caller must stop and record it,
        not carry on reading lines it cannot ask about.
        """
        settings = get_settings()
        last_error: Optional[str] = None
        budget = max_tokens or settings.llm_max_tokens
        for attempt in range(settings.llm_max_retries + 1):
            try:
                result = self._chat_once(system, user, max_tokens=budget)
            except _RateLimited as exc:
                last_error = str(exc)
                wait = _retry_after(exc, settings.llm_retry_seconds * (attempt + 1))
                if attempt >= settings.llm_max_retries:
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
                if attempt >= settings.llm_max_retries:
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
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code == 429:
                raise _RateLimited(f"the model is rate limited: {detail}") from exc
            raise LLMError(f"the model returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised past
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

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

    def complete_json(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> Any:
        """Ask the model for JSON and return it parsed."""
        return self.chat(system, user, max_tokens=max_tokens).parsed


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

        logger.info("loading the embedding model %s", name)
        model = SentenceTransformer(name)
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
        vectors = model.encode(
            texts,
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]
    if backend != "http":
        raise LLMError(f"unknown embedding backend {settings.embedding_backend!r}")

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
        detail = exc.read()[:200].decode("utf-8", "replace")
        raise LLMError(f"the embedding model returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc

    items = data.get("data") or []
    # The order of the response is by index, not by position.
    items.sort(key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in items]
