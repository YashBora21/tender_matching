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

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger
from app.services.llm import LLMClient, LLMError

logger = get_logger(__name__)

# The version of the prompt below. It is part of the cache key.
PROMPT_VERSION = "1"

SYSTEM_PROMPT = """You read a manufacturer's document and state what capability it documents.

Given a requirement and a passage, answer:
- "found": true or false. True when the passage states the capability the requirement asks for. False when the passage does not state it - a passage about something else is a false, not a guess.
- "attribute": what the passage states, in lowercase words ("body material", "operating pressure", "size").
- "value": the value the passage states, as a string.
- "unit": the unit, or null.
- "operator": how the value compares to the requirement: eq | ne | gt | gte | lt | lte | between | approx | unknown.
- "model": the product the passage is about, if it names one.
- "quote": the sentence from the passage that states it, copied exactly. Do not paraphrase, do not correct, do not join sentences. Copy the text as it appears.

Answer with JSON only, in this shape:
{"found": true, "attribute": "body material", "value": "SS316", "unit": null, "operator": "eq", "model": "Series 75/76", "quote": "Body of body shall be SS316"}"""


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


@dataclass
class FactResult:
    """What one extraction produced."""

    fact: Optional[ExtractedFact] = None
    error: Optional[str] = None

    @property
    def found(self) -> bool:
        return self.fact is not None and self.fact.found


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
        fact = ExtractedFact(**answer)
    except (LLMError, ValidationError, TypeError) as exc:
        logger.warning("fact extraction failed: %s", exc)
        return FactResult(error=str(exc))
    return FactResult(fact=fact)
