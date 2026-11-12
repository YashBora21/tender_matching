"""Receiving a tender a caller uploads (PRD 11, FR-12).

`POST /api/tenders` hands a PDF to this module, and three things happen:

1. **The file is frozen first.** It is written under
   `DATA_DIR/tenders/<tender_id>/` with its hash in a manifest, the same shape
   `make tenders` produces, so a run and a report cannot tell an uploaded
   tender from a collected one. Nothing is parsed before the bytes are safe.
2. **The tender row is found or created by its hash.** The same file uploaded
   twice is the same tender, not a second one (FR-12).
3. **The pipeline runs in the background.** Reading a 200-page tender is
   minutes of parsing and extraction; the request that uploads it answers with
   an id and the caller polls `GET /api/tenders/{id}/requirements`.

The tender's state is recorded while this happens, so "still reading" is never
mistaken for "read it and found nothing" - which would be the
absence-of-evidence error PRD 4 forbids.

A tender is usually a *wrapper*: a PDF that links to its bill of quantities and
its specification, and embeds neither. The links are fetched by the same code
the corpus runner uses. A tender that is itself the bill of quantities - a
single PDF with the table inside - becomes its own attachment instead of being
reported as a tender with nothing in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.repositories.tender import LLMCache, TenderRepo
from app.schemas.enums import TenderReasonCode, TenderStatus
from app.services.corpus.audit import audit_tender
from app.services.tender.collect import (
    TenderDocument,
    TenderManifest,
    classify_document,
    collect_tender,
    now_iso,
    read_manifest,
    sha256_bytes,
    write_manifest,
)

logger = get_logger(__name__)

# What an uploaded PDF can be in its own right. A wrapper is none of these and
# gets its content from the documents it links to.
_SELF_READING = frozenset({"boq-pdf", "boq-csv", "spec", "scanned"})


def effective_limits() -> dict:
    """The process's numeric limits, read now (user step 5).

    This is what a reading attempt is frozen with when it starts, and what
    `/api/preflight` shows as the current effective limits. A resumed attempt
    is governed by these, never by the limits an earlier attempt recorded: the
    previous attempt's row is history, not configuration. No model identity
    and no endpoint are in here - those are deployment configuration, not
    metrics.
    """
    from app.services.tender.ingest import READER_VERSION
    from app.services.tender.source_policy import fingerprint
    settings = get_settings()
    limits = {
        # 0 means "no ceiling", as everywhere else the budgets are read.
        "call_budget": settings.llm_call_budget,
        "token_budget": settings.llm_token_budget,
        "max_output_tokens": settings.llm_max_tokens,
        "max_retries": settings.llm_max_retries,
        "spend_caps_enabled": getattr(settings, 'llm_spend_caps_enabled', False),
        "schema_version": settings.schema_version,
        "app_version": settings.app_version,
        "reader_version": READER_VERSION,
        "source_policy": fingerprint(),
    }
    if limits['spend_caps_enabled']:
        limits["daily_cap_inr"] = settings.llm_daily_cap_inr
        limits["monthly_cap_inr"] = settings.llm_monthly_cap_inr
    return limits


def _attempt_start(repo: TenderRepo, tender_id: str) -> Optional[str]:
    try:
        with repo.session.begin_nested():
            tender = repo.get_tender(tender_id) or {}
            return repo.start_reading_attempt(tender_id, limits=effective_limits(),
                sha256=tender.get("sha256"), filename=tender.get("filename"))
    except Exception:  # The savepoint preserves the caller's successful writes.
        logger.warning("tender %s: reading history unavailable; apply migrations", tender_id)
        return None


def _attempt_finish(repo: TenderRepo, attempt_id: Optional[str], *, status: str,
                    stop_reason=None, reason_code=None, metrics=None) -> None:
    if attempt_id is None:
        return
    try:
        with repo.session.begin_nested():
            repo.finish_reading_attempt(attempt_id, status=status, stop_reason=stop_reason,
                                        reason_code=reason_code, metrics=metrics)
    except Exception:
        logger.warning("reading attempt %s could not be closed", attempt_id)


@dataclass
class Uploaded:
    """What `POST /api/tenders` answers with."""

    tender_id: str
    status: str
    duplicate: bool = False


def tender_dir(tender_id: str) -> Path:
    """Where a tender's frozen files live: its wrapper and its attachments."""
    return get_settings().data_dir / "tenders" / tender_id


def safe_name(filename: Optional[str]) -> str:
    """The uploaded file's own name, or an error.

    A name from a client is untrusted: it is reduced to its final component so
    it cannot walk out of the tender's directory, and a tender has to be a PDF.
    """
    name = Path((filename or "").replace("\\", "/")).name
    if not name or name.startswith("."):
        raise ValidationError("the uploaded file has no name")
    if Path(name).suffix.lower() != ".pdf":
        raise ValidationError("a tender is a PDF; other documents are attachments")
    return name


def is_pdf(data: bytes) -> bool:
    """A PDF starts with `%PDF-`, whatever its name says."""
    return data.startswith(b"%PDF-")


def store_upload(
    session: Session,
    *,
    filename: str,
    data: bytes,
    category: Optional[str] = None,
) -> Uploaded:
    """Freeze the file, and give it the tender row that identifies it.

    The caller has already checked the size and the type; this is the writing
    and the bookkeeping. A file already frozen is not written twice: the
    manifest beside it is the record that it was read.
    """
    name = safe_name(filename)
    if not data:
        raise ValidationError("the uploaded file is empty")
    if not is_pdf(data):
        raise ValidationError("the uploaded file is not a PDF")

    digest = sha256_bytes(data)
    _, pages = classify_document(data, name)

    repo = TenderRepo(session)
    try:
        tender_id = repo.upsert_tender(
            filename=name,
            sha256=digest,
            page_count=pages,
            category=category,
            status=TenderStatus.QUEUED,
        )
    except IntegrityError:
        # Two uploads of the same file at once: the other one created the row.
        session.rollback()
        tender_id = repo.upsert_tender(
            filename=name,
            sha256=digest,
            page_count=pages,
            category=category,
            status=TenderStatus.QUEUED,
        )

    directory = tender_dir(tender_id)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        row = repo.get_tender(tender_id) or {}
        return Uploaded(
            tender_id=tender_id,
            status=row.get("status") or TenderStatus.DONE.value,
            duplicate=True,
        )

    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / name
    wrapper.write_bytes(data)
    write_manifest(
        TenderManifest(
            tender_id=tender_id,
            category=category or "unknown",
            filename=name,
            wrapper_path=str(wrapper),
            wrapper_sha256=digest,
            page_count=pages,
            retrieved_at=now_iso(),
            documents=[],
        ),
        directory,
    )
    logger.info("stored tender %s from %s", tender_id, name)
    return Uploaded(
        tender_id=tender_id, status=TenderStatus.QUEUED.value, duplicate=False
    )


def ingest(tender_id: str, *, force: bool = False) -> str:
    """Read a stored tender and write its requirements. Returns its final state.

    This is the background task. It opens its own session because the request
    that scheduled it has already been answered and its session closed.

    Each run of this function is one *reading attempt*: it is opened with the
    limits it will run under and closed with its outcome (user step 5). The
    attempt record is bookkeeping - a failure to write it is logged and never
    changes the reading's result.
    """
    from app.repositories.db import session_scope

    with session_scope() as session:
        repo = TenderRepo(session)
        # History writes use savepoints so a bookkeeping error cannot undo a read.
        attempt_id = _attempt_start(repo, tender_id)
        repo.set_status(tender_id, TenderStatus.PROCESSING)
        session.commit()

        try:
            status, error, code, metrics = _read(
                session, tender_id, reading_id=attempt_id, force=force
            )
        except Exception as exc:  # noqa: BLE001 - the state is the answer
            session.rollback()
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("tender %s could not be read: %s", tender_id, exc)
            # The attempt is closed first: if closing it fails, its rollback
            # must not discard the status that is written after it.
            _attempt_finish(
                repo,
                attempt_id,
                status=TenderStatus.FAILED.value,
                stop_reason=message,
            )
            repo.set_status(tender_id, TenderStatus.FAILED, error=message)
            session.commit()
            return TenderStatus.FAILED.value

        # Closing history must preserve the pipeline's successful writes.
        _attempt_finish(
            repo,
            attempt_id,
            status=status.value,
            stop_reason=error,
            reason_code=code.value if code else None,
            metrics=metrics,
        )
        repo.set_status(tender_id, status, error=error, reason_code=code)
        session.commit()
        logger.info("tender %s %s", tender_id, status.value)
        return status.value


def _read(
    session: Session,
    tender_id: str,
    *,
    reading_id: Optional[str] = None,
    force: bool = False,
) -> tuple[TenderStatus, Optional[str], Optional[TenderReasonCode], dict]:
    """Collect the tender's documents and run its pipeline.

    The fourth element is what the attempt ledger records as the attempt's
    metrics: the counts the audit measured, or an empty dict when the read
    stopped before there was anything to count.
    """
    metrics: dict = {}
    directory = tender_dir(tender_id)
    manifest = read_manifest(directory / "manifest.json")
    wrapper = _wrapper(directory, manifest)
    if wrapper is None:
        return (
            TenderStatus.FAILED,
            "the uploaded file is no longer on disk; upload it again",
            None,
            metrics,
        )

    collected = collect_tender(
        tender_id,
        wrapper,
        directory,
        category=manifest.get("category") or "unknown",
        previous=manifest,
    )
    # A document a person uploaded is not a link the wrapper carries, so it is
    # not rediscovered. It is kept: the reviewer put it there because the
    # fetch could not get it (FR-4).
    collected.documents.extend(_manual_documents(manifest, collected))
    if not collected.documents:
        own = _wrapper_document(wrapper, directory)
        if own is not None:
            collected.documents.append(own)
    write_manifest(collected, directory)

    audit = audit_tender(
        directory,
        repo=TenderRepo(session),
        # The cache writes carry this tender and this attempt as provenance,
        # so a reset can later tell tender-derived entries from legacy ones.
        cache=LLMCache(session, tender_id=tender_id, reading_id=reading_id),
        force=force,
    )
    metrics = {
        # Tolerant on purpose: the audit is a duck type (a caller may hand
        # audit_tender a stub), and the attempt's counts are bookkeeping -
        # a missing number is recorded as 0, never allowed to fail the read.
        "llm_calls": getattr(audit, "llm_calls", 0),
        "llm_cache_hits": getattr(audit, "llm_cache_hits", 0),
        "prompt_tokens": getattr(audit, "prompt_tokens", 0),
        "completion_tokens": getattr(audit, "completion_tokens", 0),
        "llm_retries": getattr(audit, "llm_retries", 0),
        "validation_attempts": getattr(audit, "validation_attempts", []),
        "attachments": getattr(audit, "attachments", 0),
        "attachments_failed": getattr(audit, "attachments_failed", 0),
        "items": getattr(audit, "items", 0),
        "requirements": getattr(audit, "requirements", 0),
        "pages_read": getattr(audit, "pages_read", 0),
    }
    if audit.failure:
        return TenderStatus.FAILED, audit.failure, audit.failure_code, metrics
    if audit.rate_limited:
        # The extraction stopped at the provider's limit. The requirements it
        # did read are stored; the tender is resumable, so it is not failed
        # and not done (D8).
        return (
            TenderStatus.RATE_LIMITED,
            audit.rate_limited_detail,
            None,
            metrics,
        )
    if getattr(audit, 'completion_gaps', None):
        return TenderStatus.INCOMPLETE, '; '.join(audit.completion_gaps), None, metrics
    # A tender that was read but gave nothing is not `done`: "done" says the
    # document was read, and an empty requirement list would be read as a
    # tender that states nothing (PRD 4, D-91).
    if not audit.pages_read:
        return (
            TenderStatus.INCOMPLETE,
            "no page of the specification was read",
            TenderReasonCode.SPECIFICATION_UNREADABLE,
            metrics,
        )
    if not audit.items:
        return (
            TenderStatus.INCOMPLETE,
            "the bill of quantities gave no items",
            None,
            metrics,
        )
    if not audit.requirements:
        return (
            TenderStatus.INCOMPLETE,
            "the specification gave no requirements",
            TenderReasonCode.NO_REQUIREMENTS_STATED,
            metrics,
        )
    return TenderStatus.DONE, None, None, metrics


def _wrapper(directory: Path, manifest: dict) -> Optional[Path]:
    """The uploaded PDF, from the manifest or beside it."""
    recorded = manifest.get("wrapper_path")
    if recorded:
        path = Path(recorded)
        if path.exists():
            return path
    name = manifest.get("filename")
    if name:
        candidate = directory / Path(name).name
        if candidate.exists():
            return candidate
    for candidate in sorted(directory.glob("*.pdf")):
        return candidate
    return None


def _manual_documents(previous: dict, collected: TenderManifest) -> list[TenderDocument]:
    """The documents that were uploaded by hand and are not links.

    `collect_tender` builds its manifest from the wrapper's links, so a
    document a reviewer uploaded to stand in for a failed fetch would vanish
    on the next attempt. It is carried over instead, and not duplicated when
    the same URL was fetched this time.
    """
    known = {document.url for document in collected.documents}
    kept: list[TenderDocument] = []
    for record in previous.get("documents", []):
        if record.get("status") != "manual" or record.get("url") in known:
            continue
        kept.append(
            TenderDocument(
                url=record["url"],
                status="manual",
                kind=record.get("kind") or "unknown",
                filename=record.get("filename"),
                path=record.get("path"),
                sha256=record.get("sha256"),
                model_or_family=record.get("model_or_family"),
                retrieved_at=record.get("retrieved_at"),
                bytes=record.get("bytes"),
                pages=record.get("pages"),
                note=record.get("note"),
            )
        )
    return kept


def _wrapper_document(wrapper: Path, directory: Path) -> Optional[TenderDocument]:
    """The uploaded PDF as an attachment, when it is readable on its own.

    A tender that embeds its bill of quantities has no links to follow. It is
    still a tender with a bill of quantities, so the file stands as its own
    attachment rather than being reported as an empty wrapper.

    This is the last resort - it only runs when the wrapper yielded no
    documents at all - so the whole file is classified, not its first two
    pages. A GeM bid document opens with bid details and carries its
    specification and BOQ table further in; two pages called it a form and
    the tender became "no attachments" (D-162).
    """
    try:
        data = wrapper.read_bytes()
    except OSError:  # pragma: no cover - `_wrapper` found it a moment ago
        return None
    kind, pages = classify_document(data, wrapper.name, page_limit=None)
    if kind not in _SELF_READING:
        return None
    try:
        relative = str(wrapper.relative_to(directory))
    except ValueError:  # pragma: no cover - the wrapper is inside the directory
        relative = wrapper.name
    return TenderDocument(
        url=f"upload://{wrapper.name}",
        status="manual",
        kind=kind,
        filename=wrapper.name,
        path=relative,
        sha256=sha256_bytes(data),
        retrieved_at=now_iso(),
        bytes=len(data),
        pages=pages,
    )
