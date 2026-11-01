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


def test_a_relative_data_dir_is_read_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DATA_DIR=backend/data` means the same thing from any directory.

    Read as a plain relative path it resolves against the working directory,
    so the same test run from `backend/` looked for `backend/backend/data`
    and found an empty corpus instead of failing.
    """
    from app.core.config import REPO_ROOT

    monkeypatch.setenv("DATA_DIR", "backend/data")
    settings = Settings()

    assert settings.data_dir.is_absolute()
    assert settings.data_dir == REPO_ROOT / "backend" / "data"


def test_an_absolute_data_dir_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert Settings().data_dir == tmp_path


def test_scoring_policy_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCORING_FAMILY_LEVEL_CAP", "0.25")
    policy = get_settings().scoring
    assert policy.family_level_cap == 0.25


def test_the_llm_client_reads_its_address_key_and_model_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three LLM_* variables are the only source of the client's target.

    A provider is a base URL, not a library: pointing the service at another
    OpenAI-compatible endpoint is an environment change, never a code edit.
    """
    from app.services.llm import LLMClient

    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "key-from-env")
    monkeypatch.setenv("LLM_MODEL", "model-from-env")

    client = LLMClient()

    assert client.base_url == "https://llm.example.test/v1"
    assert client.api_key == "key-from-env"
    assert client.model == "model-from-env"


def test_the_llm_client_refuses_to_start_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key is a loud failure, never a silent empty extraction."""
    from app.services.llm import LLMClient, LLMError

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "model-from-env")

    with pytest.raises(LLMError):
        LLMClient()


# Variables `.env.example` documents that no setting reads: the compose file,
# the scripts and Tesseract itself consume these.
NOT_A_SETTING = {
    "DATABASE_URL_DOCKER",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "TESSDATA_PREFIX",
    "VITE_API_URL",
    "TEST_DATABASE_URL",
}


def test_the_example_env_file_and_the_settings_are_the_same_list() -> None:
    """Every setting has a line in `.env.example`, and every line is a setting.

    The example file is the whole documented interface to a deployment, so it is
    checked against the model instead of kept in step by memory: a setting with
    no line is a knob nobody can find, and a line with no setting is a knob that
    silently does nothing (D-121).
    """
    import re
    from pathlib import Path

    example = (
        Path(__file__).resolve().parents[2] / ".env.example"
    ).read_text(encoding="utf-8")
    documented = {
        match.group(1)
        # A commented line is still documentation: an optional setting ships
        # commented out, with its default as the example value.
        for match in re.finditer(r"^#?\s*([A-Z][A-Z0-9_]*)=", example, re.MULTILINE)
    }
    settings = {name.upper() for name in Settings.model_fields}

    assert sorted(settings - documented) == []
    assert sorted(documented - settings - NOT_A_SETTING) == []
