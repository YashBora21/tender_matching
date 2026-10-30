"""The atomic gold rows: proposals only, and every quote is on the page.

The rows are one attribute of one requirement against one manufacturer, so a
per-attribute status can be checked on its own instead of inside a compound
clause. Two rules are enforced here:

- a row is a proposal until a reviewer confirms it: `label_state` is
  `proposed` and `confirmed` is `no`. Nothing in this file may say otherwise;
- a non-empty `gold_quote` is an exact substring of the named page of the
  named document. The check reads the real catalogue PDF, so it skips when the
  corpus is not present (the PDFs are not in git) rather than passing on an
  unverified quote.

The labels are real catalogue facts, not synthetic fixtures; this module tests
their form and their citation, not a made-up manufacturer.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GOLD = REPO / "eval" / "gold" / "proposed-GEM_9671792-items-10-11-atomic.csv"
CATALOGUE = REPO / "backend" / "data" / "catalogue"

ALLOWED_STATUSES = {
    "full_match",
    "partial_match",
    "missing_capability",
    "unsupported_requirement",
    "no_evidence_found",
}

REQUIRED_COLUMNS = {
    "requirement_id",
    "requirement",
    "manufacturer",
    "item_id",
    "item_title",
    "attribute",
    "required_value",
    "documented_value",
    "gold_document",
    "gold_page",
    "gold_quote",
    "true_status",
    "label_state",
    "confirmed",
    "proposal_reason",
    "tender_sha256",
}


def _rows() -> list[dict[str, str]]:
    with GOLD.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _page_text(document: str, page: int) -> str | None:
    """The page text, or None when the corpus is not on this machine."""
    if not CATALOGUE.is_dir():
        return None
    matches = list(CATALOGUE.rglob(document))
    if not matches:
        return None
    import pymupdf as fitz

    with fitz.open(matches[0]) as pdf:
        return pdf[page - 1].get_text("text")


def test_the_atomic_rows_carry_the_expected_columns():
    rows = _rows()
    assert rows, "the atomic gold file is empty"
    assert REQUIRED_COLUMNS.issubset(rows[0].keys())


def test_every_atomic_row_is_a_proposal_and_nothing_is_confirmed():
    for row in _rows():
        assert row["label_state"] == "proposed", row
        assert row["confirmed"] == "no", row


def test_one_row_per_requirement_manufacturer_and_attribute():
    seen: set[tuple[str, str, str]] = set()
    for row in _rows():
        key = (row["requirement_id"], row["manufacturer"], row["attribute"])
        assert key not in seen, f"duplicate atomic row: {key}"
        seen.add(key)


def test_every_atomic_row_has_a_known_status():
    for row in _rows():
        assert row["true_status"] in ALLOWED_STATUSES, row


def test_every_atomic_row_names_a_document_page_and_quote():
    for row in _rows():
        assert row["gold_document"], row
        assert row["gold_page"].isdigit(), row
        assert row["gold_quote"].strip(), row


def test_every_quote_is_an_exact_substring_of_the_named_page():
    """A quote nobody can find on the page is not evidence. The corpus PDFs
    are not in git, so the test skips when they are absent."""
    checked = 0
    for row in _rows():
        page = int(row["gold_page"])
        text = _page_text(row["gold_document"], page)
        if text is None:
            pytest.skip(
                "the catalogue PDFs are not present, so the quotes cannot be "
                "checked against their pages"
            )
        assert row["gold_quote"] in text, (
            f"{row['manufacturer']} {row['attribute']} "
            f"p{row['gold_page']}: quote is not on the page: "
            f"{row['gold_quote']!r}"
        )
        checked += 1
    assert checked == len(_rows())
