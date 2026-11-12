"""Tender routes (PRD 11): the tenders, their attachments, and the manual
upload that stands in for a fetch that failed.

A tender PDF is a wrapper: it links to the bill of quantities and the
specification, and it embeds nothing. The pipeline fetches those links. When
a fetch fails â€” the link has moved, the host is unreachable, the tender
was withdrawn â€” the tender is *not* a tender with no requirements. It is a
tender whose attachments are missing, and the fix is to upload them.

`POST /api/tenders` is the other direction: a caller hands over the tender
itself. The file is frozen, the tender is created or found by its hash, and
the reading happens in the background â€” a 200-page tender is minutes of work
and the request that uploads it must not wait for it.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.repositories.db import session_scope
from app.repositories.tender import TenderRepo
from app.schemas.enums import TenderStatus
from app.services.cli.recover import settle
from app.services.tender.upload import (
    ingest,
    safe_name,
    store_upload,
    tender_dir,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/api/tenders", tags=["tenders"])


def _read_manifest(tender_id: str) -> dict:
    path = tender_dir(tender_id) / "manifest.json"
    if not path.exists():
        return {"tender_id": tender_id, "documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tender_id": tender_id, "documents": []}
    data.setdefault("documents", [])
    return data


def _write_manifest(tender_id: str, manifest: dict) -> None:
    directory = tender_dir(tender_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _identifier(value: str) -> str:
    """The id as a UUID, or a 404.

    A malformed id is not a tender that does not exist, but the caller gets
    the same answer: there is nothing at that address. Checking it here keeps
    a bad path from reaching the database, where it would be a 500.
    """
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise NotFoundError(f"no tender with id {value}") from None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_tender(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Accept a tender PDF and start reading it.

    The size is checked while reading, so an oversized upload is refused
    without being written; the content is checked before the name is trusted
    (PRD 13). The answer is the tender's id, not its requirements: those exist
    when the background read has finished.
    """
    settings = get_settings()
    name = safe_name(file.filename)
    data = await file.read(settings.fetch_max_bytes + 1)
    if len(data) > settings.fetch_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"the file is over the {settings.fetch_max_bytes} byte limit"
            ),
        )

    uploaded = store_upload(session, filename=name, data=data)
    # Committed before the background task is scheduled: that task opens its
    # own session, and it must be able to see the row it was told to read.
    session.commit()

    # A read that died between storing its requirements and recording its
    # stages leaves the row saying `processing` with nothing reading it. That
    # row is what stops the caller's own re-upload from scheduling a read, so
    # it is settled here before the decision below (D-157).
    state = uploaded.status
    if uploaded.duplicate and state == TenderStatus.PROCESSING.value:
        settled = settle(session, uploaded.tender_id, repo=TenderRepo(session))
        if settled is not None:
            session.commit()
            state = settled.value

    # A new file is read now. A duplicate is read again when its earlier
    # reading stopped short â€” it failed, or the provider's limit cut it off â€”
    # because that read is resumable and this is the caller asking for it. A
    # tender already reading is left to finish.
    if not uploaded.duplicate or state in (
        TenderStatus.FAILED.value,
        TenderStatus.RATE_LIMITED.value,
        TenderStatus.INCOMPLETE.value,
    ):
        background.add_task(ingest, uploaded.tender_id)

    logger.info("tender %s accepted (%s)", uploaded.tender_id, name)
    return {
        "tender_id": uploaded.tender_id,
        "status": state,
        "duplicate": uploaded.duplicate,
    }


@router.post("/{tender_id}/read-again", status_code=status.HTTP_202_ACCEPTED)
def read_again(
    tender_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    """Read a tender again, with the limits in force now (user step 5).

    The explicit action a resume needs: a reading that stopped at a budget or
    rate limit is resumed by asking for it, not by re-uploading bytes. The new
    attempt supersedes the old reading - its requirements are superseded by
    the fresh ones, never destroyed - and the attempt ledger keeps every
    previous reading, each with the limits it ran under.

    A tender already being read is refused rather than double-read: two
    background reads writing the same rows is a race, not a resume.
    """
    identifier = _identifier(tender_id)
    repo = TenderRepo(session)
    tender = repo.get_tender(identifier, for_update=True)
    if tender is None:
        raise NotFoundError(f"no tender with id {tender_id}")
    if tender["status"] in (TenderStatus.QUEUED.value, TenderStatus.PROCESSING.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the tender is already being read",
        )

    repo.set_status(identifier, TenderStatus.QUEUED)
    # Committed before the task is scheduled: the task opens its own session
    # and must be able to see the state it was told to read.
    session.commit()
    background.add_task(ingest, identifier, force=True)
    logger.info("tender %s read again", identifier)
    return {"tender_id": identifier, "status": TenderStatus.QUEUED.value}


@router.get("/{tender_id}/requirements")
def get_requirements(
    tender_id: str, session: Session = Depends(get_session)
) -> dict:
    """The tender's parsed atomic requirements (PRD 11).

    A tender that is still being read answers with what has been read so far
    and says so in `partial` (D-82). The rows are real requirements, never a
    guess at what the rest of the document says, and the status is what stops a
    reader from taking a short list for a complete one (PRD 4).
    """
    identifier = _identifier(tender_id)
    repo = TenderRepo(session)
    tender = repo.get_tender(identifier)
    if tender is None:
        raise NotFoundError(f"no tender with id {tender_id}")
    return _requirements_payload(repo, tender)


@router.get("/{tender_id}/requirements/stream")
def stream_requirements(tender_id: str) -> StreamingResponse:
    """Push the requirements while the tender is being read (D-82).

    The same payload as the route above, sent as `text/event-stream`: a
    `snapshot` when the client connects, a `requirements` event whenever what
    is stored changes, and an `end` when the tender stops reading. A client
    that cannot read a stream falls back to polling the plain route.
    """
    identifier = _identifier(tender_id)
    # Checked before the stream starts, so a wrong id is a 404 and not a
    # connection that stays open saying nothing.
    with session_scope() as session:
        tender = TenderRepo(session).get_tender(identifier)
    if tender is None:
        raise NotFoundError(f"no tender with id {tender_id}")

    return StreamingResponse(
        _requirements_events(identifier),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # A proxy that buffers would hold every event until the read
            # finished, which is the wait this endpoint exists to remove.
            "X-Accel-Buffering": "no",
        },
    )


# The states in which a tender is still being read, and its requirement list is
# therefore partial.
_READING = (TenderStatus.QUEUED.value, TenderStatus.PROCESSING.value)


def _requirements_payload(repo: TenderRepo, tender: dict) -> dict:
    """What the requirements routes answer, from the rows stored right now."""
    items = {item["item_id"]: item for item in repo.list_items(tender["id"])}
    from app.services.tender.source_policy import context_only, source_context
    requirements = []
    contexts = []
    for row in repo.list_requirements(tender["id"]):
        item = items.get(row.get("item_id")) or {}
        (contexts if context_only(row) else requirements).append(
            {
                **row,
                **source_context(row),
                "item_title": item.get("name"),
                "category": item.get("category"),
            }
        )

    return {
        "tender_id": str(tender["id"]),
        "filename": tender.get("filename"),
        "status": tender["status"],
        "error": tender.get("error"),
        "reason_code": tender.get("reason_code"),
        "page_count": tender.get("page_count"),
        # A short list while the read is running is not the document stating
        # few requirements: the flag is what tells the two apart.
        "partial": tender["status"] in _READING,
        "counts": {
            "items": len(items),
            "requirements": len(requirements),
            "flagged": sum(1 for row in requirements if row.get("review_required")),
        },
        "requirements": requirements,
        "context_clauses": contexts,
    }


def _sse(event: str, data: dict) -> str:
    """One server-sent event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _requirements_events(tender_id: str) -> Iterator[str]:
    """The tender's requirements, as they are read (D-82).

    The extraction writes a snapshot of what it has read so far, so the stream
    is a read of that table at an interval rather than a second copy of the
    extraction's state. That is what lets a client reconnect and still be shown
    everything, and what keeps the list correct when the read stops short.
    """
    interval = get_settings().stream_interval_seconds
    previous: Optional[dict] = None

    while True:
        with session_scope() as session:
            repo = TenderRepo(session)
            tender = repo.get_tender(tender_id)
            payload = None if tender is None else _requirements_payload(repo, tender)

        if payload is None:
            yield _sse("error", {"tender_id": tender_id, "message": "the tender is gone"})
            return

        # The final state is an event of its own, carrying the same payload:
        # the client replaces its list and knows the read has stopped. A
        # tender that had already finished before the client connected ends
        # immediately instead of opening with a snapshot and closing.
        if not payload["partial"]:
            yield _sse("end", payload)
            return

        if payload != previous:
            yield _sse("snapshot" if previous is None else "requirements", payload)
            previous = payload

        # A comment is not an event. It keeps the connection alive while the
        # model is thinking and nothing has changed yet.
        yield ": keep-alive\n\n"
        time.sleep(interval)


@router.get("/{tender_id}/attachments")
def list_attachments(tender_id: str) -> dict:
    """Every attachment of a tender: its name, its URL and its hash.

    This is what the report lists, and what the UI shows next to an
    attachment that could not be fetched.
    """
    manifest = _read_manifest(tender_id)
    documents = []
    for record in manifest.get("documents", []):
        documents.append(
            {
                "url": record.get("url"),
                "name": record.get("filename"),
                "sha256": record.get("sha256"),
                "kind": record.get("kind"),
                "status": record.get("status"),
                "bytes": record.get("bytes"),
                "pages": record.get("pages"),
                "retrieved_at": record.get("retrieved_at"),
                "note": record.get("note"),
            }
        )
    return {
        "tender_id": tender_id,
        "category": manifest.get("category"),
        "attachments": documents,
        # What the UI needs to decide whether to prompt for an upload.
        "missing": [
            record.get("url")
            for record in manifest.get("documents", [])
            if record.get("status") in ("failed", "restricted")
        ],
    }


@router.post("/{tender_id}/attachments")
def upload_attachment(
    tender_id: str,
    file: UploadFile = File(...),
    url: Optional[str] = Form(default=None),
    kind: Optional[str] = Form(default=None),
) -> dict:
    """Upload a document a fetch could not get.

    This is the fallback the UI offers when an attachment failed: the
    reviewer has the file, and the pipeline is given it directly. The
    document is frozen with its hash like any other, and the manifest
    records where it came from, so the report can still say which file was
    used.
    """
    settings = get_settings()
    name = Path(file.filename or "attachment.pdf").name
    if not name or name.startswith("."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="the file has no name"
        )
    if Path(name).suffix.lower() not in (".pdf", ".docx", ".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a tender attachment is a PDF, a DOCX or a CSV",
        )

    directory = tender_dir(tender_id)
    raw = directory / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / name

    with target.open("wb") as handle:
        shutil.copyfileobj(file.file, handle)

    size = target.stat().st_size
    if size > settings.fetch_max_bytes:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"the file is {size} bytes, over the {settings.fetch_max_bytes} limit",
        )

    from app.services.tender.collect import classify_document, now_iso, sha256_file

    digest = sha256_file(target)
    detected_kind, pages = classify_document(target.read_bytes(), name)

    manifest = _read_manifest(tender_id)
    manifest.setdefault("tender_id", tender_id)
    documents = manifest.setdefault("documents", [])

    # An upload replaces the failed record for the same URL, so the report
    # lists one attachment and not two.
    source = url or f"upload://{name}"
    for record in documents:
        if record.get("url") == source or (
            url and record.get("url") == url and record.get("status") != "ok"
        ):
            record.update(
                {
                    "url": source,
                    "status": "manual",
                    "kind": kind or detected_kind,
                    "filename": name,
                    "path": f"raw/{name}",
                    "sha256": digest,
                    "retrieved_at": now_iso(),
                    "bytes": size,
                    "pages": pages,
                    "note": None,
                }
            )
            break
    else:
        documents.append(
            {
                "url": source,
                "status": "manual",
                "kind": kind or detected_kind,
                "filename": name,
                "path": f"raw/{name}",
                "sha256": digest,
                "retrieved_at": now_iso(),
                "bytes": size,
                "pages": pages,
            }
        )

    _write_manifest(tender_id, manifest)
    logger.info("uploaded attachment %s for tender %s", name, tender_id)

    return {
        "tender_id": tender_id,
        "name": name,
        "url": source,
        "sha256": digest,
        "kind": kind or detected_kind,
        "bytes": size,
        "pages": pages,
        "status": "manual",
    }
