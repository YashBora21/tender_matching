"""Tender ingestion tests: the bilingual lines, the item identity, the
requirement records.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas.enums import ExtractionMethod, LedgerStatus, RequirementType
from app.services.tender.ingest import (
    RequirementRecord,
    TenderItem,
    assign_to_items,
    english_half,
    is_mangled,
    item_id_for,
    read_boq_csv,
    read_boq_table,
    read_specification,
)


def test_the_english_half_of_a_bilingual_line_is_read():
    """A GeM line is "Hindi/English" or "Hindi English"."""
    assert english_half("बड ववरण/Bid Details") == "Bid Details"
    assert english_half("मा ालय/रा!य का नाम/Ministry/State Name") == "Ministry/State Name"
    assert english_half("Quantity") == "Quantity"


def test_a_line_with_no_english_has_no_english_half():
    assert english_half("बड ववरण") == ""


def test_a_mangled_line_is_flagged_and_is_not_a_missing_requirement():
    """The Devanagari is sometimes mangled by the PDF's encoding. A line that
    cannot be read is an unreadable line, not a requirement that was never
    stated."""
    assert is_mangled("बड ववरण/Bid Details") is False
    assert is_mangled("¶¤ ���श�ष") is True
    assert is_mangled("Cast steel body, 16 bar") is False


def test_the_item_key_is_the_number_within_the_tender():
    """The item is identified by the tender it belongs to and its number
    within it. The tender is a column, so the key is the number alone
    (FR-T9)."""
    assert item_id_for("GEM_9671792", "3") == "3"


def test_a_boq_csv_gives_the_items(tmp_path):
    path = tmp_path / "boq.csv"
    path.write_text(
        "Item Number,Item Title,Item Description,Item Quantity,Unit of Measure,"
        "Consignee ID,Delivery Period (In number of days)\n"
        "1,GATE VALVE SIZE 150 MM CLASS 150,Flanged Gate valve 6 inches,13,pieces,X,126\n"
        "2,CAST STEEL GATE VALVE SIZE 250MM,Flanged Gate valve 10 inches,1,pieces,X,126\n",
        encoding="utf-8",
    )

    items = read_boq_csv(path, "GEM_1", "valve")

    assert len(items) == 2
    assert items[0].item_id == "1"
    assert items[0].title == "GATE VALVE SIZE 150 MM CLASS 150"
    assert items[0].quantity == "13"
    assert items[0].unit == "pieces"
    assert items[1].item_id == "2"


def test_a_boq_table_gives_the_items_whatever_the_header_says():
    """The buyers write the header four different ways."""
    headers = [
        ["BOQ Item number", "BOQ Item Title", "BOQ Item Description", "BOQ Item Quantity"],
        ["Sr. No.", "Item Descriptions", "Qty", "Unit"],
        ["SR.NO", "Brief Description", "ITEM DESCRIPTION", "QUANTITY", "UNIT"],
        ["Item No", "Material Code", "Item Title", "Material Description", "Quantiry", "Unit"],
    ]
    for header in headers:
        rows = [header, ["1", "GATE VALVE 150MM", "Flanged gate valve", "13"]]
        items = read_boq_table(rows, "GEM_1", "valve")
        assert len(items) == 1, header
        assert items[0].item_id == "1"
        assert items[0].title


def test_the_item_column_is_not_mistaken_for_the_description():
    """A table with "Item" and "Descriptions" has an item column and a
    description column, and "description" must not be taken for the item."""
    rows = [
        ["Sr. No.", "Item", "Descriptions", "Qty", "Unit"],
        ["1", "GATE VALVE, SIZE-150 MM, CLASS-150", "Flanged Gate valve 6 inch", "13", "Nos."],
    ]
    items = read_boq_table(rows, "T1")

    assert len(items) == 1
    assert items[0].title == "GATE VALVE, SIZE-150 MM, CLASS-150"
    assert items[0].description == "Flanged Gate valve 6 inch"


def test_a_table_that_runs_over_a_page_break_keeps_its_columns():
    """A table that continues onto the next page does not repeat its
    header, so the columns are carried over."""
    from app.services.tender.ingest import _find_header_row, read_boq_table

    first = [
        ["BOQ", "", "", ""],
        ["Sr. No.", "Item", "Descriptions", "Qty"],
        ["1", "GATE VALVE 150 MM", "Flanged Gate valve 6 inch", "13"],
    ]
    second = [["2", "CHECK VALVE 100 MM", "Wafer check valve", "4"]]

    header_row, columns = _find_header_row(first)
    assert columns is not None

    items = read_boq_table(first[header_row + 1:], "T1", columns=columns)
    items += read_boq_table(second, "T1", columns=columns)

    assert [item.item_id for item in items] == ["1", "2"]
    assert items[1].title == "CHECK VALVE 100 MM"


def test_a_row_without_a_number_is_not_an_item():
    rows = [["Item No", "Item Title"], ["", "no number"], ["2", "GATE VALVE"]]
    items = read_boq_table(rows, "GEM_1")
    assert [item.item_id for item in items] == ["2"]


def test_a_scanned_page_without_specifications_gives_no_items(monkeypatch, tmp_path):
    """A scanned page carries the buyer's letterhead and the signature block
    as well as the requirements, and those are numbered too. A line that
    states no specification is not an item."""
    from app.services.tender import ingest

    scanned = "1.\nShow Cause Notice\n2.\n0 Circulation of the names of Agencies\n"
    monkeypatch.setattr("app.services.tender.parse.ocr_page", lambda page: scanned)

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = ingest.read_boq_items(path, "T1")

    assert items == []


def test_a_repeated_item_number_is_written_once(monkeypatch, tmp_path):
    """A scanner reads a two-column table as two columns of text, so a
    number can be seen twice."""
    from app.services.tender import ingest

    scanned = "1.\nGATE VALVE 150 MM\nFlanged gate valve\n1.\nGATE VALVE 150 MM\nFlanged gate valve\n"
    monkeypatch.setattr("app.services.tender.parse.ocr_page", lambda page: scanned)

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = ingest.read_boq_items(path, "T1")

    assert [item.item_id for item in items] == ["1"]


def test_a_requirement_carries_its_item():
    requirement = RequirementRecord(
        text="Body of body shall be SS316 with pressure rating 16 bar",
        item_id="GEM_1:3",
        requirement_type=RequirementType.TECHNICAL,
    )
    assert requirement.item_id == "GEM_1:3"
    assert requirement.requirement_type is RequirementType.TECHNICAL


def test_a_scanned_bill_of_quantities_gives_its_items(monkeypatch, tmp_path):
    """A bill of quantities the buyer scanned is still a bill of quantities.

    The number of an item arrives on its own line when the table was read as
    two columns, and the item is named on the line below.
    """
    from app.services.tender import ingest

    scanned = "1.\nGATE VALVE SIZE 150 MM\nFlanged gate valve 6 inch\n2.\nCHECK VALVE\nWafer check valve\n"
    monkeypatch.setattr(
        "app.services.tender.parse.ocr_page", lambda page: scanned
    )

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = ingest.read_boq_items(path, "T1")

    assert [item.item_id for item in items] == ["1", "2"]
    assert items[0].title.startswith("GATE VALVE")
    assert "Flanged gate valve" in items[0].description


def test_a_scanned_bill_of_quantities_with_inline_numbers(monkeypatch, tmp_path):
    """The number and the words arrive on one line when the table was not
    read as two columns."""
    from app.services.tender import ingest

    scanned = "1. GATE VALVE SIZE 150 MM\nFlanged gate valve 6 inch\n"
    monkeypatch.setattr(
        "app.services.tender.parse.ocr_page", lambda page: scanned
    )

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = ingest.read_boq_items(path, "T1")

    assert [item.item_id for item in items] == ["1"]
    assert items[0].title.startswith("GATE VALVE")


# --- how a page is read (step 2) -------------------------------------------
#
# Every PDF below is synthetic: it is drawn by the test, not taken from a
# tender.


def _prose_pdf(path: Path, lines: list[str]) -> None:
    """A page with a text layer and no rules: a prose page."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    top = 60
    for line in lines:
        page.insert_text((60, top), line, fontsize=9)
        top += 14
    document.save(path)
    document.close()


def _table_pdf(path: Path, rows: list[list[str]]) -> None:
    """A page whose text is laid out in a ruled table."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    columns = (55, 95, 235, 430, 465, 510)
    top = 60
    for row in rows:
        for index, cell in enumerate(row):
            page.insert_text((columns[index], top), cell, fontsize=7)
        top += 18
    bottom = top
    for index in range(len(columns) - 1):
        page.draw_line(
            pymupdf.Point(columns[index], 40),
            pymupdf.Point(columns[index], bottom),
            width=0.5,
        )
    page.draw_line(
        pymupdf.Point(columns[-1], 40), pymupdf.Point(columns[-1], bottom), width=0.5
    )
    for y in list(range(40, bottom, 18)):
        page.draw_line(
            pymupdf.Point(columns[0], y), pymupdf.Point(columns[-1], y), width=0.5
        )
    document.save(path)
    document.close()


def test_a_prose_page_is_read_as_clauses_not_as_lines(tmp_path):
    """A line break in a PDF is a wrap. The wrapped lines are joined before
    anything is split, so a clause is never cut in half."""
    path = tmp_path / "spec.pdf"
    _prose_pdf(
        path,
        [
            "5.2 The pump shall be tested at a pressure of 10 bar",
            "and the impeller material shall be stainless steel.",
        ],
    )

    read = read_specification(path)

    assert [row["extraction_method"] for row in read.rows] == [
        ExtractionMethod.TEXT.value
    ]
    assert read.rows[0]["description"] == (
        "5.2 The pump shall be tested at a pressure of 10 bar and the "
        "impeller material shall be stainless steel."
    )
    assert read.pages[0].status == LedgerStatus.PARSED.value
    assert read.pages[0].method == ExtractionMethod.TEXT.value


def test_a_section_heading_is_context_and_not_a_requirement(tmp_path):
    """An all-capitals heading names the scope of the clauses under it. It is
    not a capability, and it is not emitted as a requirement."""
    path = tmp_path / "spec.pdf"
    _prose_pdf(
        path,
        [
            "SPECIFICATION DETAILS FOR 75 HP PUMP & MOTOR",
            "5.1 The pump shall be of vertical submersible type.",
        ],
    )

    read = read_specification(path)

    assert len(read.rows) == 1
    assert read.rows[0]["title"] == "SPECIFICATION DETAILS FOR 75 HP PUMP & MOTOR"
    assert read.rows[0]["description"].startswith("5.1 The pump")


def test_a_sparse_ruled_grid_is_not_read_as_a_table(tmp_path):
    """A ruled page of prose is a grid with almost nothing in it. Reading its
    cells as rows is what turned legal paragraphs into requirements."""
    path = tmp_path / "spec.pdf"
    _table_pdf(
        path,
        [
            ["", "BHARAT HEAVY ELECTRICALS LIMITED", "HPE-21101-01"],
            ["", "", ""],
            ["", "The Bidder(s) will not offer any inducement.", ""],
            ["COPYRIGHT & CONFIDENTIAL", "", ""],
            ["", "", ""],
        ],
    )

    read = read_specification(path)

    assert read.rows
    assert all(
        row["extraction_method"] != ExtractionMethod.TABLE.value for row in read.rows
    )


def test_a_ruled_table_is_read_as_a_table(tmp_path):
    """A real table has several columns, consistent rows and text in most of
    its cells. Its description column holds the requirements."""
    path = tmp_path / "spec.pdf"
    _table_pdf(
        path,
        [
            ["Sr. No.", "Item", "Descriptions", "Qty", "Unit"],
            ["1", "GATE VALVE", "Cast carbon steel body", "13", "Nos."],
            ["2", "CHECK VALVE", "Wafer type, 8 bar", "4", "Nos."],
        ],
    )

    read = read_specification(path)

    assert [row["number"] for row in read.rows] == ["1", "2"]
    assert read.rows[0]["description"] == "Cast carbon steel body"
    assert read.pages[0].method == ExtractionMethod.TABLE.value
    assert read.pages[0].status == LedgerStatus.PARSED.value


def test_a_page_that_is_mostly_a_picture_is_ocred(monkeypatch, tmp_path):
    """A scan often carries a typed letterhead over a picture of the page. The
    text layer is present and says nothing; the words that matter are in the
    image, so the page is read through OCR."""
    import pymupdf

    path = tmp_path / "scan.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (60, 60),
        "File No. NH-SPS-TS016/29/2026-TWN (Computer No. 175321)",
        fontsize=9,
    )
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8))
    pixmap.set_rect(pixmap.irect, (255, 255, 255))
    page.insert_image(pymupdf.Rect(0, 0, page.rect.width, page.rect.height), pixmap=pixmap)
    document.save(path)
    document.close()

    monkeypatch.setattr(
        "app.services.tender.parse.ocr_page_with_reason",
        lambda page: ("5.1 The pump shall deliver 50 m3/h at 30 m head.", None),
    )

    read = read_specification(path)

    assert read.pages[0].method == ExtractionMethod.OCR.value
    assert read.rows[0]["extraction_method"] == ExtractionMethod.OCR.value
    assert read.rows[0]["description"].startswith("5.1 The pump shall deliver")


def test_a_picture_page_that_cannot_be_ocred_is_a_failed_page_with_the_reason(tmp_path, monkeypatch):
    """The page is not read as silent: the ledger says it failed, and says
    that the machine had no OCR to read it with (AGENTS #3, FR-T2)."""
    import pymupdf

    path = tmp_path / "spec.pdf"
    document = pymupdf.open()
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8))
    pixmap.set_rect(pixmap.irect, (255, 255, 255))
    page.insert_image(pymupdf.Rect(0, 0, page.rect.width, page.rect.height), pixmap=pixmap)
    document.save(path)
    document.close()

    monkeypatch.setattr(
        "app.services.tender.parse.tesseract_missing",
        lambda: "tesseract is not installed, so a page with no text layer cannot be read",
    )

    read = read_specification(path)

    assert read.pages[0].status == LedgerStatus.FAILED.value
    assert "tesseract" in read.pages[0].error


def test_a_single_item_tender_takes_its_prose_clauses(tmp_path):
    """A prose specification has no item column. A tender with one item is
    not a guess: every clause of the specification is a clause of it."""
    path = tmp_path / "spec.pdf"
    _prose_pdf(path, ["5.1 The pump shall deliver 50 m3/h at 30 m head."])
    rows = read_specification(path).rows

    assigned, unassigned = assign_to_items(
        rows, [TenderItem(item_id="1", title="SUBMERSIBLE PUMP")]
    )

    assert not unassigned
    assert assigned[0].item_id == "1"
    assert assigned[0].extraction_method == ExtractionMethod.TEXT.value
