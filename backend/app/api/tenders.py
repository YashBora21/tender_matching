"""Tender routes (PRD 11): the tenders, their attachments, and the manual
upload that stands in for a fetch that failed.

A tender PDF is a wrapper: it links to the bill of quantities and the
specification, and it embeds nothing. The pipeline fetches those links. When
a fetch fails — the link has moved, the host is unreachable, the tender
was withdrawn — the tender is *not* a tender with no requirements. It is a
tender whose attachments are missing, and the fix is to upload them.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.enums import HealthState

logger = get_logger(__name__)
router = APIRouter(prefix="/api/tenders", tags=["tenders"])


def _tender_dir(tender_id: str) -> Path:
    """Where a tender's frozen attachments live."""
    return get_settings().data_dir / "tenders" / tender_id


def _read_manifest(tender_id: str) -> dict:
    path = _tender_dir(tender_id) / "manifest.json"
    if not path.exists():
        return {"tender_id": tender_id, "documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tender_id": tender_id, "documents": []}
    data.setdefault("documents", [])
    return data


def _write_manifest(tender_id: str, manifest: dict) -> None:
    directory = _tender_dir(tender_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


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

    directory = _tender_dir(tender_id)
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

    from app.services.tendercollect import classify_document, now_iso, sha256_file

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
