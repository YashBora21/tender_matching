"""Vocabulary tests: the attribute catalogue is data, not code (FR-6, FR-27).

A deployment must be able to add an attribute or a synonym by editing a data
file. These tests load an alternative vocabulary and check that the normalizer
and the material comparison follow it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.comparison import attributes as attrs
from app.comparison.vocabulary import load_vocabulary
from app.services.normalizer import normalize

CUSTOM_VOCABULARY = {
    "attributes": [
        {
            "canonical_term": "chill_depth",
            "value_type": "numeric",
            "synonyms": ["chill depth", "hardfacing depth"],
            "default_unit": "mm",
        }
    ],
    "material_groups": {
        "duplex": ["duplex", "2205", "s32205"],
    },
    "material_parents": {},
    "material_pattern": r"\b(?:duplex|2205|s32205)\b",
    "certification_pattern": r"\bNACE\s?MR\s?\d{4}\b",
    "ip_pattern": r"\bIP\s?\d{2}\b",
    "dn_pattern": r"\bDN\s?\d+\b",
}


@pytest.fixture
def custom_vocabulary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = tmp_path / "vocabulary.json"
    path.write_text(json.dumps(CUSTOM_VOCABULARY), encoding="utf-8")
    monkeypatch.setenv("ATTRIBUTE_VOCABULARY_PATH", str(path))
    # The caches are keyed on the path, but the settings are cached too.
    from app.core.config import get_settings

    get_settings.cache_clear()
    load_vocabulary.cache_clear()
    yield str(path)
    get_settings.cache_clear()
    load_vocabulary.cache_clear()


def test_a_custom_vocabulary_replaces_the_shipped_one(custom_vocabulary: str) -> None:
    vocabulary = load_vocabulary(custom_vocabulary)
    assert [spec.canonical_term for spec in vocabulary.attributes] == ["chill_depth"]


def test_a_custom_attribute_is_recognised(custom_vocabulary: str) -> None:
    result = normalize("Chill depth shall be 3 mm")
    assert result.attribute == "chill_depth"
    assert result.value == "3"
    assert result.unit == "mm"


def test_an_attribute_not_in_the_custom_vocabulary_is_not_recognised(custom_vocabulary: str) -> None:
    # "rotational speed" is in the shipped vocabulary but not the custom one.
    result = normalize("Rotational speed shall be 1500 RPM")
    assert result.attribute is None


def test_a_custom_material_group_is_used(custom_vocabulary: str) -> None:
    assert attrs.material_group("2205") == "duplex"
    assert attrs.material_group("SS316") is None


def test_the_shipped_vocabulary_is_the_default() -> None:
    vocabulary = load_vocabulary()
    terms = {spec.canonical_term for spec in vocabulary.attributes}
    assert "rotational_speed" in terms
    assert "material_grade" in terms


def test_a_value_shape_beats_an_attribute_word_that_cannot_extract_a_value() -> None:
    """"Seal shall be Viton" names a seal but states a material.

    The attribute word is the first candidate, not the last word on the
    matter: when it cannot pull a value out of this text and another
    attribute's value shape can, the value shape wins.
    """
    result = normalize("Seal shall be Viton")
    assert result.attribute == "material_grade"
    assert result.text_value == "viton"
    assert result.is_usable


def test_an_attribute_word_that_can_extract_a_value_still_wins() -> None:
    # "mechanical seal" is a seal arrangement, and no grade is stated.
    result = normalize("Seal shall be a mechanical seal")
    assert result.attribute == "seal_arrangement"

    result = normalize("Mechanical seal shall be provided")
    assert result.attribute == "seal_arrangement"


def test_a_compound_requirement_keeps_the_attribute_it_carries_a_value_for() -> None:
    """The value shape decides, so a compound line is not stolen by a grade.

    "SS316 ... pressure rating 16 bar" contains a grade and a pressure. The
    numeric attribute can extract the 16 bar; the material attribute would
    take the grade, but the requirement is about the pressure.
    """
    result = normalize("Material of body shall be SS316 with pressure rating 16 bar")
    assert result.attribute == "operating_pressure"
    assert result.value == "16"
    assert result.unit == "bar"


def test_the_neighbouring_seal_elastomers_already_resolved_correctly() -> None:
    # "seal" is the only word that names an arrangement and is also a
    # plausible material word; "gasket" and "packing" are not synonyms.
    for text, expected in (
        ("Gasket shall be PTFE", "material_grade"),
        ("Packing shall be Viton", "material_grade"),
        ("Wetted parts shall be SS316", "material_grade"),
    ):
        assert normalize(text).attribute == expected
