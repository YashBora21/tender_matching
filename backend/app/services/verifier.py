"""Four-layer verifier (FR-23, FR-24).

1. **Citation validity** - the quote exists verbatim (whitespace-normalized) on
   the cited page. This layer is deterministic and is a hard gate.
2. **Entity validity** - the passage refers to the manufacturer and the model
   or family being evaluated.
3. **Semantic validity** - the passage supports the claimed attribute and value.
4. **Comparison validity** - units, operator and conditions are consistent and
   convertible.

Any failure yields `needs_review` with the reason. A failed verification is
never published as a confirmed match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from app.comparison.units import is_compatible
from app.schemas.enums import Applicability, VerificationOutcome


class Layer(IntEnum):
    CITATION = 1
    ENTITY = 2
    SEMANTIC = 3
    COMPARISON = 4


@dataclass
class LayerResult:
    layer: Layer
    passed: bool
    reason: str
    # True when the layer is unsure, which also forces review (PRD 9).
    borderline: bool = False


@dataclass
class VerificationReport:
    """The outcome of all four layers."""

    layers: list[LayerResult] = field(default_factory=list)
    applicability: Applicability = Applicability.MODEL_LEVEL

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.layers)

    @property
    def failed_layer(self) -> Optional[Layer]:
        for result in self.layers:
            if not result.passed:
                return result.layer
        return None

    @property
    def reasons(self) -> list[str]:
        return [result.reason for result in self.layers if not result.passed]

    @property
    def borderline(self) -> bool:
        return any(result.borderline for result in self.layers)

    @property
    def outcome(self) -> VerificationOutcome:
        if not self.passed:
            return VerificationOutcome.FAILED
        if self.applicability is Applicability.FAMILY_LEVEL:
            return VerificationOutcome.FAMILY_LEVEL
        return VerificationOutcome.PASSED

    @property
    def review_required(self) -> bool:
        return not self.passed or self.borderline


def normalise_whitespace(text: str) -> str:
    """Collapse all whitespace so a re-wrapped quote still matches."""
    return re.sub(r"\s+", " ", text).strip()


def check_citation(quote: Optional[str], page_text: Optional[str]) -> LayerResult:
    """Layer 1: the quote must exist on the cited page.

    The quote is matched whitespace-normalized (FR-23). A quote that is longer
    than the page cannot be verified, and an empty quote is a failure, not a
    pass: an unverifiable claim is exactly what this layer exists to catch.
    """
    if not quote or not quote.strip():
        return LayerResult(Layer.CITATION, False, "the claim carries no quote to verify")
    if page_text is None:
        return LayerResult(Layer.CITATION, False, "the cited page has no text to check the quote against")

    needle = normalise_whitespace(quote)
    haystack = normalise_whitespace(page_text)
    if not needle:
        return LayerResult(Layer.CITATION, False, "the claim carries no quote to verify")
    if needle in haystack:
        return LayerResult(Layer.CITATION, True, "the quote appears verbatim on the cited page")

    # A quote that spans a line break inside a table cell still matches once
    # the cell separator is normalised.
    collapsed = re.sub(r"\s*\|\s*", " ", haystack)
    if needle in collapsed:
        return LayerResult(Layer.CITATION, True, "the quote appears on the cited page")

    # Quote longer than the page can never match; say so plainly.
    if len(needle) > len(haystack):
        return LayerResult(
            Layer.CITATION, False, "the quote is longer than the cited page"
        )
    return LayerResult(
        Layer.CITATION, False, "the quote was not found on the cited page"
    )


def check_entity(
    passage: Optional[str],
    manufacturer: Optional[str],
    model: Optional[str],
    family: Optional[str] = None,
) -> LayerResult:
    """Layer 2: the passage must be about this manufacturer and model or family.

    A model or family name in the passage identifies the product. The
    manufacturer's name does not have to appear: a datasheet page saying
    "GV series body is SS316" is about the manufacturer whose catalogue it is,
    and the page it came from is already provenance.

    When the model is named the evidence is model-level. When only the family is
    named the evidence is family-level: uncertain applicability, not a confirmed
    match (PRD 4).
    """
    if not passage:
        return LayerResult(Layer.ENTITY, False, "the passage is empty")

    lowered = passage.lower()

    model_named = bool(model) and model.lower() in lowered
    family_named = bool(family) and family.lower() in lowered

    if model_named:
        return LayerResult(Layer.ENTITY, True, "the passage names the model under test")
    if family_named:
        return LayerResult(
            Layer.ENTITY,
            True,
            f"the passage names the {family} family, not the specific model",
            borderline=True,
        )

    # No model or family identified the passage. The manufacturer name is then
    # the only provenance signal available.
    if manufacturer:
        if manufacturer.lower() in lowered:
            return LayerResult(Layer.ENTITY, True, "the passage names the manufacturer")
        return LayerResult(
            Layer.ENTITY, False, f"the passage does not mention {manufacturer}"
        )
    if model or family:
        return LayerResult(
            Layer.ENTITY, False, "the passage does not identify the model or family"
        )
    return LayerResult(Layer.ENTITY, True, "no model was specified to check against")


def check_semantic(
    passage: Optional[str],
    attribute: Optional[str],
    value: Optional[str],
    unit: Optional[str] = None,
) -> LayerResult:
    """Layer 3: the passage must actually support the claimed attribute and value.

    This is a containment check, not an LLM judgement: the claimed value has to
    appear in the passage near the attribute. It is deliberately conservative -
    it fails rather than approves something it cannot see.
    """
    if not passage:
        return LayerResult(Layer.SEMANTIC, False, "the passage is empty")
    if not attribute:
        return LayerResult(Layer.SEMANTIC, False, "the claim names no attribute to support")
    if not value:
        return LayerResult(Layer.SEMANTIC, False, "the claim names no value to support")

    lowered = passage.lower()
    value_text = value.strip().lower()

    # A numeric claim: the number must appear in the passage.
    numeric = re.match(r"^-?\d+(?:\.\d+)?$", value_text)
    if numeric:
        # Compare without trailing zeros: 1500 vs 1500.0.
        candidates = {value_text, value_text.rstrip("0").rstrip("."), f"{float(value_text):g}"}
        if any(candidate in lowered for candidate in candidates):
            return LayerResult(Layer.SEMANTIC, True, "the passage contains the claimed value")
        return LayerResult(
            Layer.SEMANTIC, False, f"the passage does not contain the claimed value {value}"
        )

    # A textual claim: the value must appear in the passage.
    if value_text in lowered:
        return LayerResult(Layer.SEMANTIC, True, "the passage contains the claimed value")
    return LayerResult(
        Layer.SEMANTIC, False, f"the passage does not contain the claimed value {value}"
    )


def check_comparison(
    requirement_unit: Optional[str],
    capability_unit: Optional[str],
    operator_is_known: bool = True,
    convertible: Optional[bool] = None,
) -> LayerResult:
    """Layer 4: units, operator and conditions must be consistent."""
    if not operator_is_known:
        return LayerResult(
            Layer.COMPARISON, False, "the requirement's operator could not be determined"
        )
    if requirement_unit and capability_unit:
        if convertible is None:
            convertible = is_compatible(requirement_unit, capability_unit)
        if not convertible:
            return LayerResult(
                Layer.COMPARISON,
                False,
                f"{requirement_unit} and {capability_unit} are not convertible",
            )
        return LayerResult(Layer.COMPARISON, True, "the units are convertible")
    if requirement_unit and not capability_unit:
        return LayerResult(
            Layer.COMPARISON, False, "the capability has no unit, so it cannot be compared"
        )
    return LayerResult(Layer.COMPARISON, True, "no unit conflict")


def verify(
    *,
    quote: Optional[str],
    page_text: Optional[str],
    passage: Optional[str],
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    family: Optional[str] = None,
    attribute: Optional[str] = None,
    value: Optional[str] = None,
    requirement_unit: Optional[str] = None,
    capability_unit: Optional[str] = None,
    operator_is_known: bool = True,
    convertible: Optional[bool] = None,
) -> VerificationReport:
    """Run all four layers and return the report."""
    report = VerificationReport()
    report.layers.append(check_citation(quote, page_text))
    report.layers.append(check_entity(passage, manufacturer, model, family))
    report.layers.append(check_semantic(passage, attribute, value))
    report.layers.append(
        check_comparison(requirement_unit, capability_unit, operator_is_known, convertible)
    )

    if report.layers[1].passed and report.layers[1].borderline:
        report.applicability = Applicability.FAMILY_LEVEL
    return report
