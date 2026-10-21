"""Ingestion against a real database.

Skipped unless TEST_DATABASE_URL points at a disposable database. The
repository code is the only part of the ingestion that cannot be tested with
a fake, because the SQL it builds is the point of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.repositories.postgres import PostgresDocumentRepo
from app.repositories.schema import chunk, document, manufacturer

pytestmark = pytest.mark.integration


def _pdf(path: Path, text_content: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), text_content, fontsize=10)
    doc.save(path)
    doc.close()


@pytest.fixture
def rollback_session(test_database_url: str):
    """A session whose writes are rolled back at the end.

    The tests run the real SQL against a real database and leave nothing
    behind, so they need no disposable database of their own.
    """
    engine = create_engine(test_database_url, future=True)
    with Session(engine, future=True) as db:
        transaction = db.begin()
        yield db
        transaction.rollback()
    engine.dispose()


def test_a_document_and_its_chunks_are_written(rollback_session):
    repo = PostgresDocumentRepo(rollback_session)
    document_id = repo.upsert_document(
        {
            "manufacturer": "Acme Valves",
            "url": "https://acme.example/d.pdf",
            "sha256": "a" * 64,
            "doc_type": "datasheet",
            "retrieved_at": "2026-10-21T09:00:00+00:00",
            "path": "raw/d.pdf",
            "model_or_family": "GV series",
            "category": "gate_valves",
        }
    )
    written = repo.insert_chunks(
        document_id,
        [
            {"page": 1, "section": "3.2 Materials", "text": "Body shall be SS316"},
            {"page": 2, "is_table": True, "text": "Pressure | 16 | bar", "row_id": "r1"},
        ],
    )
    rollback_session.flush()

    assert written == 2
    rows = rollback_session.execute(select(chunk.c.text, chunk.c.page, chunk.c.is_table)).all()
    assert len(rows) == 2
    assert {row.page for row in rows} == {1, 2}


def test_the_same_document_is_not_written_twice(rollback_session):
    repo = PostgresDocumentRepo(rollback_session)
    record = {
        "manufacturer": "Acme Valves",
        "url": "https://acme.example/d.pdf",
        "sha256": "b" * 64,
        "doc_type": "datasheet",
        "path": "raw/d.pdf",
    }
    first = repo.upsert_document(record)
    second = repo.upsert_document(record)
    rollback_session.flush()

    assert first == second
    count = rollback_session.execute(select(func.count()).select_from(document)).scalar()
    assert count == 1


def test_re_ingesting_replaces_the_old_chunks(rollback_session):
    """A document that changed is re-ingested whole: the old chunks describe
    content that is no longer in the file."""
    repo = PostgresDocumentRepo(rollback_session)
    document_id = repo.upsert_document(
        {
            "manufacturer": "Acme Valves",
            "sha256": "c" * 64,
            "doc_type": "datasheet",
            "path": "raw/d.pdf",
        }
    )
    repo.insert_chunks(document_id, [{"page": 1, "text": "old text"}])
    rollback_session.flush()

    repo.insert_chunks(document_id, [{"page": 1, "text": "new text"}])
    rollback_session.flush()

    rows = rollback_session.execute(select(chunk.c.text)).all()
    assert [row.text for row in rows] == ["new text"]


def test_the_manufacturer_is_created_once(rollback_session):
    repo = PostgresDocumentRepo(rollback_session)
    repo.get_manufacturer_id("Acme Valves")
    repo.get_manufacturer_id("Acme Valves")
    rollback_session.flush()

    count = rollback_session.execute(select(func.count()).select_from(manufacturer)).scalar()
    assert count == 1


def test_the_keyword_index_finds_a_chunk(rollback_session):
    """FR-11's keyword half: the chunk text is searchable."""
    repo = PostgresDocumentRepo(rollback_session)
    document_id = repo.upsert_document(
        {
            "manufacturer": "Acme Valves",
            "sha256": "d" * 64,
            "doc_type": "datasheet",
            "path": "raw/d.pdf",
        }
    )
    repo.insert_chunks(
        document_id,
        [{"page": 1, "text": "The body shall be austenitic stainless steel grade SS316"}],
    )
    rollback_session.flush()

    found = rollback_session.execute(
        select(chunk.c.text).where(
            chunk.c.search_vector.op("@@")(
                func.to_tsquery("english", "stainless")
            )
        )
    ).all()
    assert len(found) == 1
    assert "SS316" in found[0].text
