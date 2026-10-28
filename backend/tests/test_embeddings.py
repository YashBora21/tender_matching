"""Embeddings: the local model is the default, the HTTP shape still works."""

from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.services import llm


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_local_backend_encodes_with_normalised_vectors(monkeypatch):
    """The default backend loads the weights in this process: no account, no
    credits. bge models are trained for cosine, so the vector is normalised."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence-transformers")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    get_settings.cache_clear()
    seen: dict = {}

    class FakeModel:
        def encode(self, texts, **kwargs):
            seen["texts"] = texts
            seen.update(kwargs)
            return [[0.1, 0.2], [0.3, 0.4]]

    monkeypatch.setattr(llm, "_sentence_transformer", lambda name: FakeModel())

    vectors = llm.embed(["a", "b"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["texts"] == ["a", "b"]
    assert seen["normalize_embeddings"] is True


def test_no_texts_is_no_call(monkeypatch):
    monkeypatch.setattr(
        llm, "_sentence_transformer", lambda name: pytest.fail("must not load a model")
    )

    assert llm.embed([]) == []


def test_the_http_backend_posts_the_openai_embeddings_shape(monkeypatch):
    """The alternative backend is any OpenAI-compatible endpoint, such as a
    local Ollama. It is config, not a second implementation."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "http")
    monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed-text")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://localhost:11434")
    get_settings.cache_clear()
    sent: dict = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        return _Response({"data": [{"index": 0, "embedding": [0.5, 0.6]}]})

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)

    assert llm.embed(["a"]) == [[0.5, 0.6]]
    assert sent["url"] == "http://localhost:11434/v1/embeddings"
    assert sent["body"] == {"model": "nomic-embed-text", "input": ["a"]}


def test_an_unknown_backend_is_refused(monkeypatch):
    """A typo in the environment must not silently produce no vectors."""
    monkeypatch.setenv("EMBEDDING_BACKEND", "sentence-transformerss")
    get_settings.cache_clear()

    with pytest.raises(llm.LLMError) as caught:
        llm.embed(["a"])

    assert "unknown embedding backend" in str(caught.value)
