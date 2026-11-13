"""`/api/preflight`: the current effective limits, and no model identity.

The measurement is faked (`measure` needs a database); what is tested is what
the preflight payload adds: the limits a read would run under now, and the
rule that the model id and endpoint do not cross an API boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.services.cli import e2e


@dataclass
class Facts:
    model_reason: str | None = None
    model_label: str = "synthetic-model at https://synthetic-llm.example.test/v1"
    ocr_reason: str | None = None
    documents: int = 3
    chunks: int = 30
    manufacturers: int = 2
    files_on_disk: int = 3
    tender_status: str | None = None
    requirements: int = 0
    needs_model: bool = False


def _patch(monkeypatch, facts):
    monkeypatch.setattr(e2e, "measure", lambda session, tender_id=None: facts)
    monkeypatch.setattr(e2e, "_stored_id", lambda session, ref: None)


def test_the_preflight_names_the_limits_a_read_would_run_under(monkeypatch):
    _patch(monkeypatch, Facts())
    settings = get_settings()

    body = e2e.preflight(None)

    limits = body["limits"]
    assert limits["call_budget"] == settings.llm_call_budget
    assert limits["token_budget"] == settings.llm_token_budget
    assert limits["max_output_tokens"] == settings.llm_max_tokens
    assert limits["max_retries"] == settings.llm_max_retries
    assert limits["spend_caps_enabled"] == getattr(settings, 'llm_spend_caps_enabled', False)
    assert limits["schema_version"] == settings.schema_version


def test_the_preflight_never_names_the_model_or_the_endpoint(monkeypatch):
    _patch(monkeypatch, Facts())

    body = e2e.preflight(None)

    assert body["model"]["configured"] is True
    assert "synthetic-model" not in str(body["model"]["target"])
    assert "synthetic-llm.example.test" not in str(body["model"]["target"])


def test_a_missing_model_is_said_without_a_target(monkeypatch):
    _patch(monkeypatch, Facts(model_reason="no API key is configured"))

    body = e2e.preflight(None)

    assert body["model"]["configured"] is False
    assert body["model"]["target"] is None
    assert body["model"]["reason"] == "no API key is configured"


def test_the_preflight_names_the_key_source_and_last_four(monkeypatch):
    """A stale key is told from a wrong one by the variable it came from and
    its last four characters; the value never crosses the boundary (user
    phase 1). The summary is read from a stub because the operator's `.env`
    may carry a real key the test must not depend on."""
    from app.services import llm

    class _Stub:
        llm_text_api_key = None
        llm_api_key = "sk-synthetic-1234567890abcdef"

    _patch(monkeypatch, Facts())
    monkeypatch.setattr(llm, "get_settings", lambda: _Stub())

    body = e2e.preflight(None)

    assert body["model"]["api_key"] == {"source": "LLM_API_KEY", "last4": "cdef"}
    assert "sk-synthetic-1234567890abcdef" not in str(body)


def test_the_preflight_admits_when_no_key_is_configured(monkeypatch):
    from app.services import llm

    class _Stub:
        llm_text_api_key = None
        llm_api_key = None

    _patch(monkeypatch, Facts())
    monkeypatch.setattr(llm, "get_settings", lambda: _Stub())

    body = e2e.preflight(None)

    assert body["model"]["api_key"] is None


def test_the_cli_model_line_says_where_the_key_came_from(monkeypatch):
    """The CLI text is the operator's own screen: it may name the key's
    source and last four characters (user phase 1)."""
    from app.services import llm

    class _Stub:
        llm_text_api_key = "sk-text-abcd1234"
        llm_api_key = None

    _patch(monkeypatch, Facts())
    monkeypatch.setattr(llm, "get_settings", lambda: _Stub())

    result = e2e.run_end_to_end(
        "nonexistent-tender-ref", session=object(), check_only=True
    )

    text = result.as_text()
    assert "key from LLM_TEXT_API_KEY, ending 1234" in text
    assert "sk-text-abcd1234" not in text
