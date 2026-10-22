"""The LLM client: strict JSON, and the answers that are not JSON."""

from __future__ import annotations

import json

import pytest

from app.services.llm import LLMClient, LLMError, LLMResult


def test_a_json_answer_is_parsed():
    result = LLMResult(content='{"attribute": "rotational_speed", "value": "1500"}')
    assert result.parsed == {"attribute": "rotational_speed", "value": "1500"}


def test_a_fenced_answer_is_still_json():
    """A fence is a formatting habit, not a different answer."""
    result = LLMResult(content='```json\n{"value": "16"}\n```')
    assert result.parsed == {"value": "16"}


def test_an_empty_answer_is_a_failure():
    with pytest.raises(LLMError):
        LLMResult(content="").parsed


def test_a_prose_answer_is_a_failure():
    """There is no safe way to read a requirement out of a sentence of
    explanation."""
    with pytest.raises(LLMError):
        LLMResult(content="The valve shall be SS316 with a 16 bar rating.").parsed


def test_a_client_without_a_model_is_refused(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(LLMError):
        LLMClient()


def test_the_client_asks_for_json_and_reads_the_answer(monkeypatch):
    """The client sends the model a question and returns what it said."""
    sent: dict = {}

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        sent["body"] = json.loads(request.data)
        sent["url"] = request.full_url
        return FakeResponse(
            {
                "choices": [
                    {"message": {"content": '{"requirements": []}', "reasoning": "thinking"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    answer = client.complete_json("system", "user")

    assert answer == {"requirements": []}
    assert sent["body"]["model"] == "openai/gpt-oss-20b"
    assert sent["body"]["messages"][0]["role"] == "system"
    # A reasoning model needs room: a small budget is spent on reasoning and
    # the answer comes back empty.
    assert sent["body"]["max_tokens"] >= 512


def test_a_model_error_is_reported_not_raised_past(monkeypatch):
    """A failed call is a failed extraction, and the caller decides what to
    do about it."""

    def fake_urlopen(request, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(LLMError) as caught:
        LLMClient().chat("system", "user")
    assert "connection reset" in str(caught.value)
