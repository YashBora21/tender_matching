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
