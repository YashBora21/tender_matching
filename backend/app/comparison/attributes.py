"""Canonical attribute vocabulary (D-3, FR-27).

The vocabulary itself lives in a data file (`backend/data/attribute_vocabulary.json`),
not in this module. This module is the lookup layer over it: it maps raw
requirement text onto a canonical attribute, and resolves material grades to
their alloy groups.

The vocabulary is deliberately explicit. An attribute that is not in the file
stays unclassified, and an unclassified requirement is flagged for review
rather than guessed at (FR-29, PRD 9).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from app.comparison.vocabulary import (
    AttributeSpec,
    Vocabulary,
    load_vocabulary,
    normalise_token,
    vocabulary_path,
)
from app.schemas.enums import ValueType

__all__ = [
    "AttributeSpec",
    "Vocabulary",
    "attribute_by_term",
    "certification_pattern",
    "clause_markers",
    "dn_pattern",
    "find_attribute",
    "ip_pattern",
    "is_mandatory",
    "load_vocabulary",
    "mandatory_markers",
    "material_group",
    "material_is_family_level",
    "material_pattern",
    "material_satisfies",
    "normalise_token",
    "optional_markers",
    "pressure_class_pattern",
    "same_value",
    "split_values",
    "value_group",
]

# Mandatory and optional wording. The lists live in the vocabulary file, so
# the atomizer, the normalizer and the document parser all read the same
# words: a phrase is not mandatory in one place and informative in another.
def mandatory_markers() -> tuple[str, ...]:
    return load_vocabulary(vocabulary_path()).mandatory_markers


def optional_markers() -> tuple[str, ...]:
    return load_vocabulary(vocabulary_path()).optional_markers


def clause_markers() -> tuple[str, ...]:
    """The words that mark a clause as carrying its own specification."""
    return load_vocabulary(vocabulary_path()).clause_markers


@lru_cache(maxsize=4)
def _matchers(vocabulary_path: Optional[str]) -> tuple[tuple[str, AttributeSpec], ...]:
    """Synonyms sorted longest first, so "operating pressure" beats "pressure"."""
    vocabulary = load_vocabulary(vocabulary_path)
    return tuple(
        sorted(
            ((synonym, spec) for spec in vocabulary.attributes for synonym in spec.synonyms),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
    )


@lru_cache(maxsize=4)
def _patterns(vocabulary_path: Optional[str]):
    """The value-shape patterns, compiled from the vocabulary file."""
    vocabulary = load_vocabulary(vocabulary_path)
    return (
        re.compile(vocabulary.material_pattern, re.IGNORECASE) if vocabulary.material_pattern else None,
        re.compile(vocabulary.certification_pattern, re.IGNORECASE) if vocabulary.certification_pattern else None,
        re.compile(vocabulary.ip_pattern, re.IGNORECASE) if vocabulary.ip_pattern else None,
        re.compile(vocabulary.dn_pattern, re.IGNORECASE) if vocabulary.dn_pattern else None,
        re.compile(vocabulary.pressure_class_pattern, re.IGNORECASE)
        if vocabulary.pressure_class_pattern
        else None,
    )


@lru_cache(maxsize=4)
def _normalised_members(vocabulary_path: Optional[str]) -> dict[str, frozenset[str]]:
    """Material group members keyed by their normalised form."""
    vocabulary = load_vocabulary(vocabulary_path)
    return {
        group: frozenset(normalise_token(member) for member in members)
        for group, members in vocabulary.material_groups.items()
    }


@lru_cache(maxsize=4)
def _value_members(vocabulary_path: Optional[str]) -> dict[str, str]:
    """Each categorical alias to its group, already normalised."""
    vocabulary = load_vocabulary(vocabulary_path)
    return {
        member: group
        for group, members in vocabulary.value_groups.items()
        for member in members
    }


def value_group(value: Optional[str]) -> Optional[str]:
    """The group a categorical value's word belongs to, or None.

    The group is looked up by the value's own word, and a value that merely
    contains a grouped word is not that value: a "gear operated" actuator is
    not a "threaded" one. The data lists every form a document writes, so
    adding a spelling is an edit to the file (FR-20).
    """
    token = normalise_token(value) if value else ""
    if not token:
        return None
    return _value_members(vocabulary_path()).get(token)


def same_value(required: Optional[str], offered: Optional[str]) -> bool:
    """True when both sides name one value: equal, grouped, or one said fuller.

    A required "screwed end" is not offered again as "screwed end" alone, but as
    "Threaded end" - another word for the same connection - so the alias group
    has to answer before the substring rule does. The substring is kept for the
    value that names itself more fully ("flanged" inside "flanged end"), and
    normalising first makes "SS 316" and "ss316" one value.
    """
    if not required or not offered:
        return False
    req_key = normalise_token(required)
    off_key = normalise_token(offered)
    if req_key == off_key:
        return True
    group = value_group(req_key)
    if group is not None and group == value_group(off_key):
        return True
    return req_key in off_key or off_key in req_key


_LIST_SEPARATOR = re.compile(r"\s*(?:/|;|\||,|\bor\b|\band\b)\s*", re.IGNORECASE)
# A list value is a short name, not a sentence. This keeps a prose line that
# happens to contain "body style:" from being read as an availability list.
_MAX_LIST_VALUE_CHARS = 60


def split_values(text: Optional[str]) -> tuple[str, ...]:
    """The values a text lists, or () when it names one value or is prose.

    A long part is a sentence, not a value, and its presence means the whole
    text is not a list: dropping the long parts would leave the short words of
    a paragraph standing as the values a document offers.
    """
    if not text:
        return ()
    parts = [part.strip(" .") for part in _LIST_SEPARATOR.split(text) if part.strip(" .")]
    if len(parts) < 2:
        return ()
    if any(len(part) > _MAX_LIST_VALUE_CHARS for part in parts):
        return ()
    # A dotted leader is a contents entry running to a page number, not a value.
    if any("..." in part for part in parts):
        return ()
    return tuple(parts)


def attribute_by_term(canonical_term: Optional[str]) -> Optional[AttributeSpec]:
    if not canonical_term:
        return None
    return load_vocabulary().by_term.get(canonical_term)


def material_pattern() -> Optional[re.Pattern]:
    """The pattern that recognises a material grade in free text."""
    return _patterns(vocabulary_path())[0]


def certification_pattern() -> Optional[re.Pattern]:
    """The pattern that recognises a standard code (IS 1239, ISO 9001)."""
    return _patterns(vocabulary_path())[1]


def ip_pattern() -> Optional[re.Pattern]:
    """The pattern that recognises an IP rating."""
    return _patterns(vocabulary_path())[2]


def dn_pattern() -> Optional[re.Pattern]:
    """The pattern that recognises a nominal size (DN50, NPS 2")."""
    return _patterns(vocabulary_path())[3]


def pressure_class_pattern() -> Optional[re.Pattern]:
    """The pattern that recognises a pressure class (Class 150, PN16)."""
    return _patterns(vocabulary_path())[4]


def _value_shaped_attribute(text: str) -> Optional[AttributeSpec]:
    """The attribute whose *value shape* the text contains, if any.

    A material grade, a standard code, an IP rating or a nominal size each
    have a shape that can be recognised in the text itself. This is the
    attribute the text can actually be *about*, independently of which
    attribute word it happens to contain.

    Which attribute a shape identifies is read from the vocabulary file, so
    renaming an attribute there does not break this.
    """
    vocabulary = load_vocabulary()
    material_pattern, certification_pattern, ip_pattern, dn_pattern, class_pattern = _patterns(
        vocabulary_path()
    )
    shapes = vocabulary.value_shapes
    if certification_pattern and certification_pattern.search(text):
        return vocabulary.by_term.get(shapes.get("certification", ""))
    if ip_pattern and ip_pattern.search(text):
        return vocabulary.by_term.get(shapes.get("ip_rating", ""))
    if dn_pattern and dn_pattern.search(text):
        return vocabulary.by_term.get(shapes.get("nominal_size", ""))
    if class_pattern and class_pattern.search(text):
        return vocabulary.by_term.get(shapes.get("pressure_class", ""))
    if material_pattern and material_pattern.search(text):
        return vocabulary.by_term.get(shapes.get("material_grade", ""))
    return None


def has_value_shape(spec: AttributeSpec, text: str) -> bool:
    """True when the text carries a value of this attribute's shape.

    The shape is named by the vocabulary file, so the code does not have to
    know which attribute is which.
    """
    vocabulary = load_vocabulary()
    shapes = vocabulary.value_shapes
    if spec.value_type is ValueType.MATERIAL:
        pattern = material_pattern()
        return bool(pattern and pattern.search(text))
    if spec.value_type is ValueType.CERTIFICATION:
        pattern = certification_pattern()
        return bool(pattern and pattern.search(text))
    if spec.canonical_term == shapes.get("ip_rating"):
        pattern = ip_pattern()
        return bool(pattern and pattern.search(text))
    if spec.canonical_term == shapes.get("nominal_size"):
        pattern = dn_pattern()
        return bool(pattern and pattern.search(text))
    if spec.canonical_term == shapes.get("pressure_class"):
        pattern = pressure_class_pattern()
        return bool(pattern and pattern.search(text))
    return False


def _can_extract(spec: AttributeSpec, text: str) -> bool:
    """True when the text carries a value this attribute can compare.

    An attribute word alone is not evidence of what the text is about: "Seal
    shall be Viton" contains the word "seal" but states a material.
    """
    if has_value_shape(spec, text):
        return True
    if spec.value_type is ValueType.CATEGORICAL:
        # A categorical attribute needs a value word after the attribute word.
        return bool(re.search(r"[:=]\s*\S", text))
    # A numeric attribute needs a number, and a unit the value can carry.
    return bool(re.search(r"-?\d+(?:\.\d+)?", text))


def find_attribute(text: str) -> Optional[AttributeSpec]:
    """Best attribute for a piece of requirement text, or None if unclear.

    An attribute word in the text is the first candidate, but not the last
    word on the matter: when the attribute word cannot extract a value from
    this text and another attribute's value shape can, the value shape wins.
    "Seal shall be Viton" names a seal but states a material, so it is a
    material requirement, not a seal arrangement.
    """
    vocabulary = load_vocabulary()
    lowered = text.lower()
    for synonym, spec in _matchers(vocabulary_path()):
        if re.search(rf"\b{re.escape(synonym)}\b", lowered):
            if _can_extract(spec, text):
                return spec
            shaped = _value_shaped_attribute(text)
            if shaped is not None and shaped.canonical_term != spec.canonical_term:
                return shaped
            return spec

    return _value_shaped_attribute(text)


def _shaped_attributes(text: str) -> tuple[AttributeSpec, ...]:
    """Every attribute whose *value shape* this text carries.

    `_value_shaped_attribute` answers "which one is this text about" and stops
    at the first shape it finds; a clause can carry several, and that is the
    difference a compound clause turns on (D-161).
    """
    vocabulary = load_vocabulary()
    patterns = _patterns(vocabulary_path())
    shapes = vocabulary.value_shapes
    found: list[AttributeSpec] = []
    for pattern, term in zip(
        patterns,
        (
            shapes.get("material_grade", ""),
            shapes.get("certification", ""),
            shapes.get("ip_rating", ""),
            shapes.get("nominal_size", ""),
            shapes.get("pressure_class", ""),
        ),
    ):
        if pattern is None or not pattern.search(text):
            continue
        spec = vocabulary.by_term.get(term)
        if spec is not None:
            found.append(spec)
    return tuple(found)


def attributes_named(text: str) -> tuple[AttributeSpec, ...]:
    """Every attribute this text names *and* carries a value of that shape for.

    `find_attribute` answers "which one is this clause about"; this answers
    "how many are in it", which is how a compound clause is recognised. A
    clause that fixes a body material and a pressure class is two
    requirements, and a comparison that reads one of them has answered part
    of the clause, not all of it (PRD 4, D-161).

    The test is the attribute's *value shape*, not "a number appears": a
    numeric attribute would otherwise count any clause with a figure in it
    ("Pressure class 150" naming an operating pressure too). An attribute
    this misses is a missed flag, not a false one.
    """
    seen: dict[str, AttributeSpec] = {}
    lowered = text.lower()
    for synonym, spec in _matchers(vocabulary_path()):
        if spec.canonical_term in seen:
            continue
        if re.search(rf"\b{re.escape(synonym)}\b", lowered) and has_value_shape(
            spec, text
        ):
            seen[spec.canonical_term] = spec
    for spec in _shaped_attributes(text):
        seen.setdefault(spec.canonical_term, spec)
    return tuple(seen.values())


def material_group(value: str) -> Optional[str]:
    """The alloy group a material value belongs to, or None if it is unknown."""
    token = normalise_token(value)
    if not token:
        return None
    vocabulary = load_vocabulary()
    for group, members in _normalised_members(vocabulary_path()).items():
        if token in members:
            return group
    return None


def material_satisfies(requirement: str, capability: str) -> Optional[bool]:
    """True/False when the two materials can be compared, None when unknown.

    A specific alloy satisfies its generic parent (SS316 satisfies "stainless
    steel"). The reverse is not a match: a generic "stainless steel" does not
    confirm SS316, it is only family-level evidence.
    """
    vocabulary = load_vocabulary()
    required = material_group(requirement)
    offered = material_group(capability)
    if required is None or offered is None:
        return None
    if required == offered:
        return True
    if vocabulary.material_parents.get(offered) == required:
        return True
    return False


def material_is_family_level(requirement: str, capability: str) -> bool:
    """True when the capability is a generic parent of the required alloy.

    "stainless steel" against a requirement of "SS316" is family-level evidence:
    uncertain applicability, never a confirmed match (PRD 4).
    """
    vocabulary = load_vocabulary()
    required = material_group(requirement)
    offered = material_group(capability)
    if required is None or offered is None:
        return False
    return required != offered and vocabulary.material_parents.get(required) == offered


def is_mandatory(text: str) -> bool:
    """True when the wording makes the requirement mandatory (default)."""
    lowered = text.lower()
    for marker in optional_markers():
        if re.search(rf"\b{re.escape(marker)}", lowered):
            return False
    for marker in mandatory_markers():
        if re.search(rf"\b{re.escape(marker)}", lowered):
            return True
    return True
