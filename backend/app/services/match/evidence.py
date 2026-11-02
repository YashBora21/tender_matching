"""Evidence consolidation and conflict detection (FR-25).

Facts about the same attribute of the same model come from several documents.
This module decides which one to use:

- Equivalent facts collapse. Repetition does not add confidence.
- Facts that disagree produce a **conflict**, never a silent winner. A conflict
  is only resolved when a document class has a strictly higher priority; a
  disagreement inside the highest-priority class stays a conflict and goes to
  review (FR-25, PRD 10).

The priority order is configuration, not code: `EVIDENCE_PRIORITY` in the
environment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.comparison.units import convert, is_compatible
from app.core.config import get_settings


class DocumentClass(StrEnum):
    """The role a document plays. The value is the priority key."""

    SPECIFICATION = "specification"
    DATASHEET = "datasheet"
    COMPLIANCE_SCHEDULE = "compliance_schedule"
    TECHNICAL_OFFER = "technical_offer"
    IGNORED = "ignored"


DOC_TYPES_PATH = Path(__file__).resolve().parents[3] / "data" / "doc_types.json"


def doc_types_path() -> str:
    """The document-type vocabulary file: the configured one, or the shipped
    default. The attribute and category vocabularies already had a setting; this
    one was only reachable by editing code, and a relocated data directory
    silently produced an empty vocabulary rather than an error."""
    return get_settings().doc_types_path or str(DOC_TYPES_PATH)


@dataclass(frozen=True)
class DocTypeVocabulary:
    """The document-type vocabulary, loaded from the data file.

    The classes, the filename markers and the file extensions live in one
    file, so the collector and the evidence layer cannot disagree about what
    a document type is.
    """

    doc_types: tuple[str, ...] = ()
    class_markers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    evidence_extensions: frozenset[str] = frozenset()
    corpus_extensions: frozenset[str] = frozenset()
    extension_classes: dict[str, str] = field(default_factory=dict)

    @property
    def markers(self) -> tuple[tuple[str, str], ...]:
        """Every filename marker with its class, longest marker first.

        Longest first so "compliance" wins over "commercial": a document
        named "compliance-schedule-commercial-terms" is a compliance
        schedule.
        """
        pairs = [
            (marker, class_name)
            for class_name, markers in self.class_markers.items()
            for marker in markers
        ]
        return tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))


@lru_cache(maxsize=4)
def load_doc_types(path: Optional[str] = None) -> DocTypeVocabulary:
    """Load the document-type vocabulary. A missing or broken file gives an
    empty vocabulary rather than a crash: the corpus is still readable, the
    documents are just not classified."""
    import json

    target = Path(path) if path else Path(doc_types_path())
    if not target.exists():
        return DocTypeVocabulary()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DocTypeVocabulary()

    return DocTypeVocabulary(
        doc_types=tuple(data.get("doc_types", ())),
        class_markers={
            class_name: tuple(markers)
            for class_name, markers in data.get("class_markers", {}).items()
        },
        evidence_extensions=frozenset(data.get("evidence_extensions", ())),
        corpus_extensions=frozenset(data.get("corpus_extensions", ())),
        extension_classes=dict(data.get("extension_classes", {})),
    )


def classify_document(filename: str) -> DocumentClass:
    """Classify a document by its name.

    A document with no explicit marker falls back on its extension. A
    specification is not technical evidence - it is the requirement being
    checked - and an ignored document is never evidence.
    """
    lowered = filename.lower()
    vocabulary = load_doc_types()
    for marker, class_name in vocabulary.markers:
        if marker in lowered:
            try:
                return DocumentClass(class_name)
            except ValueError:
                continue

    extension = "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    class_name = vocabulary.extension_classes.get(extension)
    if class_name:
        try:
            return DocumentClass(class_name)
        except ValueError:
            pass
    return DocumentClass.IGNORED


@dataclass
class Evidence:
    """One documented fact about one attribute of one model."""

    attribute: str
    value: str
    unit: Optional[str] = None
    document_class: DocumentClass = DocumentClass.TECHNICAL_OFFER
    document_id: Optional[str] = None
    page: Optional[int] = None
    quote: Optional[str] = None
    model: Optional[str] = None
    family: Optional[str] = None
    # A distributor or reseller document. It never overrides a manufacturer
    # document of the same class (FR-7).
    is_secondary: bool = False

    @property
    def is_evidence(self) -> bool:
        return self.document_class not in (DocumentClass.SPECIFICATION, DocumentClass.IGNORED)


class Disposition(StrEnum):
    """What happened to one piece of evidence during consolidation."""

    SELECTED = "selected"
    EQUIVALENT_DUPLICATE = "equivalent-duplicate"
    CONFLICT = "conflict"
    LOWER_PRIORITY = "lower-priority"


@dataclass
class AuditEntry:
    """The record of one evidence item's fate (FR-25)."""

    evidence: Evidence
    disposition: Disposition
    priority_rank: int


@dataclass
class Consolidation:
    """The result of consolidating a set of evidence for one attribute."""

    attribute: str
    selected: Optional[Evidence] = None
    conflict: bool = False
    #: The competing values when there is a conflict.
    conflicting_values: list[str] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)
    review_reason: Optional[str] = None


def _numeric(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    match = re.match(r"^-?\d+(?:\.\d+)?$", text.strip())
    return float(match.group(0)) if match else None


def _equivalent(left: Evidence, right: Evidence) -> bool:
    """True when two facts say the same thing.

    Numeric values are equivalent when equal, or equal after a deterministic
    unit conversion. Everything else is compared as normalised text.
    """
    left_number = _numeric(left.value)
    right_number = _numeric(right.value)
    if left_number is not None and right_number is not None:
        if left.unit and right.unit and left.unit != right.unit:
            if not is_compatible(left.unit, right.unit):
                return False
            right_number = convert(right_number, right.unit, left.unit)
        return abs(left_number - right_number) <= max(abs(left_number), abs(right_number)) * 1e-9
    return left.value.strip().lower() == right.value.strip().lower()


def _priority_order() -> list[DocumentClass]:
    """The configured priority order, highest first."""
    order: list[DocumentClass] = []
    for name in get_settings().evidence_priority_list:
        try:
            order.append(DocumentClass(name))
        except ValueError:
            continue
    return order


def _rank(document_class: DocumentClass, order: list[DocumentClass]) -> int:
    """The priority rank of a class. Unlisted classes rank below all listed ones."""
    if document_class in order:
        return order.index(document_class)
    return len(order)


def _sort_key(item: Evidence, order: list[DocumentClass]) -> tuple[int, int]:
    """Class first, then a manufacturer document ahead of a distributor one.

    The secondary flag only separates documents of the *same* class: a
    distributor datasheet does not outrank a manufacturer specification
    (FR-7). Within a class, the manufacturer's own document wins.
    """
    return (_rank(item.document_class, order), 1 if item.is_secondary else 0)


def consolidate(attribute: str, evidence: list[Evidence]) -> Consolidation:
    """Consolidate the evidence for one attribute into a single outcome.

    Equivalent facts collapse onto the first one. Facts that disagree produce
    a conflict, which is only resolved when a strictly higher-priority class
    says otherwise.
    """
    result = Consolidation(attribute=attribute)
    order = _priority_order()
    candidates = [item for item in evidence if item.is_evidence]
    if not candidates:
        return result

    # Sort by priority, then by document order so the outcome is deterministic.
    candidates.sort(key=lambda item: _sort_key(item, order))

    groups: list[list[Evidence]] = []
    for item in candidates:
        for group in groups:
            if _equivalent(group[0], item):
                group.append(item)
                break
        else:
            groups.append([item])

    # Every disposition is decided before any audit entry is written, so each
    # fact appears in the audit exactly once. The list is parallel to
    # `candidates`, which the audit is written in.
    dispositions: list[Disposition] = [Disposition.CONFLICT] * len(candidates)
    index_of = {id(item): position for position, item in enumerate(candidates)}

    def mark(item: Evidence, disposition: Disposition) -> None:
        dispositions[index_of[id(item)]] = disposition

    if len(groups) == 1:
        group = groups[0]
        result.selected = group[0]
        mark(group[0], Disposition.SELECTED)
        for item in group[1:]:
            mark(item, Disposition.EQUIVALENT_DUPLICATE)
    else:
        # The groups disagree. The highest-priority class wins, but only if it
        # is strictly higher than every other class involved.
        best_group = groups[0]
        best_rank = _sort_key(best_group[0], order)
        other_ranks = [_sort_key(group[0], order) for group in groups[1:]]
        if all(best_rank < other for other in other_ranks):
            result.selected = best_group[0]
            mark(best_group[0], Disposition.SELECTED)
            for item in best_group[1:]:
                mark(item, Disposition.EQUIVALENT_DUPLICATE)
            for group in groups[1:]:
                for item in group:
                    mark(item, Disposition.LOWER_PRIORITY)
        else:
            # A disagreement inside the highest-priority class: a conflict,
            # not a guess.
            result.conflict = True
            result.conflicting_values = [group[0].value for group in groups]
            result.review_reason = (
                "documents disagree about this attribute: "
                + ", ".join(
                    f"{group[0].value} ({group[0].document_class.value})" for group in groups
                )
            )
            for group in groups:
                for item in group:
                    mark(item, Disposition.CONFLICT)

    for position, item in enumerate(candidates):
        result.audit.append(
            AuditEntry(
                evidence=item,
                disposition=dispositions[position],
                priority_rank=_rank(item.document_class, order),
            )
        )
    return result
