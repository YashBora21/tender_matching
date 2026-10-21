"""Migration test: the migration creates the PRD's tables.

Requires a disposable database. Skipped when TEST_DATABASE_URL is not set, so the
default `pytest` run needs no Postgres and no Docker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration

# Every table the PRD requires, plus the additions in PRD 5.3.1.
EXPECTED_TABLES = {
    "manufacturer",
    "product",
    "document",
    "chunk",
    "fact",
    "alias",
    "tender",
    "requirement",
    "run",
    "result",
    "tender_page",
    "tender_table",
    "tender_item",
    "extraction_batch",
    "attribute",
}


def test_migration_creates_all_prd_tables(test_database_url: str) -> None:
    engine = create_engine(test_database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    try:
        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", test_database_url)
        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            names = set(inspect(conn).get_table_names())

        assert EXPECTED_TABLES <= names
        assert "vector" in {
            row[0] for row in engine.connect().execute(text("SELECT extname FROM pg_extension"))
        }
    finally:
        engine.dispose()
