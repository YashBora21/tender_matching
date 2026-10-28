"""Migration test: the migration creates the PRD's tables.

Requires a disposable database. Skipped when TEST_DATABASE_URL is not set, so the
default `pytest` run needs no Postgres and no Docker.

The test migrates a schema of its own and drops that schema, never `public`:
the database it points at may hold a corpus or a run, and a test must not
delete the evidence a report was built from (D11).
"""

from __future__ import annotations

import uuid

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
    schema = f"migration_test_{uuid.uuid4().hex[:8]}"
    # `public` stays on the path so the vector extension's type resolves even
    # though the tables are created in the test's schema.
    engine = create_engine(
        test_database_url,
        future=True,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        cfg.set_main_option("sqlalchemy.url", test_database_url)
        # The version table belongs to this schema. Without this alembic reads
        # the version in `public`, which is on the search path, finds it at
        # head, and creates nothing here.
        cfg.set_main_option("version_table_schema", schema)

        with engine.begin() as conn:
            # The migration runs on this connection, whose search_path puts
            # the test schema first: alembic_version and every table land
            # there, and nothing is written to public.
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            names = set(inspect(conn).get_table_names(schema=schema))
            extensions = {
                row[0]
                for row in conn.execute(text("SELECT extname FROM pg_extension"))
            }

        assert EXPECTED_TABLES <= names
        assert "vector" in extensions
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_the_embedding_column_has_the_model_dimension(test_database_url: str) -> None:
    """The column is `vector(384)`, so a vector from another model is refused
    by PostgreSQL rather than compared in the wrong space (D-6, 0009)."""
    engine = create_engine(test_database_url, future=True)
    try:
        with engine.connect() as conn:
            dimension = conn.execute(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'chunk'::regclass AND attname = 'embedding'"
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert dimension == "vector(384)"
