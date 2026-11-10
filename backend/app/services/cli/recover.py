"""A read that stopped with no process left to finish it (D-120).

The background read lives inside the API process (`services.tenderupload`).
The pipeline commits each stage as it goes, so a process that dies mid-read
leaves the stage rows behind and the tender's own row still saying `processing`.
Nothing then ever writes a terminal state: the row is polled forever, and the
requirements stream stays open because `processing` means "still being read".

This closes that hole at the only moment it can be closed safely - when a
process starts and has not scheduled a read of its own yet. It decides from the
stage rows, never from a timer: a stage that stopped at the provider's limit
says so, and that is a state the next run resumes from. A read that is genuinely
in flight has a stage in a non-terminal state, so it is left alone even when
another worker shares the database.
"""

from __future__ import annotations

from typing import Mapping, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.repositories.tender import TenderRepo
from app.schemas.enums import TenderStatus

logger = get_logger(__name__)

# What a stage leaves behind when it reached a verdict. `skipped` is a verdict:
# the stage decided there was nothing for it to do.
_SETTLED = frozenset({"done", "skipped"})
# The states a background read owns, and therefore the states a dead read
# leaves behind.
_ORPHANED = (TenderStatus.QUEUED, TenderStatus.PROCESSING)


def final_state(
    stages: Mapping[str, str], requirements: int
) -> Optional[TenderStatus]:
    """The state a tender should hold, read from its stages alone.

    `None` means the read has not settled - either it never started (no stage)
    or a stage is still in flight - and a process may still be writing to it.

    A tender whose stages all settled but which stored no requirement is left
    alone rather than called `done`: "the document was read" and "the document
    stated nothing" are the two answers PRD 4 forbids merging, and the stages
    alone cannot tell `done` from `incomplete` (D-91).

    A tender with requirements stored and **no stage at all** is the other
    half of that: the pipeline writes the requirements first and the stage
    rows after (`pipeline._persist`), so a process that died in between leaves
    exactly this - the answer written, the record of having read it missing -
    and nothing will ever finish it. It is called `incomplete`, which is what
    it is (D-157).
    """
    if not stages:
        if requirements > 0:
            return TenderStatus.INCOMPLETE
        return None
    values = stages.values()
    if "rate_limited" in values:
        return TenderStatus.RATE_LIMITED
    if "failed" in values:
        return TenderStatus.FAILED
    if requirements > 0 and all(status in _SETTLED for status in values):
        return TenderStatus.DONE
    return None


def _reason(
    repo: TenderRepo, tender_id: str, status: TenderStatus
) -> Optional[str]:
    """The stage's own words for why the read stopped, when it has any."""
    if status is TenderStatus.DONE:
        return None
    wanted = (
        "rate_limited" if status is TenderStatus.RATE_LIMITED else "failed"
    )
    for section, state in repo.stage_statuses(tender_id).items():
        if state != wanted:
            continue
        detail = (repo.get_stage(tender_id, section) or {}).get("detail")
        if detail:
            return detail
    if status is TenderStatus.RATE_LIMITED:
        return "the read stopped at the provider's limit and no process finished it"
    if status is TenderStatus.INCOMPLETE:
        return (
            "the read stored its requirements and never recorded which stages "
            "it finished, so the row was left saying it was still reading"
        )
    return "the read failed and no process recorded why"


def settle(
    session: Session, tender_id: str, *, repo: Optional[TenderRepo] = None
) -> Optional[TenderStatus]:
    """Record the state of a read no process is finishing, if it has one.

    The caller owns the transaction, exactly as `reconcile` arranges it. This
    is the per-tender half: the upload route asks it about a duplicate the
    database still calls `processing`, because that row is what stops the
    caller's own re-upload from scheduling a read (D-157).
    """
    repo = repo or TenderRepo(session)
    status = final_state(
        repo.stage_statuses(tender_id), repo.count_requirements(tender_id)
    )
    if status is None:
        return None
    if status is TenderStatus.DONE:
        from app.services.tender.completeness import completion_gaps
        gaps = completion_gaps(repo.get_tender(tender_id) or {}, repo.list_attachments(tender_id),
                               repo.list_pages(tender_id), repo.list_requirements(tender_id))
        if gaps:
            repo.set_status(tender_id, TenderStatus.INCOMPLETE, error='; '.join(gaps))
            return TenderStatus.INCOMPLETE
    repo.set_status(tender_id, status, error=_reason(repo, tender_id, status))
    logger.warning(
        "tender %s was left reading by a read that never finished; recorded %s",
        tender_id,
        status.value,
    )
    return status


def reconcile(
    session: Session, *, repo: Optional[TenderRepo] = None
) -> list[dict]:
    """Finish the reads that no process is going to finish.

    Returns what it repaired, one row per tender, so a caller can report it
    rather than repair it silently. The caller owns the transaction: this
    writes rows and does not commit them.
    """
    repo = repo or TenderRepo(session)
    repaired: list[dict] = []
    for row in repo.tenders_in_states(_ORPHANED):
        tender_id = row["id"]
        status = settle(session, tender_id, repo=repo)
        if status is None:
            continue
        repaired.append(
            {"tender_id": tender_id, "was": row["status"], "now": status.value}
        )
    return repaired
