"""FR-16: the category is inferred per bill-of-quantities item.

A tender's category is a coarse hint. The item is what a requirement belongs
to, so the item's own words decide the category it is compared in (FR-T9), and
an item whose category cannot be settled is flagged rather than guessed.

The vocabulary is a data file, so these tests never invent a category in code:
the shipped file is read, and a temporary one is written where a test needs a
vocabulary of its own.
"""

from __future__ import annotations

import json

import pytest

from app.services.match.category import (
    UNKNOWN,
    categories_path,
    category_names,
    category_types,
    document_type,
    infer_category,
    infer_type,
    load_categories,
    mentioned_categories,
    mentioned_types,
)


def test_the_shipped_vocabulary_names_the_categories_the_corpus_uses():
    from pathlib import Path

    from app.core.config import get_settings

    vocabulary = load_categories()
    assert vocabulary, "the shipped category vocabulary is missing"
    assert {"valves", "pumps", "motors"} <= category_names(vocabulary)
    # The file is a data file under the data directory, not a list in a module.
    assert Path(categories_path()).name == "categories.json"
    assert Path(categories_path()).parent == Path(get_settings().data_dir)


@pytest.mark.parametrize(
    "title, description, expected",
    [
        ("GATE VALVE SIZE 150 MM CLASS 150", "Flanged gate valve 6 inch", "valves"),
        ("GLOBE VALVE 80 MM", "", "valves"),
        ("Severn glocon make control valves 1", "", "valves"),
        ("CENTRIFUGAL PUMP 50 M3/HR", "Dewatering application", "pumps"),
        ("Submersible pump for sump", "", "pumps"),
        ("INDUCTION MOTOR 30 KW", "Squirrel cage motor", "motors"),
    ],
)
def test_an_item_is_classified_by_its_own_words(title, description, expected):
    guess = infer_category(title, description)
    assert guess.category == expected
    assert guess.term
    assert not guess.ambiguous


def test_the_most_specific_term_wins():
    """A motor-operated gate valve is bought as a valve.

    "gate valve" says what the thing is; "motor" says how it is driven. The
    longer term is the more specific description of the item.
    """
    guess = infer_category("MOTOR OPERATED GATE VALVE")
    assert guess.category == "valves"
    assert guess.term == "gate valve"


def test_an_item_naming_two_categories_equally_is_flagged_not_guessed():
    """"gate valve" and "gear motor" are equally specific, and the item names
    both. Picking one would compare the requirement against the wrong corpus."""
    guess = infer_category("GATE VALVE AND GEAR MOTOR")
    assert guess.category == UNKNOWN
    assert guess.ambiguous
    assert "more than one category" in (guess.reason or "")


def test_an_item_that_names_no_category_is_unknown():
    """A spare part is a real requirement with no category in the vocabulary.

    It is left unknown - and therefore unfiltered by the run - instead of
    being pushed into whichever category the tender declares.
    """
    guess = infer_category("PISTON RING", "Spare for the compressor")
    assert guess.category == UNKNOWN
    assert not guess.known
    assert not guess.ambiguous
    assert guess.reason == "the item names no product category"


def test_an_empty_vocabulary_classifies_nothing_and_does_not_crash():
    guess = infer_category("GATE VALVE", categories=())
    assert guess.category == UNKNOWN
    assert guess.reason == "the category vocabulary is empty"


def test_the_vocabulary_is_a_data_file_a_deployment_can_own(tmp_path, monkeypatch):
    """Adding a category is editing a file, not editing code (FR-6)."""
    path = tmp_path / "categories.json"
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {"name": "gaskets", "terms": ["spiral wound", "ring gasket"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CATEGORY_VOCABULARY_PATH", str(path))

    assert category_names() == frozenset({"gaskets"})
    assert infer_category("Spiral Wound Gasket 4 inch").category == "gaskets"
    # A term that is not in this vocabulary is not a category.
    assert infer_category("GATE VALVE").category == UNKNOWN


def test_a_missing_vocabulary_file_gives_no_categories_instead_of_crashing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CATEGORY_VOCABULARY_PATH", str(tmp_path / "absent.json"))
    assert load_categories() == ()
    assert infer_category("GATE VALVE").category == UNKNOWN


def test_a_broken_vocabulary_file_gives_no_categories(tmp_path, monkeypatch):
    path = tmp_path / "categories.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CATEGORY_VOCABULARY_PATH", str(path))
    assert load_categories() == ()


def test_the_items_stage_is_keyed_on_the_category_vocabulary(tmp_path, monkeypatch):
    """The item stage infers the category, so the vocabulary is one of its
    inputs: correcting the vocabulary re-reads the items, and nothing before
    them."""
    from app.core.config import get_settings
    from app.services.tender.pipeline import STAGE_ATTACHMENTS, STAGE_ITEMS, stage_keys

    one = tmp_path / "one.json"
    one.write_text(json.dumps({"categories": [{"name": "valves", "terms": ["valve"]}]}))
    two = tmp_path / "two.json"
    two.write_text(
        json.dumps({"categories": [{"name": "valves", "terms": ["valve", "gate valve"]}]})
    )
    attachments = [
        {"kind": "boq-csv", "sha256": "a" * 64, "status": "new"},
        {"kind": "boq-pdf", "sha256": "b" * 64, "status": "new"},
    ]
    kwargs = dict(
        prompt_version="1", schema_version="1", model="m", vocabulary_hash="v"
    )

    monkeypatch.setenv("CATEGORY_VOCABULARY_PATH", str(one))
    before = stage_keys(attachments, **kwargs)
    monkeypatch.setenv("CATEGORY_VOCABULARY_PATH", str(two))
    # Settings are read once per process; a changed environment is a restart.
    get_settings.cache_clear()
    after = stage_keys(attachments, **kwargs)

    assert before[STAGE_ATTACHMENTS] == after[STAGE_ATTACHMENTS]
    assert before[STAGE_ITEMS] != after[STAGE_ITEMS]


def test_a_valve_item_names_its_type_from_the_same_data_file():
    """The category is `valves`; the type inside it is `gate`, and it comes
    from the data file, not from a list in this module (D-97)."""
    guess = infer_type("valves", "GATE VALVE, SIZE-150 MM, CLASS-150")
    assert guess.product_type == "gate"
    assert guess.term == "gate valve"
    assert not guess.ambiguous


def test_a_sluice_valve_is_a_gate_valve():
    assert infer_type("valves", "SLUICE VALVE 200 MM").product_type == "gate"


def test_a_type_is_not_inferred_without_a_category():
    guess = infer_type(None, "GATE VALVE")
    assert guess.product_type is None
    assert not guess.known
    assert guess.reason


def test_two_types_named_equally_are_ambiguous_and_not_guessed():
    """A line that names a gate valve and a ball valve equally is not
    filtered on a coin toss (FR-T9)."""
    guess = infer_type("valves", "GATE VALVE AND BALL VALVE")
    assert guess.product_type is None
    assert guess.ambiguous
    assert "more than one type" in (guess.reason or "")


def test_mentioned_types_tells_silent_from_different():
    """Silence is not a different type: a chunk that never says which valve
    it is may still be about the wanted one (PRD 4)."""
    assert mentioned_types("valves", "Body shall be SS316") == frozenset()
    assert mentioned_types("valves", "Ball valve body") == frozenset({"ball"})
    assert mentioned_types("valves", "Gate valve and ball valve") == frozenset(
        {"gate", "ball"}
    )


def test_the_valve_types_are_declared_in_the_data_file():
    names = {kind.name for kind in category_types("valves")}
    assert {"gate", "globe", "check", "ball", "butterfly"} <= names
    # A category with no declared types is not an error; it simply has none.
    assert category_types("pumps") == ()


def test_a_document_is_placed_by_its_family_not_only_its_words():
    """A datasheet page rarely repeats "ball valve"; the cover names the
    family. The family is in the data file and places the document (D-100)."""
    assert document_type("valves", "D-Series").product_type == "control"
    assert document_type("valves", "Series 7/8").product_type == "ball"
    assert document_type("valves", "Worcester Controls").product_type == "ball"
    assert document_type("valves", "VM-4700-S").product_type == "ball"


def test_a_document_that_names_no_type_has_an_unknown_type():
    """Unknown is not a different type: the caller keeps the document and
    marks its evidence family-level (D-100)."""
    guess = document_type("valves", "WCENBR0001_EN_AQ.pdf")
    assert guess.product_type is None
    assert not guess.known


def test_a_family_matches_however_the_title_punctuates_it():
    """A family in the vocabulary is matched after normalisation, so the
    hyphen in "Series 7/8" does not make it dead against "SERIES-7-8-R1"
    (D-107)."""
    assert (
        document_type(
            "valves",
            "1692719045-Technical-Bulletin---118-PRODUCT-SPECIFICATION-SERIES-7-8-R1",
        ).product_type
        == "ball"
    )
    assert document_type("valves", "Fisher D series").product_type == "control"
    assert document_type("valves", "Fisher large ED/EWD ET/EWT").product_type == "control"
    assert document_type("valves", "Fisher globe valves").product_type == "globe"


def test_mentioned_categories_tells_silent_from_different():
    """A pump catalogue is not evidence for a valve item, even when its
    manifest never filled the category column (D-107)."""
    assert mentioned_categories("Goulds wastewater pumps") == frozenset({"pumps"})
    assert mentioned_categories("Godwin dewatering pumps") == frozenset({"pumps"})
    assert mentioned_categories("lv-motors-hazardous-area-application") == frozenset(
        {"motors"}
    )
    assert mentioned_categories("Control valves") == frozenset({"valves"})
    # A file name that runs its words together still says what it is.
    assert mentioned_categories("submersibledewateringpumps_e10361") == frozenset(
        {"pumps"}
    )
    # Silence is not a different category.
    assert mentioned_categories("salesprogram_e00543-2") == frozenset()
    assert mentioned_categories("Body shall be SS316") == frozenset()


def test_every_item_is_classified_from_its_own_words():
    """Two items in one tender can be in two categories: the tender's own
    category is not copied onto them (FR-16, FR-T9)."""
    from app.services.tender.pipeline import assign_categories
    from app.services.tender.ingest import TenderItem

    items = assign_categories(
        [
            TenderItem(item_id="1", title="GATE VALVE SIZE 150 MM"),
            TenderItem(item_id="2", title="CENTRIFUGAL PUMP 50 M3/HR"),
            TenderItem(item_id="3", title="PISTON RING"),
        ]
    )

    assert [item.category for item in items] == ["valves", "pumps", UNKNOWN]
    assert items[0].category_term == "gate valve"
    assert items[0].category_ambiguous is False
    # The item nothing recognised says why, so the run can flag it.
    assert items[2].category_reason
    assert items[2].category_ambiguous is False
