"""Requirement atomizer (FR-13).

Splits a compound line into atomic requirements, each keeping a link to the
text it came from. "Body shall be SS316, protection IP68, speed 1500 RPM"
becomes three requirements, not one.

A table row that is already atomic is not split again: the caller marks it as
a table extraction and this module leaves it alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.comparison import attributes as attrs
from app.schemas.enums import ExtractionMethod, RequirementType

# Separators that reliably end one requirement and start the next.
_SPLIT_PATTERNS = (
    # "a) body SS316  b) trim 13Cr" - a lettered sub-clause.
    re.compile(r"(?:\s*[;,]\s*|\s+)(?=[a-z]\)\s)", re.IGNORECASE),
    # "1) ... 2) ..." - a numbered sub-clause.
    re.compile(r"(?:\s*[;,]\s*|\s+)(?=\d{1,2}[.)]\s)"),
    # "and" / "with" / "plus" between two values, but not inside a word.
    re.compile(r",\s+(?:and|with|plus)\s+", re.IGNORECASE),
    # A semicolon or ";" between two clauses.
    re.compile(r"\s*;\s*"),
)

# A clause that carries its own comparison is atomic on its own.
_CLAUSE_HINT = re.compile(
    r"\b(?:shall|must|should|be|not\s+less|not\s+more|min(?:imum)?|max(?:imum)?|"
    r"at\s+least|at\s+most|>=|<=|≥|≤|%|rated|providing|having|with)\b",
    re.IGNORECASE,
)

# A requirement with two or more measurable values is compound.
_MULTIPLE_VALUES = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[A-Za-zµ°%][A-Za-z0-9µ°\^/·\-\.\*]*)"
    r"(?:\s*(?:,|and|/|&)\s*)"
    r"(?:\d+(?:\.\d+)?\s*[A-Za-zµ°%][A-Za-z0-9µ°\^/·\-\.\*]*)",
    re.IGNORECASE,
)

# A bare material grade next to another specification.
_MATERIAL_ADJUNCT = re.compile(
    r",\s*(?:SS\s?\d{3}[L]?|CF8M|CF8|AISI\s?\d{3,4}|stainless\s+steel|"
    r"carbon\s+steel|bronze|brass|monel|ptfe|epdm|nbr|viton)\b",
    re.IGNORECASE,
)


@dataclass
class AtomicRequirement:
    """One atomic requirement and where it came from."""

    text: str
    # Character offsets into the source text.
    start: int
    end: int
    # The parent clause this was split out of, if any.
    parent_clause: Optional[str] = None
    requirement_type: RequirementType = RequirementType.TECHNICAL
    extraction_method: ExtractionMethod = ExtractionMethod.PROSE
    # True when the atomizer was not sure it had found every part.
    review_required: bool = False
    review_reason: Optional[str] = None


def _split_spans(text: str) -> list[tuple[int, int]]:
    """Split the text into spans, keeping the original offsets.

    Every pattern in the list is tried against every span. A pattern that
    matched nothing on one span says nothing about the next pattern, so the
    list is always run to completion - otherwise a rule that happens to come
    after a non-matching one is dead code.
    """
    spans = [(0, len(text))]
    for pattern in _SPLIT_PATTERNS:
        new_spans: list[tuple[int, int]] = []
        for start, end in spans:
            chunk = text[start:end]
            last = 0
            for match in pattern.finditer(chunk):
                cut = match.start()
                if cut == 0:
                    continue
                piece = chunk[last:cut]
                if piece.strip():
                    new_spans.append((start + last, start + cut))
                last = match.end()
            piece = chunk[last:]
            if piece.strip():
                new_spans.append((start + last, end))
        spans = new_spans

    return [(s, e) for s, e in spans if text[s:e].strip()]


def _is_compound(piece: str) -> bool:
    """True when a piece still holds more than one requirement.

    Structural, not keyword-based: the piece is compound when it is a
    comma-separated list whose parts each carry a specification of their own.
    "Body shall be SS316, protection IP68, speed 1500 RPM" is three; "Body
    shall be SS316" is one.
    """
    if _MULTIPLE_VALUES.search(piece):
        return True
    if _MATERIAL_ADJUNCT.search(piece):
        return True

    parts = [part.strip() for part in piece.split(",") if part.strip()]
    if len(parts) < 2:
        return False
    # Every part after the first must carry a specification of its own.
    tail = parts[1:]
    return all(_carries_specification(part) for part in tail)


def _carries_specification(part: str) -> bool:
    """True when a clause states a value, a grade or a standard."""
    if _CLAUSE_HINT.search(part):
        return True
    if re.search(r"\d", part):
        return True
    for pattern in (
        attrs.material_pattern(),
        attrs.certification_pattern(),
        attrs.ip_pattern(),
    ):
        if pattern and pattern.search(part):
            return True
    return False


def _split_again(piece: str, offset: int) -> list[tuple[int, int]]:
    """Split a still-compound piece on its remaining commas."""
    spans: list[tuple[int, int]] = []
    last = 0
    for match in re.finditer(r"\s*,\s*", piece):
        cut = match.start()
        if cut == 0:
            continue
        part = piece[last:cut]
        if part.strip():
            spans.append((offset + last, offset + cut))
        last = match.end()
    tail = piece[last:]
    if tail.strip():
        spans.append((offset + last, offset + len(piece)))
    return spans


# A section heading that introduces a list of sub-clauses: "3.2 Materials:".
_HEADING = re.compile(r"^\s*[\d.]+[A-Za-z]?\s+[A-Za-z][A-Za-z /&()-]{0,60}:\s*$")


def atomize(
    text: str,
    *,
    parent_clause: Optional[str] = None,
    extraction_method: ExtractionMethod = ExtractionMethod.PROSE,
    requirement_type: RequirementType = RequirementType.TECHNICAL,
) -> list[AtomicRequirement]:
    """Split a requirement into atomic requirements.

    A table row is left as one requirement: the table pipeline already emits
    one row per parameter (FR-T6), and re-splitting it would break the row
    provenance.
    """
    text = text.strip()
    if not text:
        return []

    if extraction_method is ExtractionMethod.TABLE:
        return [
            AtomicRequirement(
                text=text,
                start=0,
                end=len(text),
                parent_clause=parent_clause,
                requirement_type=requirement_type,
                extraction_method=extraction_method,
            )
        ]

    results: list[AtomicRequirement] = []
    for start, end in _split_spans(text):
        piece = text[start:end].strip()
        if not piece:
            continue
        # A heading introduces the sub-clauses that follow it: it is their
        # parent clause, not a requirement of its own.
        if _HEADING.match(piece) and parent_clause is None:
            parent_clause = piece.rstrip(":").strip()
            continue
            continue
        if _is_compound(piece):
            for sub_start, sub_end in _split_again(piece, start):
                sub = text[sub_start:sub_end].strip()
                if not sub:
                    continue
                results.append(
                    AtomicRequirement(
                        text=sub,
                        start=sub_start,
                        end=sub_end,
                        parent_clause=parent_clause,
                        requirement_type=requirement_type,
                        extraction_method=extraction_method,
                    )
                )
            continue
        results.append(
            AtomicRequirement(
                text=piece,
                start=start,
                end=end,
                parent_clause=parent_clause,
                requirement_type=requirement_type,
                extraction_method=extraction_method,
            )
        )

    if not results:
        results.append(
            AtomicRequirement(
                text=text,
                start=0,
                end=len(text),
                parent_clause=parent_clause,
                requirement_type=requirement_type,
                extraction_method=extraction_method,
            )
        )
    return results
