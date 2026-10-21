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
def fresh_settings() -> None:
    """Drop the cached Settings around every test.

    `get_settings` is lru_cached because config is read once at startup, but a test
    that changes an env var must see the change.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url
