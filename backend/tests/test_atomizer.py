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


def test_a_semicolon_joined_line_is_split() -> None:
    """A semicolon-joined clause is exactly the compound line FR-13 is for.

    Every split rule in the list must be tried: a rule that comes after a
    non-matching one is dead code if the list stops at the first miss.
    """
    parts = atomize("Valve shall be SS316; trim 13Cr; seal Viton")
    texts = [part.text for part in parts]
    assert len(texts) == 3
    assert texts == ["Valve shall be SS316", "trim 13Cr", "seal Viton"]


def test_a_numbered_sub_clause_does_not_hide_the_semicolon_rule() -> None:
    parts = atomize("1) body SS316 2) trim 13Cr; seal Viton")
    texts = [part.text for part in parts]
    assert len(texts) == 3
    assert "seal Viton" in texts


def test_a_lettered_sub_clause_does_not_hide_the_semicolon_rule() -> None:
    parts = atomize("a) body SS316 b) trim 13Cr; seal Viton")
    texts = [part.text for part in parts]
    assert len(texts) == 3
    assert "seal Viton" in texts


def test_splitting_does_not_explode_into_empty_pieces() -> None:
    parts = atomize("; ".join(["trim 13Cr"] * 30))
    assert len(parts) == 30
    assert all(part.text.strip() for part in parts)
