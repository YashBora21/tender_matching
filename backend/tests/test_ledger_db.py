"""The page ledger against a real database.

Skipped unless TEST_DATABASE_URL points at a disposable database. The ledger is
the one place the ingestion's page states are stored, and the SQL that stores
them - the delete-then-insert that makes a re-read replace rather than append -
is the point of it, so a fake repository cannot prove it.

Every row here is synthetic: no real tender, no real page.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.repositories.tender import TenderRepo

pytestmark = pytest.mark.integration


@pytest.fixture
def rollback_session(test_database_url: str):
    """A session whose writes are rolled back at the end.

    The tests run the real SQL and leave nothing behind.
    """
    engine = create_engine(test_database_url, future=True)
    with Session(engine, future=True) as db:
        transaction = db.begin()
        yield db
        transaction.rollback()
    engine.dispose()


def _tender_with_spec(repo: TenderRepo, label: str) -> tuple[str, str]:
    """A synthetic tender and the attachment its pages come from."""
    tender_id = repo.upsert_tender(
        filename=f"{label}.pdf",
        sha256=hashlib.sha256(f"synthetic ledger test {label}".encode()).hexdigest(),
        page_count=3,
    )
    attachment_id = repo.upsert_attachment(
        tender_id,
        {
            "url": f"https://tender.example/{label}-spec.pdf",
            "filename": f"{label}-spec.pdf",
            "kind": "spec",
            "status": "new",
        },
    )
    return tender_id, attachment_id


def test_every_page_state_round_trips_with_the_reason_of_the_failed_one(rollback_session):
    """FR-T2: a page that could not be read is stored as `failed` with the
    reason, not as a page that was processed."""
    repo = TenderRepo(rollback_session)
    tender_id, attachment_id = _tender_with_spec(repo, "ledger-states")

    written = repo.replace_pages(
        tender_id,
        [
            {
                "page": 1,
                "tags": ["technical"],
                "extraction_method": "table",
                "quality_warnings": [],
                "ledger_status": "parsed",
                "skip_reason": None,
                "attachment_id": attachment_id,
            },
            {
                "page": 2,
                "tags": ["technical"],
                "extraction_method": "ocr",
                "quality_warnings": ["the page is mostly a picture"],
                "ledger_status": "ocr",
                "skip_reason": None,
                "attachment_id": attachment_id,
            },
            {
                "page": 3,
                "tags": ["boilerplate"],
                "extraction_method": None,
                "quality_warnings": [],
                "ledger_status": "failed",
                "skip_reason": "the OCR read nothing on this page",
                "attachment_id": attachment_id,
            },
        ],
    )
    rollback_session.flush()

    assert written == 3
    rows = {row["page"]: row for row in repo.list_pages(tender_id)}
    assert sorted(rows) == [1, 2, 3]
    assert rows[1]["ledger_status"] == "parsed"
    assert rows[2]["ledger_status"] == "ocr"
    assert rows[3]["ledger_status"] == "failed"
    assert rows[3]["skip_reason"] == "the OCR read nothing on this page"
    assert rows[1]["attachment_id"] == attachment_id


def test_a_second_read_replaces_the_ledger_instead_of_appending_to_it(rollback_session):
    """A tender read twice has one ledger. Rows left from the first read would
    describe pages that the current read never saw."""
    repo = TenderRepo(rollback_session)
    tender_id, attachment_id = _tender_with_spec(repo, "ledger-replace")

    def page(number: int, status: str) -> dict:
        return {
            "page": number,
            "tags": ["technical"],
            "extraction_method": "text",
            "quality_warnings": [],
            "ledger_status": status,
            "skip_reason": None,
            "attachment_id": attachment_id,
        }

    repo.replace_pages(tender_id, [page(1, "parsed"), page(2, "parsed")])
    rollback_session.flush()
    repo.replace_pages(tender_id, [page(1, "parsed"), page(2, "failed")])
    rollback_session.flush()

    rows = repo.list_pages(tender_id)
    assert [row["page"] for row in rows] == [1, 2]
    assert [row["ledger_status"] for row in rows] == ["parsed", "failed"]


def test_a_tender_that_has_never_been_read_has_an_empty_ledger(rollback_session):
    """The backfill decides to re-read a tender by asking whether its ledger is
    empty, so an unread tender must answer with nothing."""
    repo = TenderRepo(rollback_session)
    tender_id, _ = _tender_with_spec(repo, "ledger-empty")

    assert repo.list_pages(tender_id) == []


def test_replacing_a_ledger_with_nothing_leaves_nothing(rollback_session):
    """A read that returned no page is not a read that kept the old pages."""
    repo = TenderRepo(rollback_session)
    tender_id, attachment_id = _tender_with_spec(repo, "ledger-cleared")
    repo.replace_pages(
        tender_id,
        [
            {
                "page": 1,
                "tags": [],
                "extraction_method": None,
                "quality_warnings": [],
                "ledger_status": "skipped",
                "skip_reason": "boilerplate",
                "attachment_id": attachment_id,
            }
        ],
    )
    rollback_session.flush()

    assert repo.replace_pages(tender_id, []) == 0
    rollback_session.flush()
    assert repo.list_pages(tender_id) == []
