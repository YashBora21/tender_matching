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
    "dn_pattern",
    "find_attribute",
    "ip_pattern",
    "is_mandatory",
    "load_vocabulary",
    "material_group",
    "material_is_family_level",
    "material_pattern",
    "material_satisfies",
    "normalise_token",
]

# Mandatory and optional wording.
MANDATORY_MARKERS = (
    "shall",
    "must",
    "mandatory",
    "is required to",
    "shall be",
    "required",
    "compulsory",
    "it is necessary",
)
OPTIONAL_MARKERS = (
    "may",
    "optional",
    "preferably",
    "preferably,",
    "if possible",
    "recommended",
    "desirable",
    "it is preferable",
)


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
    )


@lru_cache(maxsize=4)
def _normalised_members(vocabulary_path: Optional[str]) -> dict[str, frozenset[str]]:
    """Material group members keyed by their normalised form."""
    vocabulary = load_vocabulary(vocabulary_path)
    return {
        group: frozenset(normalise_token(member) for member in members)
        for group, members in vocabulary.material_groups.items()
    }


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


def find_attribute(text: str) -> Optional[AttributeSpec]:
    """Best attribute for a piece of requirement text, or None if unclear."""
    vocabulary = load_vocabulary()
    lowered = text.lower()
    for synonym, spec in _matchers(vocabulary_path()):
        if re.search(rf"\b{re.escape(synonym)}\b", lowered):
            return spec

    material_pattern, certification_pattern, ip_pattern, dn_pattern = _patterns(vocabulary_path())
    if certification_pattern and certification_pattern.search(text):
        return vocabulary.by_term.get("certification")
    if ip_pattern and ip_pattern.search(text):
        return vocabulary.by_term.get("ip_rating")
    if dn_pattern and dn_pattern.search(text):
        return vocabulary.by_term.get("nominal_size")
    if material_pattern and material_pattern.search(text):
        return vocabulary.by_term.get("material_grade")
    return None


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
    for marker in OPTIONAL_MARKERS:
        if re.search(rf"\b{re.escape(marker)}", lowered):
            return False
    for marker in MANDATORY_MARKERS:
        if re.search(rf"\b{re.escape(marker)}", lowered):
            return True
    return True
