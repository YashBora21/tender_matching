"""Attribute vocabulary loaded from a data file (FR-6, FR-27).

The vocabulary is data, not code: a JSON file lists the canonical attributes,
their synonyms, their value types and their default units, plus the material
grade groups. Adding a category or a new attribute is an edit to that file, not
to a Python module (FR-6 - "adding a new category means adding a URL list; no
code changes").

The file ships with the repository as the default vocabulary. A deployment can
point `ATTRIBUTE_VOCABULARY_PATH` at its own file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.schemas.enums import ValueType

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VOCABULARY_PATH = REPO_ROOT / "backend" / "data" / "attribute_vocabulary.json"


def vocabulary_path() -> str:
    """The vocabulary file to load: the configured one, or the shipped default."""
    return get_settings().attribute_vocabulary_path or str(DEFAULT_VOCABULARY_PATH)


@dataclass(frozen=True)
class AttributeSpec:
    """One canonical attribute and how to recognise it in free text."""

    canonical_term: str
    value_type: ValueType
    synonyms: tuple[str, ...] = ()
    default_unit: Optional[str] = None


@dataclass(frozen=True)
class Vocabulary:
    """The loaded vocabulary: attributes, material groups and the patterns."""

    attributes: tuple[AttributeSpec, ...] = ()
    material_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    material_parents: dict[str, str] = field(default_factory=dict)
    # Regex sources for the value shapes, kept in the data file so they can be
    # extended without touching the code.
    material_pattern: str = ""
    certification_pattern: str = ""
    ip_pattern: str = ""
    dn_pattern: str = ""

    @property
    def by_term(self) -> dict[str, AttributeSpec]:
        return {spec.canonical_term: spec for spec in self.attributes}


def normalise_token(text: str) -> str:
    """Lower-case and strip punctuation, so `SS-316` and `ss 316` compare equal."""
    return re.sub(r"[^a-z0-9.]+", "", text.lower().replace(" ", ""))


@lru_cache(maxsize=4)
def load_vocabulary(path: Optional[str] = None) -> Vocabulary:
    """Load the vocabulary from disk. Missing or broken file gives an empty vocabulary."""
    target = Path(path) if path else Path(vocabulary_path())
    if not target.exists():
        return Vocabulary()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Vocabulary()

    attributes = tuple(
        AttributeSpec(
            canonical_term=item["canonical_term"],
            value_type=ValueType(item["value_type"]),
            synonyms=tuple(item.get("synonyms", ())),
            default_unit=item.get("default_unit"),
        )
        for item in data.get("attributes", ())
    )

    material_groups = {
        group: frozenset(members)
        for group, members in data.get("material_groups", {}).items()
    }

    return Vocabulary(
        attributes=attributes,
        material_groups=material_groups,
        material_parents=dict(data.get("material_parents", {})),
        material_pattern=data.get("material_pattern", ""),
        certification_pattern=data.get("certification_pattern", ""),
        ip_pattern=data.get("ip_pattern", ""),
        dn_pattern=data.get("dn_pattern", ""),
    )
