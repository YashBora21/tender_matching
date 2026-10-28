"""The fragment filter: a piece of a line is not a requirement.

Every clause below is synthetic. None of it is taken from a real tender.
"""

from __future__ import annotations

from app.services.tenderingest import (
    FragmentReport,
    RequirementRecord,
    drop_fragments,
    is_fragment,
)


def _record(text: str, *, page: int = 1, item_id: str = "1") -> RequirementRecord:
    return RequirementRecord(text=text, item_id=item_id, page=page)


def test_a_clause_number_on_its_own_is_a_fragment() -> None:
    assert is_fragment("5.2") is True
    assert is_fragment("24.13.10") is True
    assert is_fragment("3.") is True


def test_a_tail_with_no_beginning_is_a_fragment() -> None:
    assert is_fragment("o r violate its provisions.") is True


def test_a_value_with_a_unit_is_kept() -> None:
    assert is_fragment("125 MM minimum") is False
    assert is_fragment("406mm") is False


def test_ocr_residue_is_a_fragment() -> None:
    assert is_fragment("|__12 [SPREADER Link aso aniP TP [p PCS]") is True
    assert is_fragment("[a7__|rpmcassamproncontactoR") is True


def test_a_short_line_that_names_nothing_is_a_fragment() -> None:
    assert is_fragment("SHO.") is True
    assert is_fragment("REQUIRED NO.") is True
    assert is_fragment("ee") is True


def test_a_short_line_that_names_a_specification_word_is_kept() -> None:
    assert is_fragment("Type Flanged ends both side") is False
    assert is_fragment("Class 150") is False


def test_a_title_with_no_verb_and_no_value_states_nothing() -> None:
    # The item title is already the item's; it is not a requirement of it.
    assert is_fragment("GATE VALVE CAST STEEL") is True
    assert is_fragment("ACKNOWLEDGEMENT OF RECEIPT") is True


def test_a_real_clause_is_kept() -> None:
    assert (
        is_fragment("5.2 The pump shall be tested at a pressure of 10 bar.")
        is False
    )


def test_a_lowercase_clause_that_states_a_value_is_kept() -> None:
    """A table cell often starts in lowercase. A number with a unit makes the
    clause self-contained evidence, not a lost tail."""
    assert is_fragment("minimum efficiency 85%") is False
    assert is_fragment("max working pressure 10 bar") is False


def test_a_lowercase_tail_is_joined_to_the_clause_before_it() -> None:
    kept, report = drop_fragments(
        [
            _record("The pump shall be tested at a pressure of 10 bar"),
            _record("and the impeller material shall be stainless steel."),
        ]
    )

    assert report == FragmentReport(dropped=0, merged=1)
    assert len(kept) == 1
    assert kept[0].text == (
        "The pump shall be tested at a pressure of 10 bar and the impeller "
        "material shall be stainless steel."
    )


def test_a_lowercase_tail_is_not_joined_to_a_finished_sentence() -> None:
    """Merging into a finished sentence corrupts it, so the tail is dropped."""
    kept, report = drop_fragments(
        [
            _record("The contractor shall indemnify the purchaser."),
            _record("o r violate its provisions."),
        ]
    )

    assert report == FragmentReport(dropped=1, merged=0)
    assert [record.text for record in kept] == [
        "The contractor shall indemnify the purchaser."
    ]


def test_a_fragment_is_never_merged_across_items_or_pages() -> None:
    kept, report = drop_fragments(
        [
            _record("The pump shall be tested at a pressure of 10 bar", item_id="1"),
            _record(
                "and the impeller material shall be stainless steel.", item_id="2"
            ),
        ]
    )

    assert report == FragmentReport(dropped=1, merged=0)
    assert len(kept) == 1


def test_a_mangled_line_is_kept_because_it_is_a_gap_not_a_fragment() -> None:
    kept, report = drop_fragments([_record("� mangled �")])

    assert report == FragmentReport(dropped=0, merged=0)
    assert len(kept) == 1


def test_the_dropped_and_merged_counts_are_reported() -> None:
    kept, report = drop_fragments(
        [
            _record("5.2"),
            _record("The pump shall be tested at a pressure of 10 bar"),
            _record("and the impeller material shall be stainless steel."),
            _record("SHO."),
        ]
    )

    assert report.dropped == 2
    assert report.merged == 1
    assert len(kept) == 1
