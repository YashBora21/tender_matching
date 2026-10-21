"""Atomizer tests: compound lines become atomic requirements (FR-13)."""

from __future__ import annotations

from app.schemas.enums import ExtractionMethod
from app.services.atomizer import atomize


def test_a_compound_line_is_split() -> None:
    parts = atomize("Body shall be SS316, protection IP68, speed 1500 RPM")
    assert len(parts) > 1
    joined = " | ".join(part.text for part in parts)
    assert "SS316" in joined
    assert "IP68" in joined
    assert "1500 RPM" in joined


def test_lettered_sub_clauses_are_split() -> None:
    parts = atomize("3.2 Materials: a) body SS316 b) trim 13Cr")
    texts = [part.text for part in parts]
    assert len(texts) == 2
    assert "body SS316" in texts[0]
    assert "trim 13Cr" in texts[1]


def test_each_part_keeps_its_offsets_in_the_source_text() -> None:
    text = "Body shall be SS316, protection IP68"
    for part in atomize(text):
        assert text[part.start : part.end].strip() == part.text


def test_a_table_row_is_not_split_again() -> None:
    # FR-T6: a table row is already one parameter.
    row = "1500 RPM, SS316, IP68"
    parts = atomize(row, extraction_method=ExtractionMethod.TABLE)
    assert len(parts) == 1
    assert parts[0].text == row


def test_a_single_requirement_stays_one_requirement() -> None:
    parts = atomize("Rotational speed shall be not less than 1500 RPM")
    assert len(parts) == 1


def test_empty_text_yields_nothing() -> None:
    assert atomize("   ") == []


def test_the_parent_clause_is_carried_onto_every_part() -> None:
    parts = atomize("a) body SS316 b) trim 13Cr", parent_clause="3.2 Materials")
    assert parts
    assert all(part.parent_clause == "3.2 Materials" for part in parts)
