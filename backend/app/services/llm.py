"""The LLM client (FR-22, FR-T7).

The LLM reads and extracts; it never decides. Every call asks for strict
JSON and the answer is validated by Pydantic before anything downstream sees
it, so a malformed or truncated answer is a failed extraction rather than a
requirement that was never stated.

The client speaks the OpenAI-compatible API, so the provider is a base URL:
Groq serves gpt-oss, a local Ollama serves an embedding model, and the
HuggingFace router serves either.
"""

from __future__ import annotations

import json
import re
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

    def chat(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> LLMResult:
        """Ask the model one question and get one answer."""
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
        try:
            with urllib.request.urlopen(request, timeout=settings.llm_timeout_seconds) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise LLMError(f"the model returned HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised past
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError("the model's answer had no message") from exc

        usage = data.get("usage") or {}
        return LLMResult(
            content=message.get("content") or "",
            reasoning=message.get("reasoning"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def complete_json(self, system: str, user: str, *, max_tokens: Optional[int] = None) -> Any:
        """Ask the model for JSON and return it parsed."""
        return self.chat(system, user, max_tokens=max_tokens).parsed


# The browser signature some providers require before they answer.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured embedding model.

    The model is reached over an OpenAI-compatible endpoint, so the same call
    serves a local Ollama and a hosted one.
    """
    if not texts:
        return []
    settings = get_settings()
    if not settings.embedding_model:
        raise LLMError("no embedding model is configured")
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
