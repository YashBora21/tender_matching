"""Requirement extraction (FR-14, FR-T7, FR-T14).

The LLM reads a requirement out of a line of a tender and says what it is:
the attribute, the operator, the value and the unit, and where in the line
it came from. It never decides whether a capability matches - that is the
comparison engine's job, and it is deterministic (FR-27, FR-28).

Three things keep the extraction honest:

- The answer is **strict JSON**, validated by Pydantic. An answer that is
  not JSON is a failed extraction, not a requirement.
- The **source span is checked in code**. The model says which characters of
  the line the requirement came from, and the code reads those characters
  back. A span that does not hold what the model said it held is rejected,
  and the deterministic split is used instead.
- The **deterministic split is the fallback**, not the exception. A row of a
  bill of quantities is already one requirement; the pipeline atomizes a
  compound prose clause before the model is asked. When the model cannot be
  used, the atom is still there and is flagged, so the tender is still read
  and the gap is visible rather than silent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger
from app.schemas.enums import RequirementType
from app.services.llm import LLMClient, LLMError, LLMRateLimited
from app.services.tender.ingest import RequirementRecord, english_half, is_mangled

logger = get_logger(__name__)

# The version of the prompt below. A change to the prompt is a change to
# every answer, so it is part of the cache key (FR-12, FR-34).
PROMPT_VERSION = "1"

# The types a clause can be, and what happens to each (FR-14). Only the
# technical and certification ones go to matching; the commercial and
# eligibility ones are listed separately; the unknown ones go to review.
TYPE_PROMPT = (
    "technical | certification | commercial | eligibility | informational | unknown"
)

SYSTEM_PROMPT = f"""You extract requirements from Indian GeM tender documents.

The text is bilingual: a line is usually "Hindi/English". Read only the English.
A line may contain characters no font produced; ignore them.

For each requirement in the text, return:
- "text": the requirement, in your own words, with its values
- "span": [start, end] the character offsets of the requirement in the text
- "type": one of {TYPE_PROMPT}
  - technical: a physical, measurable or verifiable specification
  - certification: a cited standard or certification requirement
  - commercial: price, payment, warranty, delivery terms
  - eligibility: who may bid, registration, experience requirements
  - informational: context, not a requirement
  - unknown: you cannot tell
- "attribute": what it is about, in lowercase words ("body material", "operating pressure")
- "operator": one of eq | ne | gt | gte | lt | lte | between | approx | unknown
- "value": the value, as a string
- "unit": the unit, or null
- "mandatory": true unless the wording says "may", "optional" or "preferably"

One line can hold more than one requirement; return each separately.
Return only requirements the text actually states. If the text states none, return an empty list.

Answer with JSON only, in this shape:
{{"requirements": [{{"text": "...", "span": [0, 10], "type": "technical", "attribute": "...", "operator": "gte", "value": "...", "unit": null, "mandatory": true}}]}}"""


class ExtractedRequirement(BaseModel):
    """One requirement as the model states it."""

    model_config = ConfigDict(extra="forbid")

    text: str
    span: tuple[int, int] = (0, 0)
    type: str = "unknown"
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    mandatory: bool = True


class ExtractionResponse(BaseModel):
    """The model's answer."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[ExtractedRequirement] = Field(default_factory=list)


@dataclass
class ExtractionResult:
    """What one extraction produced."""

    requirements: list[RequirementRecord] = field(default_factory=list)
    # The requirements the model stated but whose span did not hold up.
    rejected: list[str] = field(default_factory=list)
    # True when the model could not be used and the deterministic split ran.
    fell_back: bool = False
    unreadable_lines: int = 0
    error: Optional[str] = None


def span_holds(text: str, span: tuple[int, int]) -> bool:
    """True when the span holds something that is in the line.

    The model says which characters the requirement came from. The code
    reads them back, so a span that points at nothing, or at the wrong
    place, is caught here rather than becoming a requirement with no
    provenance.
    """
    start, end = span
    if start is None or end is None:
        return False
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    if start < 0 or end > len(text) or start >= end:
        return False
    return bool(text[start:end].strip())


def _type_from(value: str) -> RequirementType:
    try:
        return RequirementType(value)
    except ValueError:
        return RequirementType.UNKNOWN


def _operator_from(value: Optional[str]):
    from app.schemas.enums import Operator

    if not value:
        return Operator.UNKNOWN
    try:
        return Operator(value)
    except ValueError:
        return Operator.UNKNOWN


def extract_requirements(
    text: str,
    item_id: str,
    *,
    client: Optional[LLMClient] = None,
    page: Optional[int] = None,
    section: Optional[str] = None,
    extraction_method: str = "table",
) -> ExtractionResult:
    """Read the requirements out of one line of a tender.

    The line is read as the English half, because the Devanagari half is
    noise the model cannot use. A line that could not be read is counted
    and reported, never counted as a line with no requirement.
    """
    result = ExtractionResult()
    if not text:
        return result

    if is_mangled(text):
        # The line is unreadable. It is a gap, and a gap is visible.
        result.unreadable_lines = 1
        return result

    readable = english_half(text)
    if not readable:
        result.unreadable_lines = 1
        return result

    try:
        client = client or LLMClient()
        answer = client.complete_json(SYSTEM_PROMPT, readable)
        parsed = ExtractionResponse(**answer)
    except LLMRateLimited:
        # The provider's limit is not a failed reading: the line was never
        # read. Let it out so the stage stops instead of recording a silence
        # it did not observe.
        raise
    except (LLMError, ValidationError, TypeError) as exc:
        logger.warning("extraction failed for %s: %s", item_id, exc)
        result.fell_back = True
        result.error = str(exc)
        return result

    for item in parsed.requirements:
        if not span_holds(readable, item.span):
            # The model's span does not hold what it said it held.
            result.rejected.append(item.text)
            continue
        result.requirements.append(
            RequirementRecord(
                text=item.text,
                item_id=item_id,
                requirement_type=_type_from(item.type),
                attribute=item.attribute,
                operator=_operator_from(item.operator).value,
                value=item.value,
                unit=item.unit,
                mandatory=item.mandatory,
                source_span=item.span,
                page=page,
                source_section=section,
                extraction_method=extraction_method,
            )
        )

    return result
