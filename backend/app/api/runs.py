"""Run routes (PRD 11): start a matching run, poll it, read its report.

A run is started in the background and answered by its id: the request that
starts it does not wait for the corpus to be searched, and the caller polls
`GET /api/runs/{id}` for the progress. The four report routes read what the run
stored; they do not re-run anything and they never call the LLM.

A report is only served once the run has stopped - `done` or `failed`. A report
of a run that is still working would be a report of half a run, and half a run
reads as a manufacturer that answered nothing.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.schemas.enums import RunStatus
from app.services.report import build_report, render_csv, render_html
from app.services.runs import create_run, execute_run
from app.services.runs import progress as run_progress

logger = get_logger(__name__)
router = APIRouter(prefix="/api/runs", tags=["runs"])

# A run that has stopped has an answer, even when the answer is that it failed.
# A rate-limited run has stopped too: the answers it found are kept, and the
# report says how far it got.
_TERMINAL = frozenset(
    {
        RunStatus.DONE.value,
        RunStatus.FAILED.value,
        RunStatus.RATE_LIMITED.value,
    }
)


class RunRequest(BaseModel):
    """What a caller must say to start a run.

    `manufacturers` is optional: without it every manufacturer in the corpus is
    assessed. The PRD's endpoint also lists "options"; none are defined there,
    so none are accepted - an option the run ignored would be a promise the
    API does not keep.
    """

    tender_id: str
    manufacturers: Optional[list[str]] = None


def _identifier(value: str, kind: str) -> str:
    """The id as a UUID, or a 404.

    A malformed id is not a run that does not exist, but the caller gets the
    same answer: there is nothing at that address. Checking it here keeps a
    bad path from reaching the database, where it would be a 500.
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError(f"no {kind} with id {value}") from None


def _run(session: Session, run_id: str) -> dict:
    """The run, or a 404. Raises `ConflictError` while it is still working."""
    state = run_progress(session, run_id).as_dict()
    if state["status"] not in _TERMINAL:
        raise ConflictError(
            f"run {run_id} has not finished (status {state['status']})",
            {"progress": state},
        )
    return build_report(session, run_id)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_run(
    request: RunRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    """Start a run: create it, answer with its id, search in the background."""
    tender_id = _identifier(request.tender_id, "tender")
    if request.manufacturers is not None and not request.manufacturers:
        # An empty list is not "all of them": the caller asked for none, and
        # a run against no manufacturer has nothing to report.
        raise ValidationError("manufacturers is empty; omit it to use the whole corpus")

    run_id = create_run(session, tender_id, request.manufacturers)
    # Committed before the background task is scheduled: that task opens its
    # own session, and it must be able to see the row it was told to run.
    session.commit()
    logger.info("run %s queued for tender %s", run_id, tender_id)

    background.add_task(execute_run, run_id)
    return run_progress(session, run_id).as_dict()


@router.get("/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_session)) -> dict:
    """The state of a run and how far it has got."""
    return run_progress(session, _identifier(run_id, "run")).as_dict()


@router.get("/{run_id}/report")
def get_report(run_id: str, session: Session = Depends(get_session)) -> dict:
    """The full JSON report: every requirement, manufacturer, quote and flag."""
    return _run(session, _identifier(run_id, "run"))


@router.get("/{run_id}/report.html", response_class=HTMLResponse)
def get_report_html(run_id: str, session: Session = Depends(get_session)) -> HTMLResponse:
    """The report a bid manager reads."""
    report = _run(session, _identifier(run_id, "run"))
    return HTMLResponse(content=render_html(report))


@router.get("/{run_id}/export.csv")
def export_csv(run_id: str, session: Session = Depends(get_session)) -> Response:
    """The report as a spreadsheet: one row per requirement × manufacturer."""
    report = _run(session, _identifier(run_id, "run"))
    return Response(
        content=render_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}.csv"'},
    )
