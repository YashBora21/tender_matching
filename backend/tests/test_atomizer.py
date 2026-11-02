"""Atomizer tests: compound lines become atomic requirements (FR-13)."""

from __future__ import annotations

from app.schemas.enums import ExtractionMethod
from app.services.requirement.atomize import atomize, atomize_records
from app.services.tender.ingest import RequirementRecord


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


def test_two_sentences_on_one_line_are_two_requirements() -> None:
    """A description that puts two obligations in one paragraph is two
    requirements, not one (FR-13). A decimal point is not a sentence break."""
    parts = atomize("Body shall be SS316. Trim shall be 13Cr.")
    texts = [part.text for part in parts]
    assert len(texts) == 2
    assert texts[0] == "Body shall be SS316"
    assert "Trim shall be 13Cr" in texts[1]


def test_a_second_obligation_joined_by_and_is_split() -> None:
    parts = atomize("The body shall be SS316 and the trim shall be 13Cr")
    texts = [part.text for part in parts]
    assert len(texts) == 2
    assert "body shall be SS316" in texts[0]
    assert "trim shall be 13Cr" in texts[1]


def test_a_bare_atom_is_searched_with_the_clause_it_came_from() -> None:
    """A split-off value names no subject. "1500 RPM" alone matches every
    page that mentions a speed, so its clause is carried with it (D-96)."""
    parts = atomize("Body shall be SS316, protection IP68, speed 1500 RPM")
    by_text = {part.text: part for part in parts}

    # A self-contained atom is left as it is.
    assert by_text["Body shall be SS316"].retrieval_text == "Body shall be SS316"

    # A bare value carries the clause, so the subject is in the search.
    bare = by_text["speed 1500 RPM"]
    assert "Body shall be SS316" in bare.retrieval_text
    assert "speed 1500 RPM" in bare.retrieval_text
    assert bare.retrieval_text.count("speed 1500 RPM") == 1


def test_a_heading_is_part_of_the_context_of_its_sub_clauses() -> None:
    parts = atomize("3.2 Materials: a) body SS316 b) trim 13Cr")
    assert parts
    for part in parts:
        assert part.retrieval_text.startswith("3.2 Materials")


# --- the production stage (D-105) -----------------------------------------


def _record(text: str, **overrides) -> RequirementRecord:
    values = {
        "text": text,
        "item_id": "10",
        "page": 3,
        "source_section": "3.2 Materials",
    }
    values.update(overrides)
    return RequirementRecord(**values)


def test_a_compound_prose_clause_is_split_with_its_provenance():
    record = _record(
        "Body shall be SS316, protection IP68, speed 1500 RPM",
        extraction_method="text",
    )

    parts = atomize_records([record])

    assert [part.text for part in parts] == [
        "Body shall be SS316",
        "protection IP68",
        "speed 1500 RPM",
    ]
    assert all(part.item_id == "10" for part in parts)
    assert all(part.page == 3 for part in parts)
    assert all(part.source_section == "3.2 Materials" for part in parts)
    assert all(part.extraction_method == ExtractionMethod.PROSE.value for part in parts)


def test_a_table_row_is_not_atomized_by_the_records_stage():
    record = _record("1500 RPM, SS316, IP68", extraction_method="table")

    assert atomize_records([record]) == [record]


def test_an_atomic_prose_clause_is_returned_unchanged():
    record = _record(
        "Rotational speed shall be not less than 1500 RPM",
        extraction_method="text",
    )

    assert atomize_records([record]) == [record]


def test_an_atom_keeps_the_review_flag_of_its_clause():
    record = _record(
        "Body shall be SS316, speed 1500 RPM",
        extraction_method="text",
        review_required=True,
        review_reason="page classified as boilerplate",
    )

    parts = atomize_records([record])

    assert len(parts) == 2
    assert all(part.review_required for part in parts)
    assert all(
        part.review_reason == "page classified as boilerplate" for part in parts
    )
