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


def test_missing_endpoint_is_refused_before_network_work(monkeypatch):
    """Synthetic configuration: preflight and client agree without a request."""
    from app.core.config import get_settings
    from app.services.llm import NO_BASE_URL, config_error

    monkeypatch.setenv('LLM_MODEL', 'synthetic-model')
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-key')
    monkeypatch.setenv('LLM_BASE_URL', '   ')
    get_settings.cache_clear()
    assert config_error() == NO_BASE_URL
    with pytest.raises(LLMError, match='LLM_BASE_URL'):
        LLMClient()


@pytest.mark.parametrize('status', [401, 429])
def test_provider_error_does_not_publish_synthetic_private_values(monkeypatch, status):
    import io
    import traceback
    import urllib.error
    from app.core.config import get_settings

    private = 'synthetic-private-provider-detail'
    monkeypatch.setenv('LLM_MODEL', private)
    monkeypatch.setenv('LLM_API_KEY', private)
    monkeypatch.setenv('LLM_MAX_RETRIES', '0')
    get_settings.cache_clear()

    def fail(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, status, private, {}, io.BytesIO(private.encode()))

    monkeypatch.setattr('app.services.llm.urllib.request.urlopen', fail)
    with pytest.raises(LLMError) as caught:
        LLMClient().chat('synthetic system', 'synthetic user')
    assert private not in ''.join(traceback.format_exception(caught.value))
    assert str(status) in str(caught.value)


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


def test_a_retry_after_header_is_read_both_ways_it_can_be_written():
    """The standard header is the channel every provider has, whatever it is
    called: seconds, or the moment to come back."""
    import urllib.error

    from app.services.llm import _retry_after

    seconds = urllib.error.HTTPError(
        "u", 429, "slow down", {"Retry-After": "12"}, None
    )
    assert _retry_after(seconds, 5.0) == 12.0

    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    until = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
    dated = urllib.error.HTTPError("u", 429, "slow down", {"Retry-After": until}, None)
    assert 20.0 <= _retry_after(dated, 5.0) <= 31.0


def test_a_wait_named_in_a_body_field_is_found_however_deep_it_sits():
    """Some providers answer with a long explanation and put the wait in a
    field after it, so the field is read wherever it lives."""
    from app.services.llm import _body_seconds

    body = (
        '{"error": {"code": 429, "message": "Resource has been exhausted.",'
        ' "status": "RESOURCE_EXHAUSTED",'
        ' "details": [{"@type": "RetryInfo", "retryDelay": "45.123s"}]}}'
    )
    assert _body_seconds(body) == pytest.approx(45.123)

    # A number is already seconds, and a snake_case name is the same field.
    assert _body_seconds('{"error": {"retry_after": 30}}') == 30.0

    # A body that is not JSON says nothing through this channel.
    assert _body_seconds("<html>429 Too Many Requests</html>") is None


def test_a_wait_named_in_prose_is_read_in_the_usual_ways():
    from app.services.llm import _prose_seconds

    assert _prose_seconds("Please try again in 12.5s") == 12.5
    assert _prose_seconds("please retry after 30 seconds") == 30.0
    assert _prose_seconds("too many requests, retry in 1 minute") == 60.0
    assert _prose_seconds("the model is rate limited") is None


def test_the_header_is_believed_before_the_body_and_the_body_before_prose():
    """A provider can say the same wait three ways. The most formal channel
    wins, because it is the one the provider is least likely to get wrong."""
    from app.services.llm import _limit_seconds

    body = '{"error": {"message": "try again in 5s", "retryDelay": "20s"}}'

    assert _limit_seconds({"Retry-After": "60"}, body) == 60.0
    assert _limit_seconds({}, body) == 20.0
    assert _limit_seconds(None, "try again in 5s") == 5.0


def test_a_limit_answered_with_a_header_stops_the_run_with_that_wait(monkeypatch):
    """The wait is carried out with the error, so a caller that stops can say
    when it is worth trying again - whatever channel the provider used."""
    import io
    import urllib.error

    from app.services.llm import LLMRateLimited

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {"Retry-After": "9"}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(LLMRateLimited) as caught:
        LLMClient().chat("system", "user")

    assert caught.value.retry_after == 9.0


def test_a_refused_call_reports_http_status_without_private_configuration(monkeypatch):
    """Synthetic provider identifiers and bodies must not reach reports/logs."""
    import io
    import urllib.error

    from app.services.llm import LLMError

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error": "bad key"}')
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.test/v1")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(LLMError) as raised:
        LLMClient().chat("system", "user")

    assert "HTTP 401" in str(raised.value)
    assert "a-model" not in str(raised.value)
    assert "api.example.test" not in str(raised.value)
    assert "bad key" not in str(raised.value)
    assert raised.value.__suppress_context__


def test_a_body_is_read_past_its_first_sentence_for_the_wait(monkeypatch):
    """A truncating read loses the number that decides how long to pause.

    The message is kept short for the log; the wait is read from the whole
    body, which can put its field after a long explanation.
    """
    import io
    import urllib.error

    from app.services.llm import LLMRateLimited

    payload = (
        '{"error": {"message": "' + "x" * 400 + '",'
        ' "details": [{"retryDelay": "77s"}]}}'
    ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, io.BytesIO(payload)
        )

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    with pytest.raises(LLMRateLimited) as caught:
        LLMClient().chat("system", "user")

    assert caught.value.retry_after == 77.0


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
    assert "OSError" in str(caught.value)
    assert "connection reset" not in str(caught.value)


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


def test_a_call_budget_stops_before_the_request_is_made(monkeypatch):
    """The budget is checked before the call, because the spend it prevents is
    a call that should never have been paid for (D-110)."""
    from app.services.llm import LLMBudgetExceeded

    sent: list[str] = []

    def fake_urlopen(request, timeout=None):
        sent.append(request.full_url)
        raise AssertionError("the budget should have stopped the call")

    monkeypatch.setattr("app.services.llm.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_CALL_BUDGET", "3")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    client.calls = 3
    with pytest.raises(LLMBudgetExceeded) as raised:
        client.chat("system", "user")

    # The stop says what was already spent, so the caller can size the next run.
    assert "3" in str(raised.value)
    assert sent == []


def test_a_token_budget_stops_the_client_that_spent_it(monkeypatch):
    from app.services.llm import LLMBudgetExceeded

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "100")
    monkeypatch.setenv("LLM_CALL_BUDGET", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    client.prompt_tokens = 90
    client.completion_tokens = 20
    with pytest.raises(LLMBudgetExceeded):
        client.chat("system", "user")


def test_no_budget_means_no_ceiling(monkeypatch):
    """Zero is the default and asks no question: an uncapped run is the
    behaviour the tests and the README already describe."""
    from app.services.llm import LLMBudgetExceeded

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    monkeypatch.setenv("LLM_CALL_BUDGET", "0")
    monkeypatch.setenv("LLM_TOKEN_BUDGET", "0")
    from app.core.config import get_settings

    get_settings.cache_clear()

    client = LLMClient()
    client.calls = 10_000
    client.prompt_tokens = 1_000_000
    try:
        client._check_budget()
    except LLMBudgetExceeded:  # pragma: no cover - the failure is the bug
        raise AssertionError("a zero budget must not stop a run")
