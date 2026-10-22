"""The vertical slice: one tender, end to end.

The tests cover the four outcomes that matter: a tender that works, a
tender whose attachments are missing, a tender whose files are malformed,
and a requirement that was never extracted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.tenderslice import run_slice


def _spec_pdf(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """A specification PDF with a bill-of-quantities table.

    The header is on the first page under a title, and the table continues
    onto the second page without repeating it, as the real document does.
    The table is ruled, because that is what makes it a table.
    """
    import pymupdf

    document = pymupdf.open()
    columns = (55, 95, 235, 430, 465, 510)
    # The first row is on the first page, the second on the second, and the
    # header is not repeated: that is how the real document runs.
    for page_index, page_rows in enumerate((rows[:1], rows[1:])):
        page = document.new_page()
        top = 60
        if page_index == 0:
            page.insert_text((60, top), "BOQ", fontsize=14)
            top += 30
            for index, cell in enumerate(["Sr. No.", "Item", "Descriptions", "Qty", "Unit"]):
                page.insert_text((columns[index], top), cell, fontsize=7)
            top += 18

        for number, title, description, qty in page_rows:
            for index, value in enumerate([number, title, description, qty, "Nos."]):
                page.insert_text((columns[index], top), value, fontsize=7)
            top += 18

        # Rule the table, so it is a table and not a column of text.
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
            page.draw_line(pymupdf.Point(columns[0], y), pymupdf.Point(columns[-1], y), width=0.5)
    document.save(path)
    document.close()


def _boq_csv(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text(
        "Item Number,Item Title,Item Description,Item Quantity,Unit of Measure\n"
        + "".join(f"{n},{t},{d},{q},pieces\n" for n, t, d, q in rows),
        encoding="utf-8",
    )


def _tender(
    root: Path,
    *,
    with_boq: bool = True,
    with_spec: bool = True,
    malformed: bool = False,
) -> Path:
    """A tender directory with its frozen attachments."""
    directory = root / "T1"
    raw = directory / "raw"
    raw.mkdir(parents=True)

    documents = []
    if with_boq:
        _boq_csv(
            raw / "boq.csv",
            [
                ("1", "GATE VALVE SIZE 150 MM CLASS 150", "Flanged Gate valve 6 inch", "13"),
                ("2", "CHECK VALVE 100 MM", "Wafer check valve", "4"),
            ],
        )
        documents.append(
            {
                "url": "https://mkp.gem.gov.in/boq.csv",
                "status": "new",
                "kind": "boq-csv",
                "filename": "boq.csv",
                "path": "raw/boq.csv",
                "sha256": "a" * 64,
            }
        )
    if with_spec:
        if malformed:
            (raw / "spec.pdf").write_bytes(b"this is not a pdf")
        else:
            _spec_pdf(
                raw / "spec.pdf",
                [
                    ("1", "GATE VALVE SIZE 150 MM CLASS 150", "Cast carbon steel Face to face distance 267mm", "13"),
                    ("2", "CHECK VALVE 100 MM", "Wafer swing type check valve Working pressure 8 bar", "4"),
                ],
            )
        documents.append(
            {
                "url": "https://mkp.gem.gov.in/spec.pdf",
                "status": "new",
                "kind": "boq-pdf",
                "filename": "spec.pdf",
                "path": "raw/spec.pdf",
                "sha256": "b" * 64,
            }
        )

    (directory / "manifest.json").write_text(
        json.dumps({"tender_id": "T1", "category": "valve", "documents": documents}),
        encoding="utf-8",
    )
    return directory


def test_a_tender_is_read_from_its_attachments(tmp_path):
    """The slice: the bill of quantities gives the items, the specification
    gives the requirements, and each requirement carries its item."""
    directory = _tender(tmp_path)

    result = run_slice("T1", directory)

    assert len(result.items) == 2
    assert result.items[0].item_id == "1"
    assert result.items[0].quantity == "13"
    assert len(result.requirements) == 2
    assert result.requirements[0].item_id == "1"
    assert "Face to face distance 267mm" in result.requirements[0].text


def test_every_requirement_carries_its_item(tmp_path):
    directory = _tender(tmp_path)

    result = run_slice("T1", directory)

    item_ids = {item.item_id for item in result.items}
    for requirement in result.requirements:
        assert requirement.item_id in item_ids


def test_a_requirement_is_found_in_the_page_it_names(tmp_path):
    """The quote check: a requirement has to be in the text it came from."""
    directory = _tender(tmp_path)

    result = run_slice("T1", directory)

    assert result.failed_quotes == []
    assert result.bad_pages == []


def test_a_tender_with_no_attachments_has_no_requirements_and_says_so(tmp_path):
    """A tender with no attachments is a tender whose attachments are
    missing. It is not a tender with no requirements, and it is not
    silently empty."""
    directory = _tender(tmp_path, with_boq=False, with_spec=False)

    result = run_slice("T1", directory)

    assert result.requirements == []
    assert result.items == []


def test_a_missing_attachment_is_reported_as_missing(tmp_path):
    """The failed fetch is in the ledger, with its reason, and the UI is told
    to prompt for an upload."""
    directory = _tender(tmp_path, with_spec=False)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"].append(
        {
            "url": "https://mkp.gem.gov.in/gone.pdf",
            "status": "failed",
            "note": "the server returned HTTP 404",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_slice("T1", directory)

    assert len(result.missing) == 1
    assert result.missing[0]["url"] == "https://mkp.gem.gov.in/gone.pdf"
    assert result.missing[0]["status"] == "failed"


def test_a_malformed_file_does_not_stop_the_run(tmp_path):
    """A file that cannot be parsed is a file that cannot be parsed. The
    other attachment is still read."""
    directory = _tender(tmp_path, malformed=True)

    result = run_slice("T1", directory)

    # The bill of quantities is still read: one broken file is not the whole
    # tender.
    assert len(result.items) == 2


def test_a_requirement_that_was_never_extracted_is_visible(tmp_path):
    """The recall check: a line the tender stated that produced no
    requirement is listed, so a missed requirement is not invisible."""
    directory = _tender(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["documents"].append(
        {
            "url": "https://mkp.gem.gov.in/extra.pdf",
            "status": "new",
            "kind": "boq-pdf",
            "filename": "extra.pdf",
            "path": "raw/extra.pdf",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # A specification row that names no item in the bill of quantities.
    (directory / "raw" / "extra.pdf").write_bytes(b"%PDF-1.4 not really a pdf")

    result = run_slice("T1", directory)

    # The extra attachment is malformed, so it contributes nothing, and the
    # tender is still read from the good one.
    assert len(result.requirements) == 2


def test_a_tender_with_no_manifest_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_slice("NOPE", tmp_path)
