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
from io import BytesIO
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

# A page whose images cover more of it than this is a scanned page.
_SCANNED_PAGE_RATIO = 0.5


# The version of the classifier below. A change to how a document is
# recognised is a change to what the stage produces, so it is part of the
# stage's key: the stage runs again, and only that stage.
CLASSIFIER_VERSION = "2"


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
    # The model or family the document covers. It is what a fact about the
    # document is about, so it is recorded.
    model_or_family: Optional[str] = None
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
    filename: Optional[str] = None
    wrapper_path: Optional[str] = None
    wrapper_sha256: Optional[str] = None
    page_count: Optional[int] = None
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

    A bill of quantities is recognised by its *header*, not by its prose: the
    words "Item", "Descriptions", "Qty" and "Unit" sit in a table, and the
    PDF splits them across lines, so a phrase search misses them. The header
    is read from the table, and the prose is only the second opinion.

    A scanned document is one whose pages are images. A scanned page often
    carries a little text - a file number, a date - so the amount of text is
    not the test: the *ratio* of page area covered by images is. A document
    that is mostly images is scanned, and the OCR pass reads it later.

    A document that cannot be opened is unknown, and the fetch is recorded as
    failed rather than as a document that says nothing.
    """
    if filename.lower().endswith(".csv"):
        text = data.decode("utf-8", "replace").lower()
        kind = "boq-csv" if ("item number" in text or "item title" in text) else "csv"
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

    if _is_scanned(data):
        # The pages are images. A scanner writes a little text on them - a
        # file number, a date - and that text must not decide what the
        # document is. It is still a document, and the OCR pass reads it
        # later.
        return "scanned", pages
    if _has_boq_header(data):
        return "boq-pdf", pages
    if any(marker in low for marker in BOQ_MARKERS) and any(
        marker in low for marker in QUANTITY_MARKERS
    ):
        return "boq-pdf", pages
    if any(marker in low for marker in SPEC_MARKERS):
        return "spec", pages
    if any(marker in low for marker in FORM_MARKERS):
        return "form", pages
    return "other", pages


def _is_scanned(data: bytes) -> bool:
    """True when the document's pages are images rather than text.

    A scanned page keeps whatever text the scanner's software wrote on it -
    a file number, a date - so the amount of text says nothing. The area
    the images cover does.
    """
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return False
    try:
        for index in range(min(document.page_count, 3)):
            page = document[index]
            area = page.rect.get_area()
            if not area:
                continue
            covered = 0.0
            for image in page.get_image_info():
                bbox = image["bbox"]
                covered = max(covered, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            if covered / area > _SCANNED_PAGE_RATIO:
                return True
    finally:
        document.close()
    return False


def _has_boq_header(data: bytes) -> bool:
    """True when the first pages hold a table with a bill-of-quantities header.

    Only the first two pages are read: pdfplumber is slow on a long
    document, and the header is at the front.
    """
    import pdfplumber

    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            for page in pdf.pages[:2]:
                for table in page.extract_tables() or []:
                    for row in table[:4]:
                        header = " ".join(str(cell or "").lower() for cell in row)
                        if (
                            ("description" in header or "particulars" in header)
                            and ("qty" in header or "quantity" in header)
                        ):
                            return True
    except Exception as exc:  # noqa: BLE001 - a table that cannot be read is not a table
        logger.debug("could not read the table header: %s", exc)
    return False


def _filename_for(url: str, index: int) -> str:
    """The name a document is frozen under.

    GeM puts the real name in the query string: every SLA and ATC link ends in
    `/contract/slafds?fileDownloadPath=.../CPS2HMDAM1.pdf`, so the path alone
    names four different documents `slafds` and the last one written wins. The
    download path is read first, and the URL path is the fallback for the hosts
    that carry the name there.
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    parts = urlsplit(url)
    download = parse_qs(parts.query).get("fileDownloadPath", [""])[0]
    name = unquote(download).rsplit("/", 1)[-1] if download else ""
    if not name:
        name = parts.path.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not name:
        name = f"document-{index}"
    return name


def _page_count(path: Path) -> Optional[int]:
    """How many pages the wrapper has, or None when it cannot be opened."""
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened has no pages
        return None
    try:
        return document.page_count
    finally:
        document.close()


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
        filename=wrapper_path.name,
        wrapper_path=str(wrapper_path),
        wrapper_sha256=sha256_file(wrapper_path),
        page_count=_page_count(wrapper_path),
        retrieved_at=now_iso(),
    )
    raw_dir = output_dir / "raw"
    known = {
        doc.get("url"): doc
        for doc in (previous or {}).get("documents", [])
        if doc.get("url")
    }
    cache_enabled = get_settings().fetch_cache_enabled
    claimed: dict[str, str] = {}

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
        data: Optional[bytes] = None
        filename = _filename_for(url, index)
        # Two links can still name the same file. A suffix keeps both on disk
        # instead of letting the second one silently overwrite the first.
        if claimed.setdefault(filename, url) != url:
            filename = f"{index}-{filename}"
            claimed[filename] = url
        target = raw_dir / filename
        if cache_enabled and old and old.get("sha256"):
            frozen = raw_dir / (old.get("filename") or "")
            if frozen.exists() and sha256_file(frozen) == old["sha256"]:
                data = frozen.read_bytes()

        if data is None:
            try:
                got = fetch(url)
            except FetchRefused as exc:
                manifest.documents.append(
                    TenderDocument(url=url, status="failed", note=str(exc))
                )
                continue
            data = got.data

        digest = sha256_bytes(data)
        # The document is classified from the file on disk, every time: a
        # change to the classifier has to take effect without fetching
        # everything again.
        kind, pages = classify_document(data, filename)

        status = "new"
        previous_sha = None
        if old:
            if old.get("sha256") == digest:
                status = "unchanged"
            else:
                status = "changed"
                previous_sha = old.get("sha256")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

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
                bytes=len(data),
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
        "filename": manifest.filename,
        "wrapper_path": manifest.wrapper_path,
        "wrapper_sha256": manifest.wrapper_sha256,
        "page_count": manifest.page_count,
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


def manifest_identity(manifest: dict, tender_dir: Path) -> tuple[Optional[str], Optional[str]]:
    """The hash and the name of a tender, from its manifest.

    A manifest written before the wrapper was recorded has no hash of its
    own, so it is taken from the wrapper file beside it. The tender is
    identified by what it contains, and the wrapper is what it contains.
    """
    sha256 = manifest.get("wrapper_sha256")
    filename = manifest.get("filename")
    if not sha256:
        wrapper = manifest.get("wrapper_path")
        if wrapper and Path(wrapper).exists():
            sha256 = sha256_file(Path(wrapper))
        else:
            # The wrapper sits beside the manifest it describes.
            for candidate in sorted(tender_dir.glob("*.pdf")):
                sha256 = sha256_file(candidate)
                filename = filename or candidate.name
                break
    if not filename:
        candidate = manifest.get("tender_id")
        filename = f"{candidate}.pdf" if candidate else None
    return sha256, filename
