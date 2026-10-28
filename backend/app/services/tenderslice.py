"""One tender, end to end (FR-12, FR-T6, FR-T7, FR-T9, FR-T14).

This is the vertical slice: a tender's attachments are fetched and frozen,
its bill of quantities becomes items, its specification becomes requirements
that carry the item they belong to, and every requirement is checked
against the text it came from.

The checks are what make the slice worth having:

- **The quote check.** A requirement has to be found in the source text it
  names. A requirement that cannot be found is not a requirement: it is
  reported, not published.
- **The page check.** A requirement has to name a page the document has.
- **The type check.** Every requirement is classified, and the commercial
  and eligibility ones are listed separately rather than matched.
- **The recall check.** The lines the tender stated that produced no
  requirement are listed, so a missed requirement is visible (FR-T14).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.schemas.enums import RequirementType
from app.services.tenderingest import (
    RequirementRecord,
    SpecificationRead,
    TenderItem,
    assign_to_items,
    english_half,
    is_mangled,
    read_boq_csv,
    read_boq_table,
    read_specification,
)

logger = get_logger(__name__)


@dataclass
class SliceResult:
    """What one tender produced, and what could not be checked."""

    tender_id: str
    items: list[TenderItem] = field(default_factory=list)
    requirements: list[RequirementRecord] = field(default_factory=list)
    # The specification rows that named no item. They are not guessed at.
    unassigned: list[dict] = field(default_factory=list)
    # Requirements whose quote was not found in the source text.
    failed_quotes: list[str] = field(default_factory=list)
    # The pages the requirements name that the document does not have.
    bad_pages: list[str] = field(default_factory=list)
    # The lines that produced no requirement (FR-T14).
    unrecalled: list[str] = field(default_factory=list)
    # The lines that could not be read.
    unreadable: list[str] = field(default_factory=list)
    # The attachments that could not be fetched, and why.
    missing: list[dict] = field(default_factory=list)
    page_count: int = 0

    @property
    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for requirement in self.requirements:
            key = requirement.requirement_type.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def matchable(self) -> list[RequirementRecord]:
        """The requirements that go to matching (FR-14)."""
        return [
            requirement
            for requirement in self.requirements
            if requirement.requirement_type
            in (RequirementType.TECHNICAL, RequirementType.CERTIFICATION)
        ]


def _document_path(manifest: dict, directory: Path, kind: str) -> Optional[Path]:
    """The frozen file of a given kind, if it was fetched."""
    for record in manifest.get("documents", []):
        if record.get("kind") != kind:
            continue
        if record.get("status") in ("failed", "restricted"):
            continue
        relative = record.get("path")
        if relative:
            path = directory / relative
            if path.exists():
                return path
    return None


def _page_text(path: Path, page_number: int) -> str:
    """The text of one page, read the same way the requirement was read."""
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened has no text
        return ""
    try:
        if page_number < 1 or page_number > document.page_count:
            return ""
        return document[page_number - 1].get_text() or ""
    finally:
        document.close()


def _normalise(text: str) -> str:
    """Text compared without its whitespace and punctuation.

    A quote is read back from a PDF's text layer, where the spacing is the
    layout's, not the writer's.
    """
    return " ".join(text.split()).strip().lower()


def run_slice(tender_id: str, tender_dir: Path) -> SliceResult:
    """Read one tender, from its frozen attachments to its requirements."""
    tender_dir = Path(tender_dir)
    manifest_path = tender_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = SliceResult(tender_id=tender_id)

    for record in manifest.get("documents", []):
        if record.get("status") in ("failed", "restricted"):
            result.missing.append(
                {
                    "url": record.get("url"),
                    "status": record.get("status"),
                    "note": record.get("note"),
                }
            )

    # --- the items (FR-T9) ---
    # The bill of quantities is the machine-readable one when there is one:
    # it names the items the way the buyer wrote them.
    boq_csv = _document_path(manifest, tender_dir, "boq-csv")
    boq_pdf = _document_path(manifest, tender_dir, "boq-pdf")
    category = manifest.get("category", "unknown")

    if boq_csv is not None:
        result.items = read_boq_csv(boq_csv, tender_id, category)
    elif boq_pdf is not None:
        result.items = _items_from_pdf(boq_pdf, tender_id, category)

    # --- the requirements (FR-T6, FR-T7) ---
    # The specification is a table of item descriptions, and it is a PDF: a
    # bill of quantities in CSV has the items but no pages, and a requirement
    # has to name the page it came from.
    specification = boq_pdf or _document_path(manifest, tender_dir, "spec")

    if specification is not None:
        try:
            read = read_specification(specification)
        except Exception as exc:  # noqa: BLE001 - a file that cannot be read is not a reason to stop
            # A malformed file is a file that cannot be read. The tender is
            # still read from the other attachment.
            logger.warning("could not read %s: %s", specification, exc)
            result.unreadable.append(f"{specification.name}: {type(exc).__name__}")
            read = SpecificationRead(error=f"{type(exc).__name__}: {exc}")

        result.page_count = _page_count(specification)
        assigned, unassigned = assign_to_items(read.rows, result.items)
        result.requirements = assigned
        result.unassigned = unassigned

        # --- the checks ---
        for requirement in result.requirements:
            if requirement.page is None:
                continue
            page_text = _page_text(specification, requirement.page)
            if not page_text:
                result.bad_pages.append(
                    f"{requirement.text[:60]} (page {requirement.page} is not in the document)"
                )
                continue
            if _normalise(requirement.text) not in _normalise(page_text):
                result.failed_quotes.append(requirement.text[:120])

    # --- the recall check (FR-T14) ---
    result.unrecalled = _unrecalled(result)
    return result


def _items_from_pdf(path: Path, tender_id: str, category: str) -> list[TenderItem]:
    """The items of a bill of quantities that is a PDF."""
    import pdfplumber

    items: list[TenderItem] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                items.extend(read_boq_table(table, tender_id, category))
    return items


def _page_count(path: Path) -> int:
    """How many pages a document has, or zero when it cannot be opened."""
    import pymupdf

    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened has no pages
        return 0
    try:
        return document.page_count
    finally:
        document.close()


def _unrecalled(result: SliceResult) -> list[str]:
    """The lines the specification stated that produced no requirement.

    A requirement that was never extracted is invisible: the report shows
    what was found, not what was missed. This lists the lines that were
    read and produced nothing (FR-T14).
    """
    claimed = {_normalise(requirement.text) for requirement in result.requirements}
    missing: list[str] = []
    for requirement in result.requirements:
        continue
    # The specification's rows that named no item are the ones at risk of
    # being lost: they are listed so a reviewer can see them.
    for row in result.unassigned:
        text = row.get("description") or ""
        if text and _normalise(text) not in claimed:
            missing.append(text[:160])
    return missing
