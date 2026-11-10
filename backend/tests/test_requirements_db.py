"""Replacing a tender's requirements against a real database.

Skipped unless TEST_DATABASE_URL points at a disposable database. The
delete-then-insert is the point of it, so a fake repository cannot prove it:
a result names one requirement row, and the foreign key refuses to delete a
row a result still points at. A tender that had ever been matched could not be
read again until the results went first (D-128).

Every row here is synthetic.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import Session

from app.repositories.schema import manufacturer, requirement, result, run
from app.repositories.tender import StoredRequirement, TenderRepo
from app.schemas.enums import (
    Applicability,
    RequirementStatus,
    RunStatus,
    VerificationOutcome,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def rollback_session(test_database_url: str):
    """A session whose writes are rolled back at the end."""
    engine = create_engine(test_database_url, future=True)
    with Session(engine, future=True) as db:
        transaction = db.begin()
        yield db
        transaction.rollback()
    engine.dispose()


def _stored(tender_id: str, text_value: str = "Body shall be SS316") -> StoredRequirement:
    return StoredRequirement(
        tender_id=tender_id,
        item_id="1",
        text=text_value,
        source_text=text_value,
        page=1,
        requirement_type="technical",
        extraction_version="test",
        schema_version="test",
    )


def _a_run_with_one_result(session: Session, tender_id: str, label: str) -> tuple:
    """A run of this tender, and the one result that answers its requirement."""
    requirement_id = session.execute(
        select(requirement.c.id).where(requirement.c.tender_id == tender_id)
    ).scalar_one()
    maker_id = session.execute(
        insert(manufacturer)
        .values(name=f"{label}-maker")
        .returning(manufacturer.c.id)
    ).scalar_one()
    run_id = session.execute(
        insert(run)
        .values(tender_id=tender_id, status=RunStatus.DONE.value)
        .returning(run.c.id)
    ).scalar_one()
    session.execute(
        insert(result).values(
            run_id=run_id,
            requirement_id=requirement_id,
            manufacturer_id=maker_id,
            status=RequirementStatus.FULL_MATCH.value,
            verification_outcome=VerificationOutcome.PASSED.value,
            applicability=Applicability.MODEL_LEVEL.value,
            score=1.0,
        )
    )
    return run_id, requirement_id


def test_a_reread_supersedes_requirements_and_preserves_results(
    rollback_session,
):
    repo = TenderRepo(rollback_session)
    tender_id = repo.upsert_tender(
        filename="reread.pdf",
        sha256=hashlib.sha256(b"synthetic reread test").hexdigest(),
        page_count=1,
    )
    repo.replace_items(tender_id, [{"item_id": "1", "name": "Ball valve"}])
    repo.replace_requirements(tender_id, [_stored(tender_id)])
    rollback_session.flush()
    run_id, old_requirement = _a_run_with_one_result(rollback_session, tender_id, "reread")

    written = repo.replace_requirements(tender_id, [_stored(tender_id, "Body shall be SS304")])
    rollback_session.flush()

    assert written == 1
    kept = rollback_session.execute(
        select(result.c.id).where(result.c.run_id == run_id)
    ).all()
    assert len(kept) == 1
    remaining = rollback_session.execute(
        select(func.count()).select_from(requirement).where(
            requirement.c.tender_id == tender_id
        )
    ).scalar_one()
    assert remaining == 2
    assert repo.count_requirements(tender_id) == 1
    survivors = rollback_session.execute(
        select(requirement.c.id).where(requirement.c.tender_id == tender_id)
    ).scalars().all()
    assert old_requirement in survivors
    assert str(old_requirement) not in {row["id"] for row in repo.list_requirements(tender_id)}


def test_the_items_stage_can_also_replace_a_tender_that_was_already_matched(
    rollback_session,
):
    """The items stage runs first, so this is the delete the re-upload actually
    hit: it removes the requirements before it removes the items."""
    repo = TenderRepo(rollback_session)
    tender_id = repo.upsert_tender(
        filename="items-reread.pdf",
        sha256=hashlib.sha256(b"synthetic items reread").hexdigest(),
        page_count=1,
    )
    repo.replace_items(tender_id, [{"item_id": "1", "name": "Ball valve"}])
    repo.replace_requirements(tender_id, [_stored(tender_id)])
    rollback_session.flush()
    run_id, _ = _a_run_with_one_result(rollback_session, tender_id, "items-reread")

    written = repo.replace_items(
        tender_id, [{"item_id": "1", "name": "Ball valve"}, {"item_id": "2", "name": "Gate valve"}]
    )
    rollback_session.flush()

    assert written == 2
    kept = rollback_session.execute(
        select(result.c.id).where(result.c.run_id == run_id)
    ).all()
    assert len(kept) == 1
