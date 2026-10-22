"""Tender ingestion tests: the bilingual lines, the item identity, the
requirement records.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas.enums import RequirementType
from app.services.tenderingest import (
    RequirementRecord,
    TenderItem,
    english_half,
    is_mangled,
    item_id_for,
    read_boq_csv,
    read_boq_table,
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
    from app.services.tenderingest import _find_header_row, read_boq_table

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
    from app.services import tenderingest

    scanned = "1.\nShow Cause Notice\n2.\n0 Circulation of the names of Agencies\n"
    monkeypatch.setattr("app.services.docparser.ocr_page", lambda page: scanned)

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = tenderingest.read_boq_items(path, "T1")

    assert items == []


def test_a_repeated_item_number_is_written_once(monkeypatch, tmp_path):
    """A scanner reads a two-column table as two columns of text, so a
    number can be seen twice."""
    from app.services import tenderingest

    scanned = "1.\nGATE VALVE 150 MM\nFlanged gate valve\n1.\nGATE VALVE 150 MM\nFlanged gate valve\n"
    monkeypatch.setattr("app.services.docparser.ocr_page", lambda page: scanned)

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = tenderingest.read_boq_items(path, "T1")

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
    from app.services import tenderingest

    scanned = "1.\nGATE VALVE SIZE 150 MM\nFlanged gate valve 6 inch\n2.\nCHECK VALVE\nWafer check valve\n"
    monkeypatch.setattr(
        "app.services.docparser.ocr_page", lambda page: scanned
    )

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = tenderingest.read_boq_items(path, "T1")

    assert [item.item_id for item in items] == ["1", "2"]
    assert items[0].title.startswith("GATE VALVE")
    assert "Flanged gate valve" in items[0].description


def test_a_scanned_bill_of_quantities_with_inline_numbers(monkeypatch, tmp_path):
    """The number and the words arrive on one line when the table was not
    read as two columns."""
    from app.services import tenderingest

    scanned = "1. GATE VALVE SIZE 150 MM\nFlanged gate valve 6 inch\n"
    monkeypatch.setattr(
        "app.services.docparser.ocr_page", lambda page: scanned
    )

    path = tmp_path / "scanned.pdf"
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    items = tenderingest.read_boq_items(path, "T1")

    assert [item.item_id for item in items] == ["1"]
    assert items[0].title.startswith("GATE VALVE")
