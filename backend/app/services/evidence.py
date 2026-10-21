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


# Markers recognised in a filename, longest first so "compliance" wins over
# "commercial".
_CLASS_MARKERS: tuple[tuple[str, DocumentClass], ...] = (
    ("compliance-schedule", DocumentClass.COMPLIANCE_SCHEDULE),
    ("compliance_schedule", DocumentClass.COMPLIANCE_SCHEDULE),
    ("compliance", DocumentClass.COMPLIANCE_SCHEDULE),
    ("technical-offer", DocumentClass.TECHNICAL_OFFER),
    ("technical_offer", DocumentClass.TECHNICAL_OFFER),
    ("technical-offering", DocumentClass.TECHNICAL_OFFER),
    ("offer", DocumentClass.TECHNICAL_OFFER),
    ("proposal", DocumentClass.TECHNICAL_OFFER),
    ("specification", DocumentClass.SPECIFICATION),
    ("spec", DocumentClass.SPECIFICATION),
    ("datasheet", DocumentClass.DATASHEET),
    ("data-sheet", DocumentClass.DATASHEET),
    ("catalogue", DocumentClass.DATASHEET),
    ("catalog", DocumentClass.DATASHEET),
    # Commercial and pricing documents are not technical evidence.
    ("commercial", DocumentClass.IGNORED),
    ("pricing", DocumentClass.IGNORED),
    ("price", DocumentClass.IGNORED),
    ("quotation", DocumentClass.IGNORED),
    ("quote", DocumentClass.IGNORED),
    ("invoice", DocumentClass.IGNORED),
)

# File types that can hold technical evidence at all.
EVIDENCE_EXTENSIONS = frozenset({".pdf", ".xlsx", ".ifc"})


def classify_document(filename: str) -> DocumentClass:
    """Classify a document by its name.

    A document with no explicit marker falls back on its extension. A
    specification is not technical evidence - it is the requirement being
    checked - and an ignored document is never evidence.
    """
    lowered = filename.lower()
    for marker, document_class in _CLASS_MARKERS:
        if marker in lowered:
            return document_class

    extension = "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    if extension == ".xlsx":
        return DocumentClass.COMPLIANCE_SCHEDULE
    if extension in {".pdf", ".ifc"}:
        return DocumentClass.TECHNICAL_OFFER
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
    candidates.sort(key=lambda item: _rank(item.document_class, order))

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
        best_rank = _rank(best_group[0].document_class, order)
        other_ranks = [
            _rank(group[0].document_class, order) for group in groups[1:]
        ]
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
