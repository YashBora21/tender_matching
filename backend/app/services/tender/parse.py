"""Document parsing (FR-8, FR-9, FR-10).

Turns a manufacturer document into chunks: a piece of text with the page it
came from, the section it sits under, and whether it is a table.

- FR-8: PDF and DOCX, with page numbers preserved and tables kept as markdown.
- FR-9: a page with no extractable text falls back to OCR and the chunk is
  tagged `ocr=True`, with a lower confidence.
- FR-10: every chunk carries its page, section heading and table flag. The
  document-level metadata (manufacturer, model, version) travels with the
  document, not with each chunk.

PDF is read with PyMuPDF for the text and for a fast table *gate*, and with
pdfplumber for the tables themselves: pdfplumber reads a ruled table's cells
far more reliably, and PyMuPDF's own table extraction drops the spaces inside
a cell ("Maxoperatingpressure"). pdfplumber is slow on a page that has no
table - 26 seconds on one brochure page, hunting for lines that are not
there - so PyMuPDF's finder is asked first, and pdfplumber only runs on the
pages that actually hold a table.
"""

from __future__ import annotations

import gc
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from app.comparison import attributes as attrs
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.match.evidence import load_doc_types
from app.services.textnorm import strip_control

logger = get_logger(__name__)

# A heading is a short line that is numbered or styled as a heading, and that
# is not itself a requirement: it introduces what follows.
_HEADING = re.compile(r"^\s*[\d.]+[A-Za-z]?\s+[A-Za-z][A-Za-z /&()\-]{0,60}\s*:?\s*$")

# Confidence of a chunk read from a page's text layer.
TEXT_CONFIDENCE = 1.0

# The longest a heading can be. A heading is a line, not a sentence.
_MAX_HEADING_CHARS = 80


def _min_chars_for_text() -> int:
    """Below this many characters a page has no usable text layer (FR-9)."""
    return get_settings().ingest_min_chars_for_text


def _ocr_confidence() -> float:
    """The confidence of a chunk read through OCR."""
    return get_settings().ingest_ocr_confidence


@dataclass
class Chunk:
    """One indexed piece of a document (FR-10)."""

    text: str
    page: int
    section: Optional[str] = None
    is_table: bool = False
    ocr: bool = False
    confidence: float = TEXT_CONFIDENCE
    # The row of a markdown table this chunk came from, when it is one.
    table_id: Optional[str] = None
    row_id: Optional[str] = None


@dataclass
class ParsedDocument:
    """A document read into chunks, with the metadata the chunks need (FR-10)."""

    chunks: list[Chunk] = field(default_factory=list)
    page_count: int = 0
    # Pages that could not be read at all. A failed page is never counted as
    # processed (FR-T2, and the same rule for the corpus).
    failed_pages: list[int] = field(default_factory=list)
    # Pages that had no text layer and were read through OCR.
    ocr_pages: list[int] = field(default_factory=list)


def _clean(text: str) -> str:
    """Strip the control characters a document's text layer carries.

    A PDF's text layer can carry a NUL byte from its glyph mapping, and
    PostgreSQL refuses a NUL in a text column. The byte is not information:
    it is an artefact of how the file was written. The rule lives in
    `textnorm`, which the verifier also uses, so a chunk and the quote checked
    against it are cleaned the same way.
    """
    return strip_control(text)


def _is_heading(line: str) -> bool:
    """True when a line reads as a section heading.

    A heading is short, numbered, and carries no requirement language. A line
    that says "shall" or names a value is a requirement, whatever it looks like.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    if not _HEADING.match(stripped):
        return False
    # The requirement words come from the vocabulary file, so a heading is
    # never one phrase here and another there.
    markers = attrs.clause_markers()
    if markers:
        source = "|".join(re.escape(marker) for marker in markers)
        if re.search(rf"\b(?:{source})\b", stripped, re.IGNORECASE):
            return False
    return True


def _rows_to_markdown(rows: list[list[Optional[str]]]) -> list[tuple[str, str, str]]:
    """Each table row as markdown, with a stable row id.

    FR-T6: every row becomes a candidate requirement carrying its column
    context, and a row id. The row id is what keeps a row's provenance when
    the table spans pages.

    A run of empty cells is written as one separator. A table with merged
    cells gives nine columns where three hold text, and joining them all
    produced rows like "Seat Type |  |  | Temperature Range |  |  |  |  | ".
    """
    out: list[tuple[str, str, str]] = []
    for index, row in enumerate(rows):
        cells = [_clean(cell or "").strip().replace("\n", " ") for cell in row]
        if not any(cells):
            continue
        if index == 0 and all(set(cell) <= {"-", ":"} for cell in cells if cell):
            # The markdown separator row, not a data row.
            continue
        parts: list[str] = []
        for cell in cells:
            if not cell and parts and not parts[-1]:
                continue
            parts.append(cell)
        while parts and not parts[-1]:
            parts.pop()
        out.append((f"r{index}", " | ".join(parts), ""))
    return out


def tesseract_missing() -> Optional[str]:
    """Why OCR cannot run on this machine, or None when it can.

    Tesseract is a program the image has to carry, not a package `pip`
    installs, so it is the one dependency a page with no text layer depends
    on. Naming it here is what keeps "this page is blank" and "this machine
    cannot read pages" from becoming the same answer.
    """
    if shutil.which("tesseract"):
        return None
    return (
        "tesseract is not installed, so a page with no text layer cannot be read "
        "(apt-get install tesseract-ocr tesseract-ocr-eng, or the Windows installer)"
    )


def is_blank_page(page) -> bool:
    """Require no source text and an entirely white render, including annotations."""
    import pymupdf
    try:
        if page.get_text().strip():
            return False
        pixels = page.get_pixmap(dpi=get_settings().ingest_ocr_dpi,
                                 colorspace=pymupdf.csGRAY, alpha=False).samples
        return bool(pixels) and pixels.count(b'\xff') == len(pixels)
    except Exception:
        # Rendering failure cannot prove a page blank; OCR still records the failure.
        return False


def ocr_page_with_reason(pymupdf_page) -> tuple[str, Optional[str]]:
    """The text OCR read on a page, and why there is none.

    The two ways a page comes back empty are different answers, and the ledger
    has to say which one it recorded: a machine that is missing the program, or
    a page that holds nothing to read.
    """
    if get_settings().ocr_backend == 'paddle':
        try:
            from app.services.tender.ocr import read_paddle_page
            from app.services.tender.ocr_layout import page_layout
            return page_layout(read_paddle_page(pymupdf_page), get_settings())['text'], None
        except Exception as exc:
            return '', f'Paddle OCR failed ({type(exc).__name__})'
    missing = tesseract_missing()
    if missing:
        return "", missing

    prefix = get_settings().tessdata_prefix
    if prefix:
        os.environ.setdefault("TESSDATA_PREFIX", prefix)
    try:
        textpage = pymupdf_page.get_textpage_ocr(
            language=get_settings().ingest_ocr_language,
            dpi=get_settings().ingest_ocr_dpi,
            full=True,
        )
        # The textpage must outlive the get_text call: PyMuPDF only holds a
        # weak reference to it, and a collection in between fails the call
        # with "weakly-referenced object no longer exists".
        gc.disable()
        try:
            text = pymupdf_page.get_text(textpage=textpage) or ""
        finally:
            gc.enable()
    except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
        logger.warning("OCR failed for a page: %s", exc)
        return "", f"the OCR could not read this page: {type(exc).__name__}: {exc}"

    if not text.strip():
        return "", "the OCR read nothing on this page"
    return text, None


def ocr_page(pymupdf_page) -> Optional[str]:
    """The text OCR read on a page, when it can (FR-9).

    A page that could not be read is None, and the caller records it as a
    failed page: the corpus is never silently missing one.
    """
    text, _ = ocr_page_with_reason(pymupdf_page)
    return text or None


def _parse_pdf(path: Path) -> ParsedDocument:
    """Read a PDF into chunks."""
    import pymupdf

    parsed = ParsedDocument()
    document = pymupdf.open(path)
    parsed.page_count = document.page_count
    section: Optional[str] = None

    # The pages that hold a table, found with PyMuPDF's fast finder. pdfplumber
    # is only asked about these.
    table_pages = _pages_with_tables(document)

    page_texts: dict[int, str] = {}
    ocr_pages: set[int] = set()
    for index, page in enumerate(document):
        number = index + 1
        text = page.get_text() or ""
        if len(text.strip()) < _min_chars_for_text():
            if is_blank_page(page):
                continue
            # No usable text layer: this page is scanned (FR-9).
            ocr_text = ocr_page(page)
            if ocr_text and ocr_text.strip():
                ocr_pages.add(number)
                parsed.ocr_pages.append(number)
                text = ocr_text
            else:
                parsed.failed_pages.append(number)
                continue
        page_texts[number] = text

    # A running header and footer repeat on every page and are not evidence of
    # anything. They are dropped from every page but the first, so the reader
    # still sees them once and the search index is not full of them.
    repeated = _header_lines(document)
    first_page = min(page_texts) if page_texts else None

    with pdfplumber_tables(path, table_pages) as tables_by_page:
        for number in sorted(page_texts):
            text = page_texts[number]
            ocr = number in ocr_pages
            for chunk in _chunks_from_text(text, number, section, ocr, repeated, first_page):
                if chunk.section is not None:
                    section = chunk.section
                parsed.chunks.append(chunk)

            # The tables on this page, as markdown rows.
            for table_index, rows in enumerate(tables_by_page.get(number, [])):
                for row_id, row_text, _ in _rows_to_markdown(rows):
                    if not row_text.strip():
                        continue
                    parsed.chunks.append(
                        Chunk(
                            text=row_text,
                            page=number,
                            section=section,
                            is_table=True,
                            ocr=ocr,
                            confidence=_ocr_confidence() if ocr else TEXT_CONFIDENCE,
                            table_id=f"p{number}t{table_index}",
                            row_id=row_id,
                        )
                    )
    document.close()
    return parsed


def _pages_with_tables(document) -> set[int]:
    """The pages that hold a table, found with PyMuPDF's table finder.

    The finder is used as a gate, not as the source of the table: it is fast,
    and pdfplumber reads the cells better. A page it misses is a page whose
    table is not ruled, which pdfplumber would struggle with too.
    """
    pages: set[int] = set()
    for index, page in enumerate(document):
        try:
            if page.find_tables().tables:
                pages.add(index + 1)
        except Exception as exc:  # noqa: BLE001 - a bad page must not stop the run
            logger.warning("table detection failed on page %s: %s", index + 1, exc)
    return pages


class pdfplumber_tables:
    """The tables on the given pages, read with pdfplumber.

    A context manager so the pdfplumber file handle is closed even when a
    page fails.
    """

    def __init__(self, path: Path, pages: set[int]):
        self.path = path
        self.pages = pages
        self._pdf = None

    def __enter__(self) -> dict[int, list[list[list[Optional[str]]]]]:
        import pdfplumber

        self._pdf = pdfplumber.open(self.path)
        found: dict[int, list[list[list[Optional[str]]]]] = {}
        for index, page in enumerate(self._pdf.pages):
            if index + 1 not in self.pages:
                continue
            try:
                tables = page.extract_tables()
            except Exception as exc:  # noqa: BLE001 - a bad table must not lose the page
                logger.warning("table extraction failed on page %s: %s", index + 1, exc)
                tables = []
            if tables:
                found[index + 1] = tables
        return found

    def __exit__(self, *exc_info) -> None:
        if self._pdf is not None:
            self._pdf.close()


def _page_lines(page) -> list[tuple[str, float]]:
    """A page's lines, with the vertical position of each.

    The text is the same as `page.get_text()` gives, joined from the spans so
    that the words of a line are not split by the layout.
    """
    lines: list[tuple[str, float]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append((text, line["bbox"][1]))
    return lines


def _header_lines(document) -> set[str]:
    """The running header and footer of a document, by position.

    A manufacturer's header sits in the top band of every page and its footer
    in the bottom band:

        F-EDD-035_REV.0 Date 30.07.2020     Page 1 of 2

    They say nothing about the valve, and they repeat on every page. Position
    is the signal, not the text: a page's real content can also repeat, and
    comparing the words would drop it.

    A line is a header or footer when it sits in the top or bottom band of
    the page and the same line appears on most pages, with its digits ignored
    so that the page number does not hide it.
    """
    if document.page_count < 2:
        return set()

    counts: dict[str, int] = {}
    band_lines: list[str] = []
    for page in document:
        height = page.rect.height or 1.0
        for text, y in _page_lines(page):
            band = get_settings().ingest_header_band_fraction
            if not (y < height * band or y > height * (1 - band)):
                continue
            band_lines.append(text)
            key = _header_key(text)
            counts[key] = counts.get(key, 0) + 1

    # On most pages. With two pages, "most" means both.
    threshold = max(2, (document.page_count * 2 + 2) // 3)
    repeated = {key for key, count in counts.items() if count >= threshold}
    return repeated


def _header_key(line: str) -> str:
    """A line with its digits removed, so a page number does not hide a header."""
    return re.sub(r"\d+", "#", line)


def _chunks_from_text(
    text: str,
    number: int,
    section: Optional[str],
    ocr: bool,
    repeated: Optional[set[str]] = None,
    keep_page: Optional[int] = None,
) -> list[Chunk]:
    """Split a page's text into chunks.

    A brochure page is not one line per paragraph: the text layer of a
    designed page is a scatter of short fragments, and chunking on every
    newline produced 263 chunks for one page of a catalogue. Short fragments
    are therefore joined into one chunk, and only a line long enough to stand
    on its own ends a chunk.
    """
    confidence = _ocr_confidence() if ocr else TEXT_CONFIDENCE
    chunks: list[Chunk] = []
    current_section = section
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        joined = " ".join(buffer).strip()
        if joined:
            chunks.append(
                Chunk(
                    text=joined,
                    page=number,
                    section=current_section,
                    is_table=False,
                    ocr=ocr,
                    confidence=confidence,
                )
            )
        buffer.clear()

    for block in text.split("\n"):
        stripped = _clean(block).strip()
        if not stripped:
            continue
        if repeated and _header_key(stripped) in repeated and keep_page != number:
            # A running header: kept on the first page, dropped on the rest.
            continue
        if _is_heading(stripped):
            flush()
            current_section = stripped.rstrip(":").strip()
            continue
        buffer.append(stripped)
        if len(stripped) >= get_settings().ingest_min_chunk_chars:
            flush()
    flush()
    return chunks


def _parse_docx(path: Path) -> ParsedDocument:
    """Read a DOCX into chunks.

    A DOCX has no pages, so every chunk carries page 1 and the order of the
    body is what preserves the reading order.
    """
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    parsed = ParsedDocument()
    section: Optional[str] = None
    table_index = 0

    for child in document.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = _clean(paragraph.text).strip()
            if not text:
                continue
            style = (paragraph.style.name or "").lower()
            if style.startswith("heading") or _is_heading(text):
                section = text.rstrip(":").strip()
                continue
            parsed.chunks.append(
                Chunk(text=text, page=1, section=section, is_table=False)
            )
        elif tag == "tbl":
            table = Table(child, document)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            table_index += 1
            for row_id, row_text, _ in _rows_to_markdown(rows):
                if not row_text.strip():
                    continue
                parsed.chunks.append(
                    Chunk(
                        text=row_text,
                        page=1,
                        section=section,
                        is_table=True,
                        table_id=f"t{table_index}",
                        row_id=row_id,
                    )
                )
    parsed.page_count = 1
    return parsed


# The parser for each file type. A new format is a new entry here, not a new
# branch in the dispatch.
PARSERS: dict[str, Callable[[Path], ParsedDocument]] = {
    ".pdf": _parse_pdf,
    ".docx": _parse_docx,
}


def parse_document(path: Path) -> ParsedDocument:
    """Parse a document into chunks.

    The file type decides the parser, not the extension alone: a `.pdf` that
    is not a PDF fails loudly rather than producing empty chunks.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        raise ValueError(f"unsupported document type: {path.suffix or path.name}")
    return parser(path)


def iter_manifest_documents(manufacturers_dir: Path) -> Iterable[tuple[Path, dict]]:
    """Every collected document, with the manifest record that describes it.

    The manifest is the hand-off from the collector: it names the file and
    carries the metadata the `document` row needs. `manufacturers_dir` is the
    directory that holds one sub-directory per manufacturer.
    """
    manufacturers = Path(manufacturers_dir)
    if not manufacturers.exists():
        return
    for manifest_path in sorted(manufacturers.glob("*/manifest.json")):
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # The manufacturer is named once, at the top of the manifest.
        manufacturer = manifest.get("manufacturer") or manifest_path.parent.name
        for record in manifest.get("documents", []):
            relative = record.get("path")
            if not relative:
                continue
            file_path = manifest_path.parent / relative
            if not file_path.exists():
                logger.warning("manifest names a missing file: %s", file_path)
                continue
            entry = dict(record)
            entry.setdefault("manufacturer", manufacturer)
            yield file_path, entry


def iter_csv_corpus(catalogue_dir: Path) -> Iterable[tuple[Path, dict]]:
    """Every document in a catalogue, with the manifest record that describes it.

    A catalogue is written in more than one way: a CSV listing every
    document, or a JSON manifest per manufacturer, which is what the
    collector writes. Both are read, and so is a file on disk that no
    manifest names - a corpus is the set of files that exist, and leaving the
    unlisted ones out would make it quietly incomplete.
    """
    catalogue_dir = Path(catalogue_dir)
    if not catalogue_dir.exists():
        return

    import csv

    # The collector's own manifests, one per manufacturer.
    for manifest_path in sorted(catalogue_dir.glob("*/manifest.json")):
        import json

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read %s: %s", manifest_path, exc)
            continue
        manufacturer = manifest.get("manufacturer") or manifest_path.parent.name
        for record in manifest.get("documents", []):
            relative = record.get("path")
            if not relative:
                continue
            file_path = manifest_path.parent / relative
            if not file_path.exists():
                logger.warning("manifest names a missing file: %s", file_path)
                continue
            entry = dict(record)
            entry.setdefault("manufacturer", manufacturer)
            entry["doc_type"] = entry.get("doc_type") or entry.get("kind")
            entry["path"] = str(file_path)
            yield file_path, entry

    for manifest_path in sorted(catalogue_dir.glob("**/manifest.csv")):
        with manifest_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                filename = (row.get("filename") or "").strip()
                if not filename:
                    continue
                manufacturer = (row.get("manufacturer") or "").strip()
                file_path = _catalogue_file(manifest_path.parent, manufacturer, filename)
                if file_path is None:
                    logger.warning("manifest names a missing file: %s", filename)
                    continue
                yield file_path, {
                    "manufacturer": manufacturer,
                    "url": (row.get("source_url") or "").strip(),
                    "sha256": (row.get("sha256") or "").strip(),
                    "doc_type": (row.get("doc_type") or "").strip(),
                    "retrieved_at": (row.get("retrieved_at") or "").strip(),
                    "version_date": None,
                    "path": file_path.name,
                    "is_secondary": False,
                    "restriction_note": None,
                    "model_or_family": (row.get("model_or_family") or "").strip() or None,
                    # A blank is left blank: no category is not a wrong one,
                    # and the filter keeps a document that declares nothing
                    # (D-137).
                    "category": (row.get("category") or "").strip().casefold() or None,
                    "notes": (row.get("notes") or "").strip(),
                }

    for file_path in _unlisted_documents(catalogue_dir):
        yield file_path, {
            # The manufacturer is the catalogue's own directory for this file,
            # not the folder it happens to sit in: a corpus keeps its fetched
            # copies under `<manufacturer>/raw/`, and naming a manufacturer
            # "raw" invents one that owns no document (D-140).
            "manufacturer": file_path.relative_to(catalogue_dir).parts[0],
            "url": "",
            "sha256": sha256_of_file(file_path),
            "doc_type": "",
            "retrieved_at": "",
            "version_date": None,
            "path": file_path.name,
            "is_secondary": False,
            "restriction_note": None,
            "model_or_family": None,
            "category": None,
            "notes": "not in the manifest",
        }


def _catalogue_file(root: Path, manufacturer: str, filename: str) -> Optional[Path]:
    """Find a catalogue file named by a manifest row.

    A catalogue keeps one directory per manufacturer, and a manifest can sit
    beside them or inside one, so the file is looked for in the
    manufacturer's directory first and then beside the manifest.
    """
    candidates = []
    if manufacturer:
        candidates.append(root / manufacturer / filename)
    candidates.append(root / filename)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _unlisted_documents(catalogue_dir: Path) -> list[Path]:
    """The documents on disk that no manifest lists."""
    listed: set[Path] = set()
    for manifest_path in catalogue_dir.glob("**/manifest.csv"):
        import csv

        with manifest_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                filename = (row.get("filename") or "").strip()
                if not filename:
                    continue
                file_path = _catalogue_file(
                    manifest_path.parent, (row.get("manufacturer") or "").strip(), filename
                )
                if file_path is not None:
                    listed.add(file_path.resolve())

    unlisted: list[Path] = []
    extensions = load_doc_types().corpus_extensions
    for file_path in sorted(catalogue_dir.rglob("*")):
        if not file_path.is_file() or file_path.suffix.lower() not in extensions:
            continue
        if file_path.name == "manifest.csv":
            continue
        if file_path.resolve() in listed:
            continue
        unlisted.append(file_path)
    return unlisted


def sha256_of_file(path: Path) -> str:
    """The hash of a file, so an unlisted document still has one."""
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
