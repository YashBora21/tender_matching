"""Tender ingestion (FR-T1 to FR-T9, FR-T13, FR-14).

Reads a tender's bill of quantities into items, and its requirements into
records that carry the item they belong to.

Two rules shape everything here:

- **The English half of a line is the requirement.** The tenders are
  bilingual, and the Devanagari is sometimes mangled by the PDF's encoding.
  A line whose English cannot be read is an unreadable line, not a missing
  requirement, and it is flagged.
- **An item is a row of the bill of quantities.** The composite key is
  (tender, item): a requirement belongs to the item it was written under,
  and a requirement is only ever compared against models in that item's
  category (FR-T9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.schemas.enums import RequirementType

logger = get_logger(__name__)

# The Devanagari block, and the punctuation the PDF's encoding leaves behind.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# A character that is neither ASCII nor Devanagari: the mangled residue.
_MANGLED = re.compile(r"[^\x00-\x7Fऀ-ॿ‌‍°·•–—‘’“”₹€ ]")


def english_half(text: str) -> str:
    """The English of a bilingual line.

    A GeM line is "Hindi/English" or "Hindi English", so the English is
    what follows the last Devanagari run. A line that is only Devanagari
    has no English half, and the caller is told so rather than being given
    an empty string that looks like a line with no content.
    """
    if not text:
        return ""
    matches = list(_DEVANAGARI.finditer(text))
    if not matches:
        return text.strip()
    tail = text[matches[-1].end():].strip()
    # The separator the PDF leaves between the two halves.
    tail = tail.lstrip("/\\-–—:;,| ")
    return tail.strip()


def is_mangled(text: str) -> bool:
    """True when a line holds characters no font could have produced.

    These are the residue of a PDF whose encoding was not right. A line made
    of them is unreadable, and an unreadable line is not a missing
    requirement.
    """
    return bool(_MANGLED.search(text))


@dataclass
class TenderItem:
    """One row of the bill of quantities (FR-T9)."""

    item_id: str
    title: str
    description: str = ""
    quantity: Optional[str] = None
    unit: Optional[str] = None
    category: str = "unknown"
    # The row the item came from, for its provenance.
    source_row: Optional[str] = None


@dataclass
class RequirementRecord:
    """One requirement, with the item it belongs to."""

    text: str
    item_id: str
    requirement_type: RequirementType = RequirementType.TECHNICAL
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    mandatory: bool = True
    # The span in the source text, so the answer can be checked against it.
    source_span: Optional[tuple[int, int]] = None
    page: Optional[int] = None
    source_section: Optional[str] = None
    extraction_method: str = "table"
    # True when the line could not be read. It is flagged, never counted as
    # a requirement that was not stated.
    unreadable: bool = False
    review_required: bool = False
    review_reason: Optional[str] = None


@dataclass
class TenderIngestion:
    """What one tender produced."""

    tender_id: str
    items: list[TenderItem] = field(default_factory=list)
    requirements: list[RequirementRecord] = field(default_factory=list)
    # Pages that could not be read at all.
    failed_pages: list[int] = field(default_factory=list)
    # Pages read through OCR.
    ocr_pages: list[int] = field(default_factory=list)
    # Lines that could not be read. Counted, so a report can say how much of
    # the tender was legible.
    unreadable_lines: int = 0
    warnings: list[str] = field(default_factory=list)


def item_id_for(tender_id: str, number: str) -> str:
    """The composite key of an item: the tender and the item number."""
    return f"{tender_id}:{number}"


def _clean_cell(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())


def read_boq_csv(path: Path, tender_id: str, category: str = "unknown") -> list[TenderItem]:
    """The items of a bill of quantities, from its CSV.

    The columns are named by the buyer, so they are matched by what they
    hold rather than by their position: the item number, the title, the
    description, the quantity and the unit.
    """
    import csv

    items: list[TenderItem] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            number = _clean_cell(
                row.get("Item Number") or row.get("Item No") or row.get("item number")
            )
            if not number:
                continue
            items.append(
                TenderItem(
                    item_id=item_id_for(tender_id, number),
                    title=_clean_cell(
                        row.get("Item Title") or row.get("Item Description") or ""
                    ),
                    description=_clean_cell(row.get("Item Description") or ""),
                    quantity=_clean_cell(row.get("Item Quantity") or ""),
                    unit=_clean_cell(
                        row.get("Unit of Measure") or row.get("Unit") or ""
                    ),
                    category=category,
                    source_row=str(index),
                )
            )
    return items


def read_boq_table(
    rows: list[list[Optional[str]]],
    tender_id: str,
    category: str = "unknown",
) -> list[TenderItem]:
    """The items of a bill of quantities, from its table.

    The header is found by its words, because the buyers write it four
    different ways: "BOQ Item Title", "Brief Description", "Item Title",
    "Material Description".
    """
    if not rows:
        return []

    header = [(_clean_cell(cell) or "").lower() for cell in rows[0]]
    number_col = _find_column(
        header, ("item number", "item no", "sr. no", "sr no", "s.no", "s.n", "serial")
    )
    title_col = _find_column(
        header,
        (
            "item title",
            "brief description",
            "item description",
            "material description",
            "description",
        ),
    )
    quantity_col = _find_column(header, ("quantity", "qty", "quantiry"))
    unit_col = _find_column(header, ("unit",))

    items: list[TenderItem] = []
    for index, row in enumerate(rows[1:], start=1):
        number = _clean_cell(_cell(row, number_col))
        if not number or not number[0].isdigit():
            continue
        items.append(
            TenderItem(
                item_id=item_id_for(tender_id, number),
                title=_clean_cell(_cell(row, title_col)),
                description=_clean_cell(_cell(row, title_col)),
                quantity=_clean_cell(_cell(row, quantity_col)),
                unit=_clean_cell(_cell(row, unit_col)),
                category=category,
                source_row=str(index),
            )
        )
    return items


def _find_column(header: list[str], names: tuple[str, ...]) -> Optional[int]:
    """The column whose header says one of these things.

    The header is compared without its punctuation, because the buyers write
    "Sr. No.", "SR.NO" and "S.N" for the same column.
    """
    for index, cell in enumerate(header):
        squashed = re.sub(r"[^a-z0-9]", "", cell)
        for name in names:
            if re.sub(r"[^a-z0-9]", "", name) in squashed:
                return index
    return None


def _cell(row: list[Optional[str]], index: Optional[int]) -> Optional[str]:
    if index is None or index >= len(row):
        return None
    return row[index]
