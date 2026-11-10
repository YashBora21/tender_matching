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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.schemas.enums import ExtractionMethod, LedgerStatus, RequirementType
from app.services.tender.classify import (
    BOILERPLATE,
    NUMBER_WITH_UNIT,
    classify_page,
    is_non_technical,
)

logger = get_logger(__name__)

# The Devanagari block, and the punctuation the PDF's encoding leaves behind.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# A character that is neither ASCII nor Devanagari: the mangled residue.
_MANGLED = re.compile(r"[^\x00-\x7Fऀ-ॿ‌‍°·•–—‘’“”₹€ ]")


def english_half(text: str) -> str:
    """Retain every English/value run in interleaved bilingual source text."""
    if not text:
        return ""
    cleaned = re.sub(r"\(cid:\d+\)", " ", text)
    cleaned = re.sub(r"[\x00-\x1f]", " ", cleaned)
    parts = []
    for part in re.split(r"[\u0900-\u097F]+", cleaned):
        if not re.search(r"[A-Za-z0-9]", part):
            continue
        parts.append(part.strip().lstrip("/\\–—:;,| "))
    return " ".join(" ".join(parts).split())


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
# 3: a page is read as a table only when its grid is a real table, and prose
#    is joined into clauses before it is split (D-88).
READER_VERSION = "8"


@dataclass
class TenderItem:
    """One row of the bill of quantities (FR-T9)."""

    item_id: str
    title: str
    description: str = ""
    quantity: Optional[str] = None
    unit: Optional[str] = None
    # The category the item's own words name (FR-16). `unknown` when they
    # name none, or name two equally - which is flagged, never guessed
    # (FR-T9).
    category: str = "unknown"
    # The term that decided the category, and why it is unresolved when it is.
    category_term: Optional[str] = None
    category_ambiguous: bool = False
    category_reason: Optional[str] = None
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
    # The triage tag of the page the clause was read from (D-87). A clause
    # from a non-technical page is recorded, but never sent to the model.
    page_tag: Optional[str] = None
    # How sure the reading is. A requirement read from a text layer is
    # certain; one read from a scanned page is not (FR-9).
    confidence: Optional[float] = None
    # True when the line could not be read. It is flagged, never counted as
    # a requirement that was not stated.
    unreadable: bool = False
    review_required: bool = False
    review_reason: Optional[str] = None
    source_text: Optional[str] = None


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


def _clean_lines(value: Optional[str]) -> str:
    """A specification cell with the lines the buyer wrote kept apart.

    Inside a table cell a line break ends one parameter and starts the next,
    while in a prose page it is only a wrap (D-88). Blank lines and the spaces
    around each line go; the break itself stays, because it is the split.
    """
    if not value:
        return ""
    return "\n".join(
        line for line in (" ".join(raw.split()) for raw in str(value).splitlines()) if line
    )


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

    from app.services.tender.parse import ocr_page

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


# --- how a page is read -----------------------------------------------------

# A page whose text layer is shorter than this has no text layer worth
# reading: a header and a page number are not a page. It is OCRed instead.
MIN_TEXT_CHARS = 40

# A page that is mostly a picture has a text layer only for the letterhead
# around the scan. The words that matter are in the image, so the page is
# OCRed however much text the layer holds.
OCR_IMAGE_SHARE = 0.5

# A real table has at least two columns, and most of its rows are the same
# width. pdfplumber guesses at a grid for a page of prose, and the guess is
# one column wide or ragged - a guessed grid is not a table (FR-T1).
TABLE_MIN_COLUMNS = 2
TABLE_WIDTH_SHARE = 0.8

# How much of a table's grid must hold text. A ruled page of prose has
# borders and a nearly empty grid; a specification table is mostly filled.
# A table whose header was found is allowed to be sparser, because the header
# itself proves the grid is a table.
TABLE_FILL_WITH_HEADER = 0.3
TABLE_FILL_WITHOUT_HEADER = 0.6

# A page whose tables hold less than this share of its characters is read as
# prose: the table is a fragment of the page, not the page.
TABLE_TEXT_SHARE = 0.5

# A clause number at the start of a clause, or after the punctuation that
# ends the clause before it: 5.2, 24.3.1, 3.4.1.2.
_CLAUSE_START = re.compile(
    r"(?:(?<=[.;:])\s+|^)(?=\d{1,2}(?:\.\d{1,2}){1,4}[.)]?\s+[A-Z(])"
)

# A heading names a section. It is all capitals, short, and states no
# obligation or limit: "SPECIFICATION DETAILS FOR 75 HP PUMP & MOTOR" is a
# heading, "125 MM MINIMUM" is a requirement.
HEADING_MAX_WORDS = 12
# An obligation word makes a line a requirement, whatever its case.
_OBLIGATION_WORDS = re.compile(
    r"\b(?:shall|should|must|required|supply|provide)\b", re.IGNORECASE
)
# A limit word makes a line a requirement only when it also states a value:
# "125 MM MINIMUM" is a requirement, "CERTIFICATION REGARDING MINIMUM LOCAL
# CONTENT" is the title of one.
_LIMIT_WORDS = re.compile(
    r"\b(?:minimum|maximum|not\s+less|not\s+more|at\s+least|at\s+most|min|max)\b",
    re.IGNORECASE,
)

# The abbreviations that end with a full stop without ending a sentence.
_ABBREVIATION = re.compile(
    r"\b(?:gr|no|nos|sr|fig|dr|m/s|pvt|ltd|co|approx|ref|dt|viz|i\.e|e\.g)\.$",
    re.IGNORECASE,
)


@dataclass
class PageRead:
    """What became of one page (FR-T2)."""

    page: int
    tag: str
    status: str
    chars: int = 0
    method: Optional[str] = None
    error: Optional[str] = None
    skip_reason: Optional[str] = None


@dataclass
class SpecificationRead:
    """The clauses of a specification, and what happened to each page."""

    rows: list[dict] = field(default_factory=list)
    pages: list[PageRead] = field(default_factory=list)
    # Set when the document itself could not be opened: no page of it was
    # looked at, which is not the same as a document with nothing in it.
    error: Optional[str] = None


def read_specification(path: Path) -> SpecificationRead:
    """Read a specification page by page, and say what became of each page.

    A page is read one of three ways, and which one is decided by what the
    page holds, not by the document as a whole:

    - **table**: the page's ruled grid is a real table (several columns, rows
      of a consistent width, mostly filled) and the table holds most of the
      page's text. The description column holds the requirement.
    - **text**: anything else with a text layer. The wrapped lines are joined
      back into clauses and split on clause numbering and sentence
      boundaries. Prose is never split on line breaks, because a line break
      in a PDF is a wrap, not the end of a requirement.
    - **ocr**: the page has no text layer, so it is read as an image.

    The header is not always the first row: a page opens with the title
    ("BOQ", "Name of Supply") and the header is below it. And a table that
    runs over a page break is not repeated, so the columns found on one
    page are carried to the next (FR-T6).
    """
    import pdfplumber

    read = SpecificationRead()
    columns: Optional[dict] = None
    try:
        pdf = pdfplumber.open(path)
    except Exception as exc:  # noqa: BLE001 - the error is the answer
        read.error = f"{type(exc).__name__}: {exc}"
        return read

    scan = None
    try:
        with pdf:
            for number, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                method = ExtractionMethod.TEXT

                if _needs_ocr(page, text):
                    # No text layer worth reading, or the page is a picture
                    # with a letterhead typed over it.
                    if scan is None:
                        scan = _open_scan(path)
                    ocr_text, ocr_error = _ocr(scan, number)
                    if ocr_text.strip():
                        # The page needed OCR, so what the OCR read is the
                        # page: the text layer, when there was one, was the
                        # letterhead around the picture.
                        text, method = ocr_text, ExtractionMethod.OCR
                    elif not text.strip():
                        # Nothing was read on this page, and it is not a
                        # page that states nothing: it is a failed page.
                        read.pages.append(
                            PageRead(
                                page=number,
                                tag=BOILERPLATE,
                                status=LedgerStatus.FAILED.value,
                                error=ocr_error or "the page could not be read",
                            )
                        )
                        continue

                tag = classify_page(text).kind

                if method is ExtractionMethod.OCR:
                    # An OCRed page has no grid to trust: what the scanner
                    # saw as columns is the scanner's guess.
                    rows = _prose_rows(
                        text, number, tag, method=ExtractionMethod.OCR.value
                    )
                    read.rows.extend(rows)
                    read.pages.append(
                        PageRead(
                            page=number,
                            tag=tag,
                            status=LedgerStatus.OCR.value,
                            chars=len(text),
                            method=ExtractionMethod.OCR.value,
                        )
                    )
                    continue

                table_rows, columns = _read_tables(
                    page, text, number, tag, columns
                )
                if table_rows is not None:
                    read.rows.extend(table_rows)
                    read.pages.append(
                        PageRead(
                            page=number,
                            tag=tag,
                            status=LedgerStatus.PARSED.value,
                            chars=len(text),
                            method=ExtractionMethod.TABLE.value,
                        )
                    )
                    continue

                rows = _prose_rows(text, number, tag)
                read.rows.extend(rows)
                read.pages.append(
                    PageRead(
                        page=number,
                        tag=tag,
                        status=(
                            LedgerStatus.PARSED.value
                            if rows
                            else LedgerStatus.SKIPPED.value
                        ),
                        chars=len(text),
                        method=ExtractionMethod.TEXT.value,
                        skip_reason=(
                            None if rows else "the page states no readable clause"
                        ),
                    )
                )
    finally:
        if scan is not None:
            scan.close()
    return read


def _open_scan(path: Path):
    """The document opened for OCR, or None when it cannot be opened."""
    import pymupdf

    try:
        return pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened has no text
        return None


def _needs_ocr(page, text: str) -> bool:
    """True when the page's words are in an image rather than the text layer.

    A scan often carries a typed letterhead over a picture of the page, so
    the text layer is present and says nothing: the file number and the page
    number, and the specification itself in the image. The area the pictures
    cover is what tells the two apart.
    """
    if len(" ".join(text.split())) < MIN_TEXT_CHARS:
        return True
    return _image_share(page) >= OCR_IMAGE_SHARE


def _image_share(page) -> float:
    """The share of the page's area covered by pictures."""
    try:
        area = float(page.width) * float(page.height)
        covered = sum(
            (
                max(0.0, float(image["x1"]) - float(image["x0"]))
                * max(0.0, float(image["bottom"]) - float(image["top"]))
            )
            for image in page.images
        )
    except Exception:  # noqa: BLE001 - a page whose pictures cannot be measured
        return 0.0
    return covered / area if area else 0.0


def _ocr(document, number: int) -> tuple[str, Optional[str]]:
    """The OCRed text of one page, and why it is empty when it is."""
    from app.services.tender.parse import ocr_page_with_reason

    if document is None:
        return "", "the document could not be opened for OCR"
    if number > len(document):
        return "", f"page {number} is not in the document"
    try:
        return ocr_page_with_reason(document[number - 1])
    except Exception as exc:  # noqa: BLE001 - the failure is the page's state
        return "", f"{type(exc).__name__}: {exc}"


def _is_real_table(
    table: list[list[Optional[str]]], *, carried: bool = False
) -> bool:
    """True when a grid pdfplumber found is a table and not a guess.

    A page of prose has no ruled cells, but pdfplumber still guesses at a
    grid for it: one column wide, or rows of whatever width the guess gave
    each line. Reading those guesses as rows is what turned legal paragraphs
    into requirements. A real table has several columns, rows that agree on
    their width, and text in most of its cells.

    A table that continues from the previous page has no header of its own,
    so its grid is proven by the header the caller is carrying.
    """
    rows = [[_clean_cell(cell) for cell in row] for row in table if row]
    if len(rows) < 2:
        return False

    widths = Counter(len(row) for row in rows)
    width, count = widths.most_common(1)[0]
    if width < TABLE_MIN_COLUMNS:
        return False
    if count / len(rows) < TABLE_WIDTH_SHARE:
        return False

    _, found = _find_header_row(table)
    filled = sum(1 for row in rows for cell in row if cell)
    cells = sum(len(row) for row in rows)
    if not cells:
        return False
    floor = (
        TABLE_FILL_WITH_HEADER if (found or carried) else TABLE_FILL_WITHOUT_HEADER
    )
    return filled / cells >= floor


def _read_tables(
    page,
    text: str,
    page_number: int,
    tag: str,
    columns: Optional[dict],
) -> tuple[Optional[list[dict]], Optional[dict]]:
    """The rows of the real tables on a page, or None when it is prose.

    The columns are returned as well as the rows, because a table that runs
    over a page break has no header of its own and the next page needs them.
    """
    accepted: list[tuple[list[list[Optional[str]]], int, Optional[dict]]] = []
    found_columns = columns
    for table in page.extract_tables() or []:
        if not table:
            continue
        if not _is_real_table(table, carried=found_columns is not None):
            continue
        header_row, found = _find_header_row(table)
        if found:
            found_columns = found
        accepted.append((table, header_row, found))

    if not accepted or found_columns is None:
        return None, columns
    if (
        found_columns.get("description") is None
        and found_columns.get("title") is None
    ):
        return None, columns

    # A page with a table and a lot of prose is a prose page: the table is a
    # fragment of it. Counting the characters is how the two are told apart.
    held = sum(
        len(_clean_cell(cell))
        for table, _, _ in accepted
        for row in table
        for cell in row
    )
    page_chars = len(" ".join(text.split()))
    if page_chars and held < TABLE_TEXT_SHARE * page_chars:
        return None, columns

    rows: list[dict] = []
    for table, header_row, found in accepted:
        start = header_row + 1 if found else 0
        for index, row in enumerate(table[start:], start=start):
            description = _clean_lines(_cell(row, found_columns.get("description")))
            if not description:
                # Some buyers name the row in the item column and describe it
                # in the description column; a row with only the first is
                # still a requirement.
                description = _clean_lines(_cell(row, found_columns.get("title")))
            if not description:
                continue
            number = _clean_cell(_cell(row, found_columns.get("number")))
            title = _clean_cell(_cell(row, found_columns.get("title")))
            quantity = _clean_cell(_cell(row, found_columns.get("quantity")))
            unit = _clean_cell(_cell(row, found_columns.get("unit")))
            row_text = " ".join(
                _clean_cell(cell) for cell in row if _clean_cell(cell)
            )
            # A specification cell states one parameter per line, and the buyer
            # wrote it that way. Flattening the cell joined those parameters into
            # one clause no parser can read: the gold clause "Three piece ball
            # valves 2" Screwed end ASME class 150 Cast carbon steel Face to face
            # distance 122mm" is four, and as one it parsed no attribute at all
            # (D-146). Each line is a row of its own, with the row's own
            # provenance - page, item number and section are unchanged.
            for line in description.split("\n"):
                rows.append(
                    {
                        "row": index,
                        "page": page_number,
                        "number": number,
                        "title": title,
                        "description": line,
                        "quantity": quantity,
                        "unit": unit,
                        "row_text": row_text,
                        "extraction_method": ExtractionMethod.TABLE.value,
                        "page_tag": tag,
                    }
                )
    return rows, found_columns


def _prose_rows(
    text: str,
    page_number: int,
    tag: str,
    method: str = ExtractionMethod.TEXT.value,
) -> list[dict]:
    """The clauses of a page of prose, one row each."""
    return [
        {
            "row": index,
            "page": page_number,
            "number": "",
            "title": section or "",
            "description": clause,
            "quantity": "",
            "unit": "",
            "row_text": clause,
            "extraction_method": method,
            "page_tag": tag,
        }
        for index, (section, clause) in enumerate(_prose_clauses(text))
    ]


def _prose_clauses(text: str) -> list[tuple[Optional[str], str]]:
    """The clauses of a page, and the heading each sits under.

    A line break in a PDF is a wrap, not the end of a clause: the lines that
    do not end a sentence are joined back together before anything is split.
    An all-capitals line that states no limit is a heading, and it is the
    section the clauses below it belong to - not a requirement of its own.
    """
    found: list[tuple[Optional[str], str]] = []
    section: Optional[str] = None
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        paragraph = buffer.strip()
        buffer = ""
        if not paragraph:
            return
        for clause in _split_clauses(paragraph):
            found.append((section, clause))

    for raw in text.split("\n"):
        line = " ".join(raw.split())
        if not line:
            flush()
            continue
        if _is_heading(line):
            flush()
            section = line
            continue
        if not buffer:
            buffer = line
        elif buffer.endswith((".", "!", "?", ":")):
            flush()
            buffer = line
        else:
            buffer = f"{buffer} {line}"
    flush()
    return found


def _is_heading(line: str) -> bool:
    """True when a line names a section rather than stating a requirement."""
    words = line.split()
    if not words or len(words) > HEADING_MAX_WORDS:
        return False
    if line.endswith((".", ";", ":", ",")):
        return False
    letters = [character for character in line if character.isalpha()]
    if not letters or not all(character.isupper() for character in letters):
        return False
    if _OBLIGATION_WORDS.search(line):
        return False
    if _LIMIT_WORDS.search(line) and NUMBER_WITH_UNIT.search(line):
        return False
    # A short line that is only a value ("125 MM") is a requirement; a
    # heading names something ("... 75 HP PUMP & MOTOR").
    if len(words) <= 3 and NUMBER_WITH_UNIT.search(line):
        return False
    return True


def _split_clauses(paragraph: str) -> list[str]:
    """Split a paragraph into clauses on clause numbers and sentences."""
    clauses: list[str] = []
    for piece in _CLAUSE_START.split(paragraph):
        piece = piece.strip()
        if not piece:
            continue
        clauses.extend(_split_sentences(piece))
    return clauses


def _split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences, keeping abbreviations together."""
    sentences: list[str] = []
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+", paragraph):
        sentence = paragraph[start:match.start()]
        following = paragraph[match.end():]
        if not following:
            break
        if not (
            following[0].isupper()
            or following[0].isdigit()
            or following[0] in "(\"'"
        ):
            continue
        if _ABBREVIATION.search(sentence):
            continue
        sentences.append(sentence.strip())
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        sentences.append(tail)
    return [sentence for sentence in sentences if sentence]


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

    The one assignment that is not a guess is a tender with a single item: a
    prose specification has no item column, and every clause it states is a
    clause of that item.
    """
    by_number = {item.item_id.rsplit(":", 1)[-1]: item for item in items}
    by_title = {_title_key(item.title): item for item in items if item.title}
    only_item = items[0] if len(items) == 1 else None

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
        if item is None and only_item is not None:
            item = only_item
        if item is None:
            unassigned.append(row)
            continue

        tag = row.get("page_tag")
        classified = is_non_technical(tag) if tag else False
        assigned.append(
            RequirementRecord(
                text=row["description"],
                item_id=item.item_id,
                # A clause on a page that is not a specification is recorded,
                # but it is not a capability a document could state (D-87).
                requirement_type=(
                    RequirementType.UNKNOWN if classified else RequirementType.TECHNICAL
                ),
                extraction_method=row.get("extraction_method") or "table",
                page=row.get("page"),
                source_section=(
                    (row.get("title") or "").strip() or item.title or None
                ),
                page_tag=tag,
                review_required=classified,
                review_reason=f"page classified as {tag}" if classified else None,
            )
        )
    return assigned, unassigned


def _title_key(text: str) -> str:
    """A title reduced to the words that identify it.

    "GATE VALVE SIZE 150 MM CLASS 150" and "Gate Valve Size 150mm Class 150"
    are the same item, and the sizes and classes are what say so.
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


# --- fragments --------------------------------------------------------------

# A clause shorter than this that names no unit and no specification word is
# a fragment of a line, not a requirement.
FRAGMENT_MIN_CHARS = 25

# A clause with more than this share of characters that are neither letters
# nor ordinary punctuation is OCR noise, not a clause.
FRAGMENT_JUNK_SHARE = 0.35

# A clause that is only a number: "5.2", "24.13.10", "3."
_PURE_NUMBER = re.compile(r"^\d{1,3}(?:\.\d{1,3}){0,4}[.)]?$")

# The characters a clause may hold. Anything else is what a bad scan leaves
# behind: "|", "_", "[", "]" and the rest of the OCR residue.
_JUNK = re.compile(r"[^0-9A-Za-z\u0900-\u097F\s.,;:/%\-()\"'&+=°*]")

# A run of two such characters is never a word: it is the residue of a table
# border or a broken glyph, however many letters surround it.
_JUNK_RUN = re.compile(r"[^0-9A-Za-z\u0900-\u097F\s.,;:/%\-()\"'&+=°*]{2,}")

# The words that name something a specification states. A short clause that
# names one of these is worth keeping; a short clause that names none is not.
SPEC_WORDS = re.compile(
    r"\b(?:size|diameter|bore|head|flow|capacity|discharge|pressure|rating|"
    r"class|material|thickness|efficiency|power|speed|voltage|frequency|"
    r"temperature|weight|length|width|height|grade|standard|tolerance|test|"
    r"type|design|connection|flange|flanged|screwed|welded|body|bonnet|seat|"
    r"stem|wedge|disc|impeller|casing|shaft|bearing|seal|ends|surface)\b",
    re.IGNORECASE,
)

# The verbs a requirement is written with. A clause with no verb and no value
# states nothing a document could be compared against.
VERB_WORDS = re.compile(
    r"\b(?:shall|should|must|is|are|be|been|has|have|had|will|can|may|"
    r"provide|provided|supply|supplied|fit|fitted|make|made|use|used|"
    r"test|tested|design|designed|give|given|include|included|ensure|"
    r"maintain|operate|comply|conform|meet|require|required|accept|allow|"
    r"permit|work|works|mounted|connected|suitable|capable)\b",
    re.IGNORECASE,
)


@dataclass
class FragmentReport:
    """How many clauses were dropped, and how many were joined to one before."""

    dropped: int = 0
    merged: int = 0


def _junk_share(text: str) -> float:
    """The share of a clause's characters that no document would print."""
    characters = [character for character in text if not character.isspace()]
    if not characters:
        return 1.0
    junk = sum(1 for character in characters if _JUNK.match(character))
    return junk / len(characters)


def is_fragment(text: str) -> bool:
    """True when a clause is a piece of a line rather than a requirement.

    A fragment is a clause number on its own ("5.2"), a line with more OCR
    residue than words, a line too short to state anything that names no
    unit and no specification word, a line with no verb and no value, or a
    line that starts in lowercase - which is the tail of a clause whose
    beginning was lost at a page break or a table cell. A lowercase line that
    states a number with a unit is the exception: it is a clause of its own.
    """
    clause = " ".join((text or "").split())
    if not clause:
        return True
    if _PURE_NUMBER.match(clause):
        return True
    if _JUNK_RUN.search(clause) or _junk_share(clause) > FRAGMENT_JUNK_SHARE:
        return True
    # A table cell often starts in lowercase ("minimum efficiency 85%"). A
    # clause that states a number with a unit is self-contained evidence, not
    # the tail of a clause whose beginning was lost, so it is kept.
    if clause[0].islower() and not NUMBER_WITH_UNIT.search(clause):
        return True
    if (
        len(clause) < FRAGMENT_MIN_CHARS
        and not NUMBER_WITH_UNIT.search(clause)
        and not SPEC_WORDS.search(clause)
    ):
        return True
    if (
        not re.search(r"\d", clause)
        and not VERB_WORDS.search(clause)
        and not SPEC_WORDS.search(clause)
    ):
        return True
    return False


def _continues(previous: str, fragment: str) -> bool:
    """True when a fragment is the rest of the clause before it.

    Both signals must agree: the fragment starts in lowercase, and the clause
    before it does not end a sentence. Joining a lowercase tail onto a
    finished sentence would corrupt the sentence, which is what happened when
    a fragment was merged into whatever line came before it.
    """
    text = (fragment or "").strip()
    if not text or not text[0].islower():
        return False
    return not previous.rstrip().endswith((".", "!", "?", ";", ":"))


# How a clause was read, for the rows that come out of a grid rather than out
# of a paragraph: a table cell and a bill-of-quantities row.
_CELL_METHODS = (ExtractionMethod.TABLE.value, ExtractionMethod.ITEM.value)


def drop_fragments(
    requirements: list[RequirementRecord],
) -> tuple[list[RequirementRecord], FragmentReport]:
    """Drop the clauses that are not requirements, joining the ones that are.

    A fragment is never guessed at: it is either the rest of the clause
    before it, in which case it is joined to it, or it is dropped and
    counted. The count is what a report needs to say how much of a tender was
    noise instead of requirements. A row of a table or of the bill of
    quantities is never dropped - its lines are the parameters (D-146).
    """
    kept: list[RequirementRecord] = []
    report = FragmentReport()

    for requirement in requirements:
        if requirement.unreadable or is_mangled(requirement.text):
            # A line that cannot be read is a gap, not a fragment: dropping
            # it would turn "I could not read this" into "the tender did not
            # say it".
            kept.append(requirement)
            continue
        if not is_fragment(requirement.text):
            kept.append(requirement)
            continue
        previous = kept[-1] if kept else None
        if (
            previous is not None
            and previous.item_id == requirement.item_id
            and previous.page == requirement.page
            and _continues(previous.text, requirement.text)
        ):
            previous.text = f"{previous.text.rstrip()} {requirement.text.strip()}"
            # The span pointed into the old text, which no longer exists.
            previous.source_span = None
            report.merged += 1
            continue
        if requirement.extraction_method in _CELL_METHODS:
            # A line of a table cell or a bill-of-quantities row is a parameter
            # the buyer wrote, not a wrap of the line above. "Cast carbon steel"
            # states a material and no number, so the prose rules call it a
            # fragment - and dropping it loses a mandatory requirement the report
            # would then never mention (D-146).
            kept.append(requirement)
            continue
        report.dropped += 1
    return kept, report

# The wording that makes an item a clause. A bill of quantities usually names
# a thing ("Solenoid valve, 4 pin connector"); a buyer who writes the
# obligation into the row ("... shall have LED indication") has stated a
# requirement there instead, and the row is then the requirement's item.
_OBLIGATION = re.compile(r"\b(?:shall|should|must)\b", re.IGNORECASE)


def item_states_a_requirement(item: TenderItem) -> bool:
    """True when the item's own words state an obligation, not a name."""
    return bool(_OBLIGATION.search(item.title or ""))


def requirements_from_items(items: list[TenderItem]) -> list[RequirementRecord]:
    """The clauses a bill of quantities states as item descriptions.

    The item is kept as the parent: the requirement points at it, so the
    comparison is scoped to the item's own category (FR-16) and the report can
    say which item the clause sits under. Nothing is guessed from the wording -
    only the rows that state an obligation become a clause, and the rest stay
    what they are, item names.
    """
    return [
        RequirementRecord(
            text=item.title.strip(),
            item_id=item.item_id,
            requirement_type=RequirementType.UNKNOWN,
            extraction_method="item",
            source_section=item.title.strip() or None,
        )
        for item in items
        if item_states_a_requirement(item)
    ]
