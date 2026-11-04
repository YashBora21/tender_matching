"""A read that stopped with no process left to finish it (D-120).

The decision is a pure function of the stage rows, so most of it is tested
without a database. The SQL that writes the answer is tested against a real
one, because the point of the repair is what the row says afterwards.

Every row here is synthetic: no real tender, no real page.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.repositories.tender import StoredRequirement, TenderRepo
from app.schemas.enums import TenderStatus
from app.services.cli.recover import final_state, reconcile


# --- the decision, from the stages alone ---------------------------------


def test_a_stage_that_stopped_at_the_limit_is_rate_limited():
    """The provider's limit is a state the next run resumes from, not a
    failure and not a finished read (D8)."""
    stages = {"attachments": "done", "extraction": "rate_limited"}

    assert final_state(stages, requirements=31) is TenderStatus.RATE_LIMITED


def test_a_failed_stage_is_failed():
    stages = {"attachments": "done", "items": "failed"}

    assert final_state(stages, requirements=0) is TenderStatus.FAILED


def test_every_stage_settled_with_requirements_is_done():
    """A process that died between the last stage and the status write is
    finished here, from what the stages already prove."""
    stages = {
        "attachments": "done",
        "items": "done",
        "requirements": "done",
        "resolution": "skipped",
        "extraction": "done",
    }

    assert final_state(stages, requirements=21) is TenderStatus.DONE


def test_settled_stages_with_no_requirement_are_left_alone():
    """"The document was read" and "the document stated nothing" are the two
    answers PRD 4 forbids merging, and the stages alone cannot tell `done` from
    `incomplete`: this row is left for a person rather than guessed (D-91)."""
    stages = {"attachments": "done", "items": "done", "requirements": "done"}

    assert final_state(stages, requirements=0) is None


def test_a_stage_still_in_flight_is_left_alone():
    """A slow read is not a dead one, and another worker may be writing."""
    stages = {"attachments": "done", "extraction": "running"}

    assert final_state(stages, requirements=12) is None


def test_a_tender_with_no_stage_is_left_alone():
    """Nothing has been read, so nothing can be concluded about it."""
    assert final_state({}, requirements=0) is None


def test_requirements_stored_with_no_stage_are_incomplete():
    """The pipeline writes the requirements first and the stage rows after, so
    a process killed in between leaves the answer stored and no record of
    reading it. Nothing will ever finish it: it is `incomplete`, not `still
    reading` (D-157)."""
    assert final_state({}, requirements=31) is TenderStatus.INCOMPLETE


# --- the repair, against a real database ---------------------------------

pytestmark_db = pytest.mark.integration


@pytest.fixture
def rollback_session(test_database_url: str):
    """A session whose writes are rolled back at the end."""
    engine = create_engine(test_database_url, future=True)
    with Session(engine, future=True) as db:
        transaction = db.begin()
        yield db
        transaction.rollback()
    engine.dispose()


def _orphaned_tender(repo: TenderRepo, label: str) -> str:
    """A tender left in `processing` by a read that never finished."""
    tender_id = repo.upsert_tender(
        filename=f"{label}.pdf",
        sha256=hashlib.sha256(f"synthetic recovery test {label}".encode()).hexdigest(),
        page_count=2,
    )
    repo.set_status(tender_id, TenderStatus.PROCESSING)
    return tender_id


@pytestmark_db
def test_a_processing_tender_whose_extraction_was_rate_limited_is_repaired(
    rollback_session,
):
    """The row the UI polls forever: `processing`, with the provider's refusal
    sitting in a stage. It becomes the state the next run resumes from."""
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "zombie-limit")
    repo.save_stage(
        tender_id,
        "extraction",
        status="rate_limited",
        key="k1",
        detail="the model is rate limited: TPM limit 8000",
    )
    repo.save_stage(tender_id, "attachments", status="done", key="k2")
    rollback_session.flush()

    repaired = reconcile(rollback_session)

    assert [row["tender_id"] for row in repaired] == [tender_id]
    assert repaired[0]["was"] == "processing"
    assert repaired[0]["now"] == "rate_limited"
    row = repo.get_tender(tender_id)
    assert row["status"] == "rate_limited"
    assert "TPM limit 8000" in (row["error"] or "")


@pytestmark_db
def test_a_read_still_in_flight_is_not_touched(rollback_session):
    """Another process may be working on it: its stages are not settled."""
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "in-flight")
    repo.save_stage(tender_id, "attachments", status="done", key="k1")
    repo.save_stage(tender_id, "extraction", status="running", key="k2")
    rollback_session.flush()

    assert reconcile(rollback_session) == []
    assert repo.get_tender(tender_id)["status"] == "processing"


@pytestmark_db
def test_a_finished_read_that_died_before_writing_its_state_is_marked_done(
    rollback_session,
):
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "finished")
    repo.save_stage(tender_id, "items", status="done", key="k1")
    repo.save_stage(tender_id, "requirements", status="done", key="k2")
    repo.replace_requirements(
        tender_id,
        [
            StoredRequirement(
                tender_id=tender_id,
                item_id=None,
                text="Body shall be SS316",
                source_text="Body shall be SS316",
                page=1,
                requirement_type="technical",
                extraction_version="test",
                schema_version="test",
            )
        ],
    )
    rollback_session.flush()

    repaired = reconcile(rollback_session)

    assert repaired[0]["now"] == "done"
    row = repo.get_tender(tender_id)
    assert row["status"] == "done"
    assert row["error"] is None


@pytestmark_db
def test_a_processing_tender_whose_stages_were_never_written_is_repaired(
    rollback_session,
):
    """The row that can never move: requirements stored, not one stage row.
    Nothing is reading it, and nothing ever will be (D-157)."""
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "no-stages")
    repo.replace_requirements(
        tender_id,
        [
            StoredRequirement(
                tender_id=tender_id,
                item_id=None,
                text="The connector shall have LED indication",
                source_text="The connector shall have LED indication",
                page=1,
                requirement_type="unknown",
                extraction_version="test",
                schema_version="test",
            )
        ],
    )
    rollback_session.flush()

    repaired = reconcile(rollback_session)

    assert [row["tender_id"] for row in repaired] == [tender_id]
    assert repaired[0]["now"] == "incomplete"
    row = repo.get_tender(tender_id)
    assert row["status"] == "incomplete"
    assert "never recorded which stages" in (row["error"] or "")


@pytestmark_db
def test_starting_a_read_clears_the_reason_of_the_attempt_before_it(
    rollback_session,
):
    """A processing row that still carries the previous attempt's rate-limit
    message reports a cause it never had (D-120)."""
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "stale-reason")
    repo.set_status(
        tender_id, TenderStatus.RATE_LIMITED, error="the model is rate limited"
    )
    rollback_session.flush()

    repo.set_status(tender_id, TenderStatus.PROCESSING)
    rollback_session.flush()

    assert repo.get_tender(tender_id)["error"] is None


@pytestmark_db
def test_finishing_a_read_clears_the_reason_it_was_resumed_from(
    rollback_session,
):
    repo = TenderRepo(rollback_session)
    tender_id = _orphaned_tender(repo, "resumed-ok")
    repo.set_status(
        tender_id, TenderStatus.RATE_LIMITED, error="the model is rate limited"
    )
    rollback_session.flush()

    repo.set_status(tender_id, TenderStatus.DONE)
    rollback_session.flush()

    row = repo.get_tender(tender_id)
    assert row["status"] == "done"
    assert row["error"] is None
