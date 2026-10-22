"""Document parsing tests: page numbers, tables, sections and OCR (FR-8 to FR-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.docparser import (
    TEXT_CONFIDENCE,
    Chunk,
    ParsedDocument,
    parse_document,
)


def _pdf_with_table(path: Path) -> None:
    """A one-page PDF with a heading, a sentence and a ruled table."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 60), "3.2 Materials", fontsize=16)
    page.insert_text((72, 90), "Body of body shall be SS316", fontsize=10)

    data = [["Parameter", "Value", "Unit"], ["Max operating pressure", "16", "bar"]]
    x0, y0, width, row_height = 72, 120, 320, 20
    rows, columns = len(data), len(data[0])
    for r in range(rows + 1):
        page.draw_line(
            pymupdf.Point(x0, y0 + r * row_height),
            pymupdf.Point(x0 + width, y0 + r * row_height),
            width=0.7,
        )
    for c in range(columns + 1):
        page.draw_line(
            pymupdf.Point(x0 + c * (width / columns), y0),
            pymupdf.Point(x0 + c * (width / columns), y0 + rows * row_height),
            width=0.7,
        )
    for r, row in enumerate(data):
        for c, value in enumerate(row):
            page.insert_text(
                (x0 + c * (width / columns) + 4, y0 + r * row_height + 14), value, fontsize=9
            )
    document.save(path)
    document.close()


def _pdf_with_two_pages(path: Path) -> None:
    import pymupdf

    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 90), "Body of body shall be SS316", fontsize=10)
    second = document.new_page()
    second.insert_text((72, 90), "Seal shall be Viton", fontsize=10)
    document.save(path)
    document.close()


def test_a_pdf_is_parsed_into_chunks_with_its_page(tmp_path):
    path = tmp_path / "doc.pdf"
    _pdf_with_two_pages(path)

    parsed = parse_document(path)

    assert parsed.page_count == 2
    assert [chunk.page for chunk in parsed.chunks] == [1, 2]
    assert any("SS316" in chunk.text for chunk in parsed.chunks)
    assert any("Viton" in chunk.text for chunk in parsed.chunks)


def test_a_table_becomes_one_chunk_per_row(tmp_path):
    """FR-T6: every row becomes a candidate requirement carrying its column
    context and a row id."""
    path = tmp_path / "doc.pdf"
    _pdf_with_table(path)

    parsed = parse_document(path)

    table_chunks = [chunk for chunk in parsed.chunks if chunk.is_table]
    assert len(table_chunks) == 2
    assert "Max operating pressure" in table_chunks[1].text
    assert "16" in table_chunks[1].text
    assert table_chunks[1].row_id
    assert table_chunks[1].table_id


def test_a_section_heading_is_carried_by_the_text_below_it(tmp_path):
    path = tmp_path / "doc.pdf"
    _pdf_with_table(path)

    parsed = parse_document(path)

    # The heading is not a chunk of its own: it names the section the rest of
    # the page sits under.
    assert not any(chunk.text == "3.2 Materials" for chunk in parsed.chunks)
    body = next(chunk for chunk in parsed.chunks if "SS316" in chunk.text)
    assert body.section == "3.2 Materials"


def test_a_table_row_keeps_its_cells_apart(tmp_path):
    """A row's cells must not run together, or the value is unreadable."""
    path = tmp_path / "doc.pdf"
    _pdf_with_table(path)

    parsed = parse_document(path)

    row = next(chunk for chunk in parsed.chunks if chunk.is_table and chunk.row_id != "r0")
    assert "|" in row.text
    assert "Max operating pressure" in row.text
    assert "Maxoperatingpressure" not in row.text


def test_a_page_with_no_text_is_ocr_flagged(tmp_path, monkeypatch):
    """FR-9: a page with no extractable text goes to OCR, is tagged, and
    carries a lower confidence."""
    path = tmp_path / "doc.pdf"
    _pdf_with_two_pages(path)

    def fake_ocr(page):
        return "Body of body shall be SS316"

    monkeypatch.setattr("app.services.docparser.ocr_page", fake_ocr)
    # A page whose text layer is this short is treated as scanned.
    monkeypatch.setenv("INGEST_MIN_CHARS_FOR_TEXT", "10000")

    parsed = parse_document(path)

    assert parsed.ocr_pages == [1, 2]
    assert all(chunk.ocr for chunk in parsed.chunks)
    assert all(chunk.confidence < TEXT_CONFIDENCE for chunk in parsed.chunks)


def test_a_page_that_cannot_be_read_is_reported_as_failed(tmp_path, monkeypatch):
    """A page that could not be read is never counted as processed."""
    path = tmp_path / "doc.pdf"
    _pdf_with_two_pages(path)

    monkeypatch.setattr("app.services.docparser.ocr_page", lambda page: None)
    monkeypatch.setenv("INGEST_MIN_CHARS_FOR_TEXT", "10000")

    parsed = parse_document(path)

    assert parsed.failed_pages == [1, 2]
    assert parsed.ocr_pages == []
    assert parsed.chunks == []


def test_a_docx_is_parsed_with_its_headings_and_tables(tmp_path):
    import docx

    path = tmp_path / "doc.docx"
    document = docx.Document()
    document.add_heading("3.2 Materials", level=1)
    document.add_paragraph("Body of body shall be SS316")
    table = document.add_table(rows=2, cols=3)
    table.style = "Table Grid"
    for index, value in enumerate(["Parameter", "Value", "Unit"]):
        table.cell(0, index).text = value
    for index, value in enumerate(["Max operating pressure", "16", "bar"]):
        table.cell(1, index).text = value
    document.save(path)

    parsed = parse_document(path)

    body = next(chunk for chunk in parsed.chunks if "SS316" in chunk.text)
    assert body.section == "3.2 Materials"
    table_chunks = [chunk for chunk in parsed.chunks if chunk.is_table]
    assert len(table_chunks) == 2
    assert "Max operating pressure" in table_chunks[1].text


def test_control_characters_are_stripped(tmp_path):
    """A PDF's text layer can carry a NUL byte, and PostgreSQL refuses a NUL
    in a text column. The byte is an artefact of the file, not information."""
    import pymupdf

    path = tmp_path / "doc.pdf"
    document = pymupdf.open()
    page = document.new_page()
    # PyMuPDF writes what it is given, including a NUL.
    page.insert_text((72, 90), "Body of body shall be SS316\x00 with trim 13Cr", fontsize=10)
    document.save(path)
    document.close()

    parsed = parse_document(path)

    assert parsed.chunks
    for chunk in parsed.chunks:
        assert "\x00" not in chunk.text
        assert "\x00" not in (chunk.section or "")


def test_an_unsupported_file_type_is_refused(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_document(path)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_document(tmp_path / "nope.pdf")


def _pdf_with_a_running_header(path: Path) -> None:
    """Three pages that each carry the same header line, with the content
    below the header band, as in a real bulletin."""
    import pymupdf

    document = pymupdf.open()
    for index in range(3):
        page = document.new_page()
        page.insert_text((72, 50), f"Acme Flow Controls REV.3 Page {index + 1} of 3", fontsize=8)
        page.insert_text(
            (72, 500),
            f"Body of body shall be SS316 variant {index} in this series, and the "
            "seat shall be resiliently seated against the disc for long service.",
            fontsize=10,
        )
    document.save(path)
    document.close()


def test_a_running_header_is_kept_once_not_on_every_page(tmp_path):
    """A header that repeats on every page says nothing about the valve. It
    is kept on the first page and dropped on the rest."""
    path = tmp_path / "doc.pdf"
    _pdf_with_a_running_header(path)

    parsed = parse_document(path)

    headers = [c for c in parsed.chunks if "Acme Flow Controls" in c.text]
    assert len(headers) == 1
    # The content of every page is still there.
    assert len([c for c in parsed.chunks if "SS316" in c.text]) == 3


def test_short_lines_are_joined_into_one_chunk(tmp_path):
    """A designed page's text layer is a scatter of fragments. Chunking on
    every newline made 263 chunks of one brochure page."""
    import pymupdf

    path = tmp_path / "doc.pdf"
    document = pymupdf.open()
    page = document.new_page()
    for index in range(40):
        page.insert_text((72, 40 + index * 12), f"frag{index}", fontsize=9)
    document.save(path)
    document.close()

    parsed = parse_document(path)

    assert len(parsed.chunks) == 1
    assert "frag0" in parsed.chunks[0].text
    assert "frag39" in parsed.chunks[0].text


def test_empty_table_cells_do_not_become_a_row_of_separators(tmp_path):
    """A table with merged cells has many empty columns. The row has to read
    as the data it holds."""
    import pdfplumber

    path = tmp_path / "doc.pdf"
    _pdf_with_table(path)

    # Build the row by hand: a header cell, two blanks, then a value.
    rows = [["Seat Type", None, None, "Temperature Range", None, None]]
    rendered = _render_row(rows[0])

    assert "|" in rendered
    assert "Seat Type" in rendered
    assert "Temperature Range" in rendered
    assert "  |  " not in rendered


def _render_row(cells) -> str:
    from app.services.docparser import _rows_to_markdown

    return _rows_to_markdown([cells])[0][1]
