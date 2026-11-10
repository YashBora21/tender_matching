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
    # A comma that introduces a fresh obligation: "..., and shall be tested".
    re.compile(r",\s*(?:and\s+)?(?=(?:shall|must|should)\b)", re.IGNORECASE),
    # "The body shall be SS316 and the trim shall be 13Cr" - a second
    # obligation, recognised by the clause marker that follows it.
    re.compile(
        r"\s+(?:and|whereas)\s+(?=[^,;]{0,60}\b(?:shall|must|should)\b)",
        re.IGNORECASE,
    ),
    # Two sentences on one line. A decimal point is not a sentence break.
    re.compile(r"(?<=[a-z0-9\)\]%])[.!?]\s+(?=[A-Z0-9])"),
    # A blank line is a paragraph break.
    re.compile(r"\n\s*\n"),
)

# A requirement with two or more measurable values is compound.
_MULTIPLE_VALUES = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[A-Za-zµ°%][A-Za-z0-9µ°\^/·\-\.\*]*)"
    r"(?:\s*(?:,|and|/|&)\s*)"
    r"(?:\d+(?:\.\d+)?\s*[A-Za-zµ°%][A-Za-z0-9µ°\^/·\-\.\*]*)",
    re.IGNORECASE,
)


def _clause_hint() -> re.Pattern:
    """The words that mark a clause as carrying its own specification.

    The words live in the vocabulary file, so the atomizer and the normalizer
    agree on what a requirement sounds like.
    """
    markers = attrs.clause_markers()
    if not markers:
        return re.compile(r"\b(?:shall|must|should|be)\b", re.IGNORECASE)
    source = "|".join(re.escape(marker) for marker in markers)
    return re.compile(rf"\b(?:{source})\b", re.IGNORECASE)


def _material_adjunct() -> Optional[re.Pattern]:
    """A bare material grade next to another specification.

    The grades live in the vocabulary file. A second copy of them here would
    be narrower than the vocabulary and would silently miss the grades it
    does not list.
    """
    pattern = attrs.material_pattern()
    if not pattern:
        return None
    return re.compile(rf",\s*(?:{pattern.pattern})", re.IGNORECASE)


@dataclass
class AtomicRequirement:
    """One atomic requirement and where it came from."""

    text: str
    # Character offsets into the source text.
    start: int
    end: int
    # The parent clause this was split out of, if any.
    parent_clause: Optional[str] = None
    # The text the atom was cut from: a section heading when there is one,
    # otherwise the whole clause. It is what makes a short atom retrievable
    # without guessing a subject.
    context: Optional[str] = None
    requirement_type: RequirementType = RequirementType.TECHNICAL
    extraction_method: ExtractionMethod = ExtractionMethod.PROSE
    # True when the atomizer was not sure it had found every part.
    review_required: bool = False
    review_reason: Optional[str] = None

    @property
    def retrieval_text(self) -> str:
        """The text to search with: the atom, with its context when bare.

        An atom that carries its own obligation ("... shall be ...") stands
        alone. A bare value ("1500 RPM", "Class 150") does not say what it is
        about, so the clause it was cut from is searched with it. The marker
        words come from the vocabulary file, so this agrees with the
        normalizer about what a clause sounds like.
        """
        context = (self.context or "").strip()
        if not context or context == self.text.strip():
            return self.text
        lowered = self.text.casefold()
        stands_alone = any(
            re.search(rf"\b{re.escape(marker)}\b", lowered)
            for marker in attrs.clause_markers()
        )
        if stands_alone:
            return self.text
        if self.text.strip().casefold() in context.casefold():
            return context
        return f"{context} {self.text}".strip()


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
    # Two obligations in one piece are two requirements, whatever punctuation
    # separates them: "The body shall be SS316 and the trim shall be 13Cr".
    if len(_clause_hint().findall(piece)) >= 2:
        return True
    adjunct = _material_adjunct()
    if adjunct is not None and adjunct.search(piece):
        return True

    parts = [part.strip() for part in piece.split(",") if part.strip()]
    if len(parts) < 2:
        return False
    # Every part after the first must carry a specification of its own.
    tail = parts[1:]
    return all(_carries_specification(part) for part in tail)


def _carries_specification(part: str) -> bool:
    """True when a clause states a value, a grade or a standard."""
    if _clause_hint().search(part):
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

# The version of the split. A change to it changes what the requirements stage
# produces, so it is part of that stage's key.
ATOMIZER_VERSION = "1"


def _method_of(value: str) -> ExtractionMethod:
    """The extraction method a record names, or prose when it names none."""
    try:
        return ExtractionMethod(value)
    except ValueError:
        return ExtractionMethod.PROSE


def atomize_records(records):
    """Split every compound requirement into atoms, keeping its provenance.

    The parsed clauses are atomized *before* the model reads them, which is
    the order the PRD gives (parser -> atomizer -> type gate -> normalizer).
    Splitting after extraction would leave one attribute and one value on
    several facts, and there is no honest way to say which atom the value
    belongs to. Split first, so every atom is extracted on its own words
    (FR-13, FR-T8).

    A table row is already one parameter and is left alone. A record that is
    already one atom is returned unchanged, so nothing the reader set - a
    page tag, a confidence, a review flag - is lost.
    """
    from app.services.tender.ingest import RequirementRecord

    atomized: list[RequirementRecord] = []
    for record in records:
        method = _method_of(record.extraction_method)
        if method is ExtractionMethod.TABLE or record.source_row_id is not None:
            atomized.append(record)
            continue
        parts = atomize(
            record.text,
            parent_clause=record.source_section,
            extraction_method=method,
            requirement_type=record.requirement_type,
        )
        if len(parts) <= 1:
            atomized.append(record)
            continue
        for part in parts:
            atomized.append(
                RequirementRecord(
                    text=part.text,
                    item_id=record.item_id,
                    requirement_type=record.requirement_type,
                    mandatory=record.mandatory,
                    # The atom's offsets are inside the clause; the clause's
                    # span is the provenance every atom shares.
                    source_span=record.source_span,
                    page=record.page,
                    source_section=record.source_section,
                    extraction_method=ExtractionMethod.PROSE.value,
                    page_tag=record.page_tag,
                    confidence=record.confidence,
                    unreadable=record.unreadable,
                    review_required=record.review_required,
                    review_reason=record.review_reason,
                    source_table_id=record.source_table_id,
                    source_row_id=record.source_row_id,
                )
            )
    return atomized


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
                context=parent_clause or text,
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
        context = f"{parent_clause} {piece}".strip() if parent_clause else piece
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
                        context=context,
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
                context=context,
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
                context=parent_clause or text,
                requirement_type=requirement_type,
                extraction_method=extraction_method,
            )
        )
    return results
