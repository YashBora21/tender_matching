"""The category a bill-of-quantities item sits in (FR-16, FR-T9).

A tender's own category is a hint, not an answer: one tender carries a gate
valve, a pump and a box of spares, and a requirement is only ever compared
against models in the category of the item it belongs to. So the category is
inferred per item, from the words of the item itself.

The inference is deterministic and comes from a data file
(`backend/data/categories.json`), not from a list in this module: adding a
category is an edit to that file (FR-6). Nothing here consults the LLM.

Two outcomes are *not* a category:

- **no term matched** - the item names nothing the vocabulary knows, and the
  requirement is not filtered by a category it does not have;
- **two categories matched equally** - the item names more than one, and
  guessing which one the buyer meant is exactly what FR-T9 forbids.

Both come back as `unknown` with `ambiguous` set, so the caller can flag the
item for review instead of quietly picking one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from app.core.config import get_settings

__all__ = [
    "Category",
    "CategoryGuess",
    "ProductType",
    "TypeGuess",
    "categories_path",
    "category_names",
    "category_types",
    "document_type",
    "infer_category",
    "infer_type",
    "load_categories",
    "mentioned_types",
]

DEFAULT_CATEGORIES_PATH = Path(__file__).resolve().parents[2] / "data" / "categories.json"

# What an item's category is when the vocabulary cannot say. It is a value and
# not a null so that the item is visibly unresolved rather than silently
# unfiltered.
UNKNOWN = "unknown"


def categories_path() -> str:
    """The vocabulary file to load: the configured one, or the shipped default."""
    return get_settings().category_vocabulary_path or str(DEFAULT_CATEGORIES_PATH)


@dataclass(frozen=True)
class ProductType:
    """One type inside a category, and the terms that name it.

    A gate valve and a ball valve are both `valves`, but a gate-valve
    requirement is not answered by a ball-valve datasheet. The type is the
    finer scope, and it lives in the same data file as the category.

    `families` are the product families the corpus declares for this type
    ("D-Series", "Series 7/8"). A catalogue names its family on the cover and
    rarely repeats the type on every page, so the family is how a page that
    never says "ball valve" can still be placed (D-100).
    """

    name: str
    terms: tuple[str, ...]
    families: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    """One product category and the terms that name it."""

    name: str
    terms: tuple[str, ...]
    types: tuple[ProductType, ...] = ()


@dataclass(frozen=True)
class CategoryGuess:
    """What an item's words say about its category."""

    category: str = UNKNOWN
    # The term that decided it, for the report: "gate valve".
    term: Optional[str] = None
    # True when the item names two categories equally, or the vocabulary
    # could not be read at all. An ambiguous item is flagged, not guessed.
    ambiguous: bool = False
    reason: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.category != UNKNOWN


@dataclass(frozen=True)
class TypeGuess:
    """What an item's words say about its type inside a category."""

    product_type: Optional[str] = None
    # The term that decided it, for the report: "gate valve".
    term: Optional[str] = None
    # True when the item names two types equally. An ambiguous type is not
    # filtered on, and is never guessed (FR-T9).
    ambiguous: bool = False
    reason: Optional[str] = None

    @property
    def known(self) -> bool:
        return bool(self.product_type)


def _normalise(text: str) -> str:
    """Lowercase, with every run of punctuation and space collapsed to one space."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@lru_cache(maxsize=256)
def _term_pattern(term: str) -> re.Pattern[str]:
    """A whole-word match for a vocabulary term.

    A bill of quantities writes "GATE VALVES", a catalogue writes "gate
    valve". The trailing plural is the same word, so it is matched here rather
    than written twice in the vocabulary: a category is added by naming it
    once.
    """
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(term) + r"(?:e?s)?(?![a-z0-9])"
    )


def _mentions(text: str, term: str) -> bool:
    return _term_pattern(term).search(text) is not None


@lru_cache(maxsize=4)
def _load(target: str) -> tuple[Category, ...]:
    path = Path(target)
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    categories: list[Category] = []
    for item in data.get("categories", ()):
        name = str(item.get("name") or "").strip()
        terms = tuple(
            term
            for term in (_normalise(t) for t in item.get("terms", ()))
            if term
        )
        types = tuple(
            ProductType(
                name=str(kind.get("name") or "").strip(),
                terms=tuple(
                    term
                    for term in (_normalise(t) for t in kind.get("terms", ()))
                    if term
                ),
                families=tuple(
                    family
                    for family in (_normalise(t) for t in kind.get("families", ()))
                    if family
                ),
            )
            for kind in item.get("types", ())
        )
        types = tuple(kind for kind in types if kind.name and kind.terms)
        if name and terms:
            categories.append(Category(name=name, terms=terms, types=types))
    return tuple(categories)


def load_categories(path: Optional[str] = None) -> tuple[Category, ...]:
    """The category vocabulary. A missing or broken file gives no categories.

    An empty vocabulary is not an error: nothing is classified, every item is
    `unknown` and therefore unfiltered, and the run still answers requirements.
    A crash here would turn a missing data file into a failed run.
    """
    return _load(str(Path(path) if path else categories_path()))


def category_names(categories: Optional[Iterable[Category]] = None) -> frozenset[str]:
    """The names the vocabulary knows. Used to tell a real category from a
    bucket that is not one (a document tagged with its manufacturer's slug)."""
    return frozenset(c.name for c in (categories if categories is not None else load_categories()))


def infer_category(
    *parts: Optional[str],
    categories: Optional[tuple[Category, ...]] = None,
) -> CategoryGuess:
    """The category an item's own words name, or `unknown`.

    The most specific term wins: an item titled "motor operated gate valve"
    is a valve, because "gate valve" is longer than "motor" and says more
    about the thing being bought. When the longest terms of two categories
    are the same length, the item names both and there is no answer to guess.
    """
    vocabulary = categories if categories is not None else load_categories()
    text = " " + _normalise(" ".join(part or "" for part in parts)) + " "
    if not text.strip() or not vocabulary:
        return CategoryGuess(reason="the category vocabulary is empty")

    best: dict[str, str] = {}
    for category in vocabulary:
        matched = [term for term in category.terms if _mentions(text, term)]
        if matched:
            best[category.name] = max(matched, key=len)

    if not best:
        return CategoryGuess(reason="the item names no product category")

    ranked = sorted(best.items(), key=lambda pair: len(pair[1]), reverse=True)
    winner, term = ranked[0]
    tied = sorted({name for name, matched in ranked if len(matched) == len(term)})
    if len(tied) > 1:
        return CategoryGuess(
            ambiguous=True,
            reason=(
                "the item names more than one category equally: "
                + ", ".join(tied)
            ),
        )
    return CategoryGuess(category=winner, term=term)


def category_types(
    category: str, categories: Optional[Iterable[Category]] = None
) -> tuple[ProductType, ...]:
    """The types a category knows, from the same data file."""
    for entry in categories if categories is not None else load_categories():
        if entry.name == category:
            return entry.types
    return ()


def infer_type(
    category: Optional[str],
    *parts: Optional[str],
    categories: Optional[Iterable[Category]] = None,
) -> TypeGuess:
    """The type an item's own words name inside its category, or none.

    The most specific term wins, exactly as it does for a category: "wedge
    gate valve" says more than "gate valve". When two types are named equally
    the answer is ambiguous, and the caller must not filter on a guess
    (FR-T9).

    A product family ("D-Series", "Series 7/8") is a type too, and it is
    matched the same way: a document that names only its family has still
    said what it is (D-100).
    """
    if not category:
        return TypeGuess(reason="the item's category is not known")
    vocabulary = category_types(category, categories)
    if not vocabulary:
        return TypeGuess(reason=f"the {category} category names no types")
    text = " " + _normalise(" ".join(part or "" for part in parts)) + " "
    if not text.strip():
        return TypeGuess(reason="the item names no product type")

    best: dict[str, str] = {}
    for kind in vocabulary:
        matched = [
            term
            for term in (*kind.terms, *kind.families)
            if _mentions(text, term)
        ]
        if matched:
            best[kind.name] = max(matched, key=len)

    if not best:
        return TypeGuess(reason="the item names no product type")

    ranked = sorted(best.items(), key=lambda pair: len(pair[1]), reverse=True)
    winner, term = ranked[0]
    tied = sorted({name for name, matched in ranked if len(matched) == len(term)})
    if len(tied) > 1:
        return TypeGuess(
            ambiguous=True,
            reason="the item names more than one type equally: " + ", ".join(tied),
        )
    return TypeGuess(product_type=winner, term=term)


def document_type(
    category: Optional[str],
    *parts: Optional[str],
    categories: Optional[Iterable[Category]] = None,
) -> TypeGuess:
    """The type a *document* declares for itself (D-100).

    A datasheet page says "150" and the column header says "Pressure class";
    the page rarely repeats "ball valve". The cover does: the document's
    declared model or family, and its file name, are what place the whole
    document in a type. This is the same inference the item title gets, so a
    document and an item are compared in one vocabulary.

    An unknown answer is not a different type: the caller keeps the document
    and treats the evidence as family-level (PRD 4).
    """
    return infer_type(category, *parts, categories=categories)


def mentioned_types(
    category: Optional[str],
    *parts: Optional[str],
    categories: Optional[Iterable[Category]] = None,
) -> frozenset[str]:
    """Every type the text positively names.

    This is what lets retrieval tell *silent* from *different*. A chunk that
    never says which kind of valve it is may still be about the wanted kind;
    a chunk that says "ball valve" is not evidence for a gate valve. Silence
    is not a different type (PRD 4).
    """
    if not category:
        return frozenset()
    text = " " + _normalise(" ".join(part or "" for part in parts)) + " "
    if not text.strip():
        return frozenset()
    return frozenset(
        kind.name
        for kind in category_types(category, categories)
        if any(_mentions(text, term) for term in kind.terms)
    )
