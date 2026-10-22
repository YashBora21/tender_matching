"""Tender attachment collection.

Every GeM tender PDF is a wrapper: it links to the bill of quantities and
the specification, and it embeds nothing. This module finds those links,
fetches what robots.txt allows, and freezes each document to disk with its
URL, its hash and the date it was taken.

A document that cannot be fetched is recorded as failed. It is never
recorded as a tender with no requirements: those are different things, and
only one of them is a fact about the tender.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.fetching import FetchRefused, fetch, robots_allows
from app.core.logging import get_logger

logger = get_logger(__name__)

# The kinds of document a tender links to, by what is in them. A link's file
# name says nothing - the same shape carries the BOQ in one tender and a
# certificate in another - so the document is identified by its content.
BOQ_MARKERS = (
    "bill of quantity",
    "boq item",
    "item number",
    "item no",
    "item title",
    "brief description",
    "item description",
    "material code",
    "material description",
)
QUANTITY_MARKERS = ("quantity", "qty", "quantiry", "quantite")
SPEC_MARKERS = (
    "material of construction",
    "technical specification",
    "scope of supply",
    "sec-ts-",
    "specification",
    "shall be",
)
FORM_MARKERS = (
    "declaration",
    "certificate",
    "undertaking",
    "checklist",
    "price bid evaluation",
)


@dataclass
class TenderDocument:
    """One document a tender links to, as it was fetched."""

    url: str
    status: str
    # new | unchanged | changed | failed | restricted | manual
    kind: str = "unknown"
    filename: Optional[str] = None
    path: Optional[str] = None
    sha256: Optional[str] = None
    previous_sha256: Optional[str] = None
    retrieved_at: Optional[str] = None
    bytes: Optional[int] = None
    pages: Optional[int] = None
    # Why a fetch was refused or failed.
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class TenderManifest:
    """What one tender is: the wrapper, and the documents it links to."""

    tender_id: str
    category: str = "unknown"
    wrapper_path: Optional[str] = None
    wrapper_sha256: Optional[str] = None
    retrieved_at: Optional[str] = None
    documents: list[TenderDocument] = field(default_factory=list)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def discover_links(pdf_path: Path) -> list[str]:
    """The document links in a tender wrapper.

    Only the links that point at a document: a form, a catalogue page and a
    mailto are not documents.
    """
    import pymupdf

    document = pymupdf.open(pdf_path)
    links: list[str] = []
    for index in range(document.page_count):
        for link in document[index].get_links():
            uri = link.get("uri")
            if not uri:
                continue
            lowered = uri.lower()
            if not lowered.endswith((".pdf", ".csv")):
                continue
            if "downloadomppdfile" in lowered or "gtc/pdfbydate" in lowered:
                continue
            links.append(uri)
    document.close()
    return list(dict.fromkeys(links))


def classify_document(data: bytes, filename: str) -> tuple[str, Optional[int]]:
    """What a document is, from its content, and how many pages it has.

    A scanned document has no text at all: it is classified by its first
    text, and when it has none it is still a document - the OCR pass reads
    it later. A document that cannot be opened is unknown, and the fetch is
    recorded as failed rather than as a document that says nothing.
    """
    if filename.lower().endswith(".csv"):
        text = data.decode("utf-8", "replace").lower()
        kind = "boq" if ("item number" in text or "item title" in text) else "csv"
        return kind, None

    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return "unknown", None
    pages = document.page_count
    body = " ".join((document[index].get_text() or "") for index in range(min(pages, 2)))
    document.close()
    low = body.lower()
    if any(marker in low for marker in BOQ_MARKERS) and any(
        marker in low for marker in QUANTITY_MARKERS
    ):
        return "boq", pages
    if any(marker in low for marker in SPEC_MARKERS):
        return "spec", pages
    if any(marker in low for marker in FORM_MARKERS):
        return "form", pages
    if not body.strip():
        # No text at all: a scanned document. It is still a document.
        return "scanned", pages
    return "other", pages


def _filename_for(url: str, index: int) -> str:
    from urllib.parse import urlsplit

    name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not name:
        name = f"document-{index}"
    return name


def collect_tender(
    tender_id: str,
    wrapper_path: Path,
    output_dir: Path,
    *,
    category: str = "unknown",
    previous: Optional[dict] = None,
) -> TenderManifest:
    """Fetch and freeze every document a tender links to.

    A document that cannot be fetched is recorded as failed, with the reason.
    It is never recorded as a tender with no requirements: those are
    different facts, and only one of them is true.
    """
    manifest = TenderManifest(
        tender_id=tender_id,
        category=category,
        wrapper_path=str(wrapper_path),
        wrapper_sha256=sha256_file(wrapper_path),
        retrieved_at=now_iso(),
    )
    raw_dir = output_dir / "raw"
    known = {
        doc.get("url"): doc
        for doc in (previous or {}).get("documents", [])
        if doc.get("url")
    }
    cache_enabled = get_settings().fetch_cache_enabled

    for index, url in enumerate(discover_links(wrapper_path)):
        allowed, note = robots_allows(url)
        if not allowed:
            manifest.documents.append(
                TenderDocument(url=url, status="restricted", note=note)
            )
            continue

        old = known.get(url)
        # The frozen copy is the cache: a document that is already on disk
        # with the same hash is not fetched again.
        if cache_enabled and old and old.get("sha256"):
            frozen = raw_dir / (old.get("filename") or "")
            if frozen.exists() and sha256_file(frozen) == old["sha256"]:
                manifest.documents.append(
                    TenderDocument(
                        url=url,
                        status="unchanged",
                        kind=old.get("kind", "unknown"),
                        filename=old.get("filename"),
                        path=old.get("path"),
                        sha256=old["sha256"],
                        retrieved_at=old.get("retrieved_at"),
                        bytes=old.get("bytes"),
                        pages=old.get("pages"),
                        note=note,
                    )
                )
                continue

        try:
            got = fetch(url)
        except FetchRefused as exc:
            manifest.documents.append(
                TenderDocument(url=url, status="failed", note=str(exc))
            )
            continue

        digest = sha256_bytes(got.data)
        filename = _filename_for(url, index)
        target = raw_dir / filename
        kind, pages = classify_document(got.data, filename)

        status = "new"
        previous_sha = None
        if old:
            if old.get("sha256") == digest:
                status = "unchanged"
            else:
                status = "changed"
                previous_sha = old.get("sha256")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(got.data)

        manifest.documents.append(
            TenderDocument(
                url=url,
                status=status,
                kind=kind,
                filename=filename,
                path=f"raw/{filename}",
                sha256=digest,
                previous_sha256=previous_sha,
                retrieved_at=now_iso(),
                bytes=len(got.data),
                pages=pages,
                note=note,
            )
        )

    return manifest


def write_manifest(manifest: TenderManifest, output_dir: Path) -> Path:
    """Write a tender's manifest, atomically."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "manifest.json"
    payload = {
        "tender_id": manifest.tender_id,
        "category": manifest.category,
        "wrapper_path": manifest.wrapper_path,
        "wrapper_sha256": manifest.wrapper_sha256,
        "retrieved_at": manifest.retrieved_at,
        "documents": [doc.to_dict() for doc in manifest.documents],
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def read_manifest(path: Path) -> dict:
    """Read a tender manifest, or an empty one."""
    if not path.exists():
        return {"documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"documents": []}
    data.setdefault("documents", [])
    return data
