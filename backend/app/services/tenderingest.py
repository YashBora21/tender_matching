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


# The version of the readers below. A change to how a bill of quantities or
# a specification is read is a change to what the stage produces, so it is
# part of the stage's key: the stage runs again, and only that stage.
READER_VERSION = "2"


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
    # How sure the reading is. A requirement read from a text layer is
    # certain; one read from a scanned page is not (FR-9).
    confidence: Optional[float] = None
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
    """The key of an item within its tender.

    The tender is a separate column, so the item is identified by its number
    alone: two tenders both have an item "1", and the pair is what tells them
    apart (FR-T9).
    """
    return number


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


def read_boq_items(path: Path, tender_id: str, category: str = "unknown") -> list[TenderItem]:
    """The items of a bill of quantities, whatever file it is.

    A bill of quantities is a CSV, or a PDF with a table, or a scanned PDF
    whose table is an image. The first is read as a CSV, the second as a
    table, and the third through OCR, so a tender is not skipped because
    the buyer scanned it (FR-9).
    """
    if path.suffix.lower() == ".csv":
        return read_boq_csv(path, tender_id, category)

    import pdfplumber

    items: list[TenderItem] = []
    columns: Optional[dict] = None
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    header_row, found = _find_header_row(table)
                    if found:
                        columns = found
                    if columns is None:
                        # No header yet: the rows cannot be read.
                        continue
                    start = header_row + 1 if found else 0
                    items.extend(
                        read_boq_table(
                            table[start:],
                            tender_id,
                            category,
                            columns=columns,
                        )
                    )
    except Exception:  # noqa: BLE001 - a file that cannot be read gives no items
        items = []

    if not items:
        # No table was found. The document may be scanned, and a scanned
        # page is read through OCR.
        items = _read_scanned_items(path, tender_id, category)
    return items


def _read_scanned_items(
    path: Path, tender_id: str, category: str = "unknown"
) -> list[TenderItem]:
    """The items of a scanned bill of quantities.

    A scanned bill of quantities is a list of items, one under the other.
    The number of each is read from the line that names it, and the
    description from the lines that follow.
    """
    import pymupdf

    from app.services.docparser import ocr_page

    items: list[TenderItem] = []
    seen: set[str] = set()
    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be read has no items
        return items
    try:
        for page in document:
            text = ocr_page(page)
            if not text:
                continue
            for number, title, description in _scanned_item_lines(text):
                item_id = item_id_for(tender_id, number)
                if item_id in seen:
                    # The same number twice: a scanner reads a two-column
                    # table as two columns of text, so a number can be seen
                    # again with a different title beside it.
                    continue
                seen.add(item_id)
                items.append(
                    TenderItem(
                        item_id=item_id,
                        title=title,
                        description=description,
                        category=category,
                        source_row=number,
                    )
                )
    finally:
        document.close()
    return items


# A line that names an item: a number, then words.
_ITEM_LINE = re.compile(r"^\s*(\d{1,3})[.)]\s*(.{4,})$")
# A line that is only a number: the item number on its own, and the item
# named on the line below. A scanner reads a two-column table as two columns
# of text, so the number and the words arrive separately.
_NUMBER_LINE = re.compile(r"^\s*(\d{1,3})[.)]\s*$")


def _scanned_item_lines(text: str) -> list[tuple[str, str, str]]:
    """The items on an OCRed page, with the lines that describe each.

    A scanned bill of quantities names an item and then describes it. The
    number names the item and the description is what the lines below it say.
    The number and the words arrive on separate lines when the table was
    read as two columns, and on the same line when it was not.

    A line that states no specification is not an item: a scanned page
    carries the buyer's letterhead, the signature block and the footer as
    well as the requirements, and those are numbered too.
    """
    lines = [" ".join(line.split()) for line in text.split("\n")]
    lines = [line for line in lines if line]

    found: list[tuple[str, str, str]] = []
    current: Optional[tuple[str, str]] = None
    pending_number: Optional[str] = None
    for line in lines:
        match = _ITEM_LINE.match(line)
        if match:
            if current is not None and _states_a_specification(current[1]):
                found.append((current[0], current[1], ""))
            current = (match.group(1), match.group(2))
            pending_number = None
            continue
        number_only = _NUMBER_LINE.match(line)
        if number_only:
            if current is not None and _states_a_specification(current[1]):
                found.append((current[0], current[1], ""))
                current = None
            pending_number = number_only.group(1)
            continue
        if pending_number is not None:
            # The number was on its own line and this is the item it names.
            current = (pending_number, line)
            pending_number = None
            continue
        if current is not None:
            number, title = current
            found.append((number, title, line))
            current = None
    if current is not None and _states_a_specification(current[1]):
        found.append((current[0], current[1], ""))
    return found


# The words that say a line states a specification. A requirement states a
# value, a limit or an obligation; a letterhead states neither.
_SPECIFICATION_WORDS = re.compile(
    r"\b(?:shall|should|must|required|not\s+less|not\s+more|not\s+exceed|"
    r"at\s+least|at\s+most|min(?:imum)?|max(?:imum)?|rated|tolerance|"
    r"mm|cm|mtr|meter|metre|bar|kg|rpm|volt|amp|kw|%|degree|"
    r"no\.?\s|nos\.?|range|with|material|construction|size|capacity|"
    r"efficiency|head|discharge|pressure|voltage|frequency|flow)\b",
    re.IGNORECASE,
)


def _states_a_specification(text: str) -> bool:
    """True when a line states something that can be compared.

    A scanned page carries the buyer's letterhead and the signature block
    as well as the requirements, and the letterhead is numbered too. A line
    that names no value and no obligation is not an item.
    """
    return bool(_SPECIFICATION_WORDS.search(text))


def read_boq_table(
    rows: list[list[Optional[str]]],
    tender_id: str,
    category: str = "unknown",
    columns: Optional[dict] = None,
) -> list[TenderItem]:
    """The items of a bill of quantities, from its table.

    The columns come from the header, which the caller may have found
    already: a table that runs over a page break does not repeat its header,
    so the columns are carried to the next page.
    """
    if not rows:
        return []

    if columns is None:
        # The header is in the rows: find it and read the rows below it.
        header_row, found = _find_header_row(rows)
        if found is None:
            return []
        columns = found
        rows = rows[header_row + 1:]

    items: list[TenderItem] = []
    for index, row in enumerate(rows, start=1):
        number = _clean_cell(_cell(row, columns.get("number")))
        if not number or not number[0].isdigit():
            continue
        title = _clean_cell(_cell(row, columns.get("title")))
        items.append(
            TenderItem(
                item_id=item_id_for(tender_id, number),
                title=title,
                description=_clean_cell(_cell(row, columns.get("description"))) or title,
                quantity=_clean_cell(_cell(row, columns.get("quantity"))),
                unit=_clean_cell(_cell(row, columns.get("unit"))),
                category=category,
                source_row=str(index),
            )
        )
    return items


# The words a column's header may say, by the role the column plays. The
# buyers write the same column several ways, so the header is matched on the
# words it uses, not on its position.
_COLUMN_WORDS: dict[str, tuple[str, ...]] = {
    "number": ("itemnumber", "itemno", "srno", "sno", "serial", "snonumber"),
    "title": ("itemtitle", "itemname", "item", "briefdescription", "descriptionofitem"),
    "description": (
        "itemdescription",
        "itemdescriptions",
        "descriptions",
        "particulars",
        "description",
    ),
    "quantity": ("quantity", "qty", "quantiry", "quantite"),
    "unit": ("unit", "uom", "unitofmeasure"),
}


def find_columns(header: list[str]) -> dict[str, Optional[int]]:
    """The column that plays each role, from the header's words.

    A column that is named exactly wins over one that merely contains the
    word: a table with "Item" and "Descriptions" has an item column and a
    description column, and "description" must not be taken for the item.
    """
    squashed = [re.sub(r"[^a-z0-9]", "", cell) for cell in header]
    found: dict[str, Optional[int]] = {}

    for role, words in _COLUMN_WORDS.items():
        chosen: Optional[int] = None
        for word in words:
            for index, cell in enumerate(squashed):
                if cell == word:
                    chosen = index
                    break
            if chosen is not None:
                break
        if chosen is None:
            for word in words:
                for index, cell in enumerate(squashed):
                    if word in cell:
                        chosen = index
                        break
                if chosen is not None:
                    break
        found[role] = chosen
    return found


def _cell(row: list[Optional[str]], index: Optional[int]) -> Optional[str]:
    if index is None or index >= len(row):
        return None
    return row[index]


def read_specification(path: Path) -> list[dict]:
    """The specification's rows, with the page each is on.

    A specification is a table: "Sr. No. | Item | Descriptions | Qty | Unit".
    The description column holds the requirement, and the row is what it
    belongs to.

    The header is not always the first row: a page opens with the title
    ("BOQ", "Name of Supply") and the header is below it. And a table that
    runs over a page break is not repeated, so the columns found on one
    page are carried to the next (FR-T6).

    A scanned specification has no table to find. Its pages are read through
    OCR and the rows are taken from the text, so a scanned document is read
    like any other (FR-9).
    """
    import pdfplumber

    rows: list[dict] = []
    columns: Optional[dict] = None
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables() or []:
                if not table:
                    continue
                header_row, found = _find_header_row(table)
                if found:
                    columns = found
                if columns is None:
                    # No header yet: the rows cannot be read as a table.
                    continue
                start = header_row + 1 if found else 0
                for index, row in enumerate(table[start:], start=start):
                    description = _clean_cell(_cell(row, columns.get("description")))
                    if not description:
                        continue
                    rows.append(
                        {
                            "row": index,
                            "page": page_number,
                            "number": _clean_cell(_cell(row, columns.get("number"))),
                            "title": _clean_cell(_cell(row, columns.get("title"))),
                            "description": description,
                            "quantity": _clean_cell(_cell(row, columns.get("quantity"))),
                            "unit": _clean_cell(_cell(row, columns.get("unit"))),
                            "row_text": " ".join(
                                _clean_cell(cell) for cell in row if _clean_cell(cell)
                            ),
                        }
                    )

    if not rows:
        # No table was found. The document may be scanned, and a scanned
        # page is read through OCR (FR-9).
        rows = _read_scanned_specification(path)
    return rows


def _read_scanned_specification(path: Path) -> list[dict]:
    """The rows of a specification whose pages are images.

    A scanned page has no table to find, so the pages are read through OCR
    and the rows are taken from the text.
    """
    import pymupdf

    from app.services.docparser import ocr_page

    rows: list[dict] = []
    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be read has no rows
        return rows
    try:
        for page_number, page in enumerate(document, start=1):
            text = ocr_page(page)
            if not text:
                continue
            for index, line in enumerate(_ocr_lines(text)):
                rows.append(
                    {
                        "row": index,
                        "page": page_number,
                        "number": "",
                        "title": "",
                        "description": line,
                        "quantity": "",
                        "unit": "",
                        "row_text": line,
                    }
                )
    finally:
        document.close()
    return rows


def _ocr_lines(text: str) -> list[str]:
    """The readable lines of an OCRed page.

    The lines of a scanned specification are short: a title, a value, a note.
    The ones worth keeping are the ones that carry a specification.
    """
    lines = []
    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if len(line) < 8:
            continue
        if not re.search(r"\d", line):
            continue
        lines.append(line)
    return lines


def _find_header_row(table: list[list[Optional[str]]]) -> tuple[int, Optional[dict]]:
    """The row of a table that is its header, and the columns it names.

    A table that opens with a title has its header further down, and a table
    that continues from the previous page has none at all - which is why the
    caller carries the columns over.
    """
    for index, row in enumerate(table[:5]):
        columns = find_columns([(_clean_cell(cell) or "").lower() for cell in row])
        # A table is a bill of quantities when it names the items. A table
        # with only a description column is one too: some buyers write the
        # description and the number and nothing else.
        if columns.get("description") is None and columns.get("title") is None:
            continue
        return index, columns
    return -1, None


def assign_to_items(
    rows: list[dict],
    items: list[TenderItem],
) -> tuple[list[RequirementRecord], list[dict]]:
    """Attach a specification's requirements to the bill of quantities.

    A requirement belongs to the item it names. The item is found by the
    number the row carries; when the row has no number, the description is
    matched against the item titles. A requirement that matches no item is
    **not** guessed at: it is returned separately, so the ambiguity is
    visible (FR-T9).
    """
    by_number = {item.item_id.rsplit(":", 1)[-1]: item for item in items}
    by_title = {_title_key(item.title): item for item in items if item.title}

    assigned: list[RequirementRecord] = []
    unassigned: list[dict] = []

    for row in rows:
        number = (row.get("number") or "").strip()
        item = by_number.get(number)
        if item is None:
            # The specification names the item in its own words, and the bill
            # of quantities names it in the buyer's words. The two are the
            # same item when they say the same thing.
            item = by_title.get(_title_key(row.get("title") or ""))
        if item is None:
            item = by_title.get(_title_key(row.get("description") or ""))
        if item is None:
            unassigned.append(row)
            continue
        assigned.append(
            RequirementRecord(
                text=row["description"],
                item_id=item.item_id,
                extraction_method="table",
                page=row.get("page"),
                source_section=item.title or None,
            )
        )
    return assigned, unassigned


def _title_key(text: str) -> str:
    """A title reduced to the words that identify it.

    "GATE VALVE SIZE 150 MM CLASS 150" and "Gate Valve Size 150mm Class 150"
    are the same item, and the sizes and classes are what say so.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())
