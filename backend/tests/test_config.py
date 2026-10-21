"""Config tests: values come from the environment, secrets never leak."""

from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    settings = Settings()
    assert settings.log_level == "DEBUG"
    assert settings.llm_model == "test-model"


def test_public_summary_never_contains_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    summary = get_settings().public_summary()
    assert "super-secret-value" not in str(summary)
    assert summary["llm_configured"] is True


def test_llm_is_not_configured_without_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # A key alone cannot call the LLM, so the service is not configured.
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("LLM_MODEL", "")
    assert get_settings().public_summary()["llm_configured"] is False


def test_cors_origins_are_split_and_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test , http://b.test")
    assert get_settings().cors_origin_list == ["http://a.test", "http://b.test"]


def test_scoring_policy_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCORING_FAMILY_LEVEL_CAP", "0.25")
    policy = get_settings().scoring
    assert policy.family_level_cap == 0.25
