"""Test configuration.

Unit tests run with no database and no Docker. Database-backed tests are marked
`integration` and skip unless TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs a live PostgreSQL database")


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch) -> None:
    """Drop the cached Settings around every test.

    `get_settings` is lru_cached because config is read once at startup, but a test
    that changes an env var must see the change.
    """
    monkeypatch.setenv('OCR_BACKEND', 'tesseract')
    # Synthetic endpoint: unit tests must never depend on private host config.
    monkeypatch.setenv('LLM_BASE_URL', 'https://synthetic-llm.example.test/v1')
    monkeypatch.setenv('LLM_TEXT_API_KEY', '')
    monkeypatch.setenv('LLM_CALL_BUDGET', '0')
    monkeypatch.setenv('LLM_TOKEN_BUDGET', '0')
    monkeypatch.setenv('LLM_MAX_RETRIES', '3')
    monkeypatch.setenv('LLM_RETRY_SECONDS', '0')
    # Single-answer fakes must not inherit a deployment batch size.
    monkeypatch.setenv('LLM_MATCH_EXTRACTION_BATCH_SIZE', '1')
    monkeypatch.setenv('REQUIREMENT_EXTRACTION_BATCH_SIZE', '1')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database() -> None:
    """Apply the migrations once before any database-backed test.

    The integration tests assert on a schema, not on migration order. Without
    this they relied on the migration test happening to run first, and on a
    fresh database the five ingestion tests ran against an empty schema (D1).
    """
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return

    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
