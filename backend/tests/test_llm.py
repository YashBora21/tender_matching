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


def test_a_rate_limited_call_waits_as_long_as_the_provider_says(monkeypatch):
    """A per-minute limit resets in under a minute, and a fixed backoff
    would retry into it."""
    import urllib.error

    from app.services.llm import _retry_after

    error = urllib.error.HTTPError(
        "u", 429, "Rate limit reached ... Please try again in 24.9s", {}, None
    )
    assert _retry_after(error, 5.0) == 24.9

    # With no advice from the provider, the backoff stands.
    plain = RuntimeError("rate limited")
    assert _retry_after(plain, 5.0) == 5.0


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


def test_a_rate_limited_call_is_retried(monkeypatch):
    """The provider's limit is per minute and per day, and a burst of calls
    is a normal way to hit it."""
    import urllib.error

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {}, None
            )
        return _FakeResponse(
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_RETRY_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    answer = LLMClient().complete_json("system", "user")

    assert answer == {"ok": True}
    assert len(calls) == 2


def test_a_model_that_only_thinks_is_retried_with_a_bigger_budget(monkeypatch):
    """gpt-oss spends its tokens on reasoning first. A model that thought and
    never answered ran out of budget, and a bigger budget is the fix."""
    calls = []

    def fake_urlopen(request, timeout=None):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return _FakeResponse(
                {
                    "choices": [{"message": {"content": "", "reasoning": "thinking"}}],
                    "usage": {},
                }
            )
        return _FakeResponse(
            {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_MAX_TOKENS", "1000")
    from app.core.config import get_settings

    get_settings.cache_clear()

    answer = LLMClient().complete_json("system", "user")

    assert answer == {"ok": True}
    assert calls[1]["max_tokens"] == 2000


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_client_counts_the_calls_and_the_tokens(monkeypatch):
    """What a run cost is measured, not left in the log (FR-34)."""

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(
            {
                "choices": [{"message": {"content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            }
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    client.complete_json("system", "user")
    client.complete_json("system", "user")

    assert client.calls == 2
    assert client.prompt_tokens == 240
    assert client.completion_tokens == 60
    assert client.tokens == 300
    assert client.retries == 0


def test_a_rate_limit_that_outlasts_the_budget_is_raised_not_swallowed(monkeypatch):
    """The caller has to stop, and the limit is not an ordinary failure: the
    passage was never read."""
    import io
    import urllib.error

    from app.services.llm import LLMRateLimited

    attempts = []

    def fake_urlopen(request, timeout=None):
        attempts.append(request.full_url)
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error": "Rate limit reached. Please try again in 3.5s."}'),
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setenv("LLM_RETRY_SECONDS", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    with pytest.raises(LLMRateLimited) as caught:
        client.chat("system", "user")

    # The provider's advice is carried out with the error, so a stopped run
    # can say when it is worth trying again.
    assert caught.value.retry_after == 3.5
    assert client.calls == 2
    assert client.retries == 1
    assert len(attempts) == 2


def test_no_retries_left_raises_the_limit_on_the_first_attempt(monkeypatch):
    """`LLM_MAX_RETRIES=0` is the Phase 12 rule: stop at the first limit."""
    import urllib.error

    from app.services.llm import LLMRateLimited

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, None)

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    with pytest.raises(LLMRateLimited):
        client.chat("system", "user")

    assert client.calls == 1
    assert client.retries == 0
