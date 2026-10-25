"""Fact extraction (FR-22).

The LLM reads a passage from the corpus and says what capability the passage
states: the attribute, the value, the unit, and the sentence that says it.

The quote is the important part. The model is asked to copy it, and the
verifier reads it back off the page (FR-23). A model that paraphrases
instead of quoting produces a claim nobody can check, and the citation layer
rejects it.

The answer is strict JSON, validated by Pydantic. An answer that is not JSON
is a failed extraction, never a capability that was never stated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger
from app.services.llm import LLMClient, LLMError

logger = get_logger(__name__)

# The version of the prompt below. It is part of the cache key.
PROMPT_VERSION = "1"

SYSTEM_PROMPT = """You read a manufacturer's document and state what capability it documents.

The passage is often a table row or a fragment of one. A row states the
values in its columns: "1 | BODY | A216 WCB, WCC/ A352 LCB, LCC/A351 CF8M"
states the body material. Read it that way.

Given a requirement and a passage, answer:
- "found": true or false. True when the passage states a capability the requirement asks about. False when the passage is about something else - a passage that does not answer the question is a false, not a guess.
- "attribute": what the passage states, in lowercase words ("body material", "operating pressure", "size", "face to face distance").
- "value": the value the passage states, as a string. A material grade is the grade: "CF8M", "A216 WCB". A dimension is the number: "267".
- "unit": the unit, or null.
- "operator": how the value compares to the requirement: eq | ne | gt | gte | lt | lte | between | approx | unknown.
- "model": the product the passage is about, if it names one.
- "quote": the text from the passage that states it, copied exactly. Do not paraphrase, do not correct, do not join sentences. Copy the text as it appears.

The passage does not have to use the requirement's words. "SS316" and "CF8M"
are the same alloy, and a passage that states one states the other.

One requirement can ask for more than one thing, and a passage can state more
than one thing. Return one entry per capability the passage states, and the
"requirements" key holds them all.

Answer with JSON only, in this shape:
{"requirements": [{"found": true, "attribute": "body material", "value": "CF8M", "unit": null, "operator": "eq", "model": "Series 75/76", "quote": "1 | BODY | A216 WCB, WCC/ A352 LCB, LCC/A351 CF8M"}]}"""


class ExtractedFact(BaseModel):
    """One candidate capability, as the model states it."""

    model_config = ConfigDict(extra="forbid")

    found: bool = False
    attribute: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    operator: Optional[str] = None
    model: Optional[str] = None
    quote: Optional[str] = None


class ExtractionResponse(BaseModel):
    """The model's answer: the capabilities the passage states."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[ExtractedFact] = Field(default_factory=list)


@dataclass
class FactResult:
    """What one extraction produced."""

    facts: list[ExtractedFact] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return any(fact.found for fact in self.facts)


def extract_fact(
    requirement_text: str,
    passage: str,
    *,
    client: Optional[LLMClient] = None,
) -> FactResult:
    """Read a passage and say what capability it states."""
    if not passage.strip():
        return FactResult(error="the passage is empty")

    user = f"REQUIREMENT:\n{requirement_text}\n\nPASSAGE:\n{passage}"
    try:
        client = client or LLMClient()
        answer = client.complete_json(SYSTEM_PROMPT, user)
        parsed = ExtractionResponse(**answer)
    except (LLMError, ValidationError, TypeError) as exc:
        logger.warning("fact extraction failed: %s", exc)
        return FactResult(error=str(exc))
    return FactResult(facts=list(parsed.requirements))
