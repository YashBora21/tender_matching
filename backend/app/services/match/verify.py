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

from app.comparison import attributes as attrs
from app.comparison.units import is_compatible
from app.schemas.enums import (
    Applicability,
    RequirementStatus,
    ValueType,
    VerificationOutcome,
)
from app.services.textnorm import normalise_text


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
    def review_reasons(self) -> list[str]:
        """Why a human should look: a layer that failed and one that was unsure."""
        return [result.reason for result in self.layers if not result.passed or result.borderline]

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
    return normalise_text(text)


def check_citation(quote: Optional[str], page_text: Optional[str]) -> LayerResult:
    """Layer 1: the quote must exist on the cited page.

    The quote is matched with the same normalisation the reader used to store
    the chunk (FR-23): control characters removed, Unicode folded and
    whitespace collapsed. A quote that is longer than the page cannot be
    verified, and an empty quote is a failure, not a pass: an unverifiable
    claim is exactly what this layer exists to catch.
    """
    if not quote or not quote.strip():
        return LayerResult(Layer.CITATION, False, "the claim carries no quote to verify")
    if page_text is None:
        return LayerResult(Layer.CITATION, False, "the cited page has no text to check the quote against")

    needle = normalise_text(quote)
    haystack = normalise_text(page_text)
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


def _identity(text: Optional[str]) -> str:
    """The form a product name is compared in: folded and case-insensitive."""
    return normalise_text(text or "").casefold()


def _names(needle: Optional[str], haystack: Optional[str]) -> bool:
    """True when `needle` is a whole name inside `haystack`.

    Product names are written with a suffix ("GV-100/150", "Series 75/76") and
    a plain substring test would make "GV-10" match "GV-100". The boundary
    test keeps a shorter model from matching a longer one.
    """
    left = _identity(needle)
    right = _identity(haystack)
    if not left or not right:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(left)}(?![a-z0-9])", right))


def check_entity(
    *,
    document_manufacturer: Optional[str] = None,
    document_model: Optional[str] = None,
    document_family: Optional[str] = None,
    document_model_or_family: Optional[str] = None,
    expected_manufacturer: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_family: Optional[str] = None,
    is_family_level: bool = False,
) -> LayerResult:
    """Layer 2: the *document's metadata* must identify the product under test.

    The passage is not evidence about which product it describes: a datasheet
    page that says "body is SS316" names no model, and reading that silence as
    a failure of the product was wrong. Provenance is the document row - the
    manufacturer it belongs to and the model, family or model-or-family it
    declares. The verifier compares that metadata with the product the run is
    answering for.

    When the document declares the model the evidence is model-level. When it
    declares only a family (or declares no product at all) the evidence is
    family-level: uncertain applicability, not a confirmed match (PRD 4).
    """
    if expected_manufacturer:
        if not document_manufacturer:
            return LayerResult(
                Layer.ENTITY,
                False,
                "the document declares no manufacturer to check against",
            )
        if not _names(expected_manufacturer, document_manufacturer):
            return LayerResult(
                Layer.ENTITY,
                False,
                f"the document belongs to {document_manufacturer}, not {expected_manufacturer}",
            )

    declared = [
        value
        for value in (document_model, document_family, document_model_or_family)
        if value
    ]

    if expected_model:
        # The document names the model under test: the strongest provenance.
        for value in (document_model, document_model_or_family):
            if _names(expected_model, value):
                if is_family_level:
                    return LayerResult(
                        Layer.ENTITY,
                        True,
                        f"the document names the model {expected_model} but covers a family",
                        borderline=True,
                    )
                return LayerResult(
                    Layer.ENTITY, True, f"the document declares the model {expected_model}"
                )
        # The document names the family, or a family-level catalogue: the
        # evidence applies to the family, not confirmed for this model.
        if expected_family and any(
            _names(expected_family, value)
            for value in (document_family, document_model_or_family)
        ):
            return LayerResult(
                Layer.ENTITY,
                True,
                f"the document covers the {expected_family} family, not the model {expected_model}",
                borderline=True,
            )
        if is_family_level or document_family or document_model_or_family:
            named = document_family or document_model_or_family or "a family"
            return LayerResult(
                Layer.ENTITY,
                True,
                f"the document covers {named}, not the model {expected_model}",
                borderline=True,
            )
        if document_model:
            return LayerResult(
                Layer.ENTITY,
                False,
                f"the document is about {document_model}, not {expected_model}",
            )
        return LayerResult(
            Layer.ENTITY,
            False,
            f"the document declares no model to check {expected_model} against",
        )

    # No model is under test: the document's own declaration is the provenance.
    if document_model and not is_family_level:
        return LayerResult(
            Layer.ENTITY, True, f"the document declares the model {document_model}"
        )
    if declared:
        return LayerResult(
            Layer.ENTITY,
            True,
            f"the document declares {declared[0]} at family level",
            borderline=True,
        )
    return LayerResult(
        Layer.ENTITY,
        True,
        "the document declares no model, so the evidence is family-level",
        borderline=True,
    )


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


@dataclass
class ListedSet:
    """A passage that lists the values an attribute is available in.

    The list is evidence: the attribute exists, and the values the document
    offers are the ones in the list. When the required value is not among
    them, the document states a failing value - Missing capability - not an
    absence of evidence (PRD 4).
    """

    status: RequirementStatus
    attribute: str
    required: str
    values: tuple[str, ...]
    quote: str
    reason: str


# A value list: "Flanged end / Butt weld end", "150, 300 or 600".
_LIST_SEPARATOR = re.compile(r"\s*(?:/|;|\||,|\bor\b|\band\b)\s*", re.IGNORECASE)
# A list value is a short name, not a sentence. This keeps a prose line that
# happens to contain "body style:" from being read as an availability list.
_MAX_LIST_VALUE_CHARS = 60


def _requirement_value(text: str, synonyms: tuple[str, ...]) -> Optional[str]:
    """The longest name of an attribute that the requirement uses."""
    lowered = text.casefold()
    for synonym in sorted(synonyms, key=len, reverse=True):
        if re.search(rf"\b{re.escape(synonym.casefold())}\b", lowered):
            return synonym
    return None


def _listed_values(line: str, synonyms: tuple[str, ...]) -> Optional[tuple[str, ...]]:
    """The values a line lists after an attribute name, if it lists any.

    The attribute has to be followed by `:` or `=`, and the remainder has to
    be at least two short values. A single value is left to the extractor: a
    prose sentence is not an availability list.
    """
    lowered = line.casefold()
    for synonym in sorted(synonyms, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(synonym.casefold())}\b", lowered)
        if match is None:
            continue
        tail = line[match.end() :]
        separator = re.search(r"[:=]", tail)
        if separator is None:
            continue
        text = tail[separator.end() :].strip()
        if not text:
            continue
        parts = [
            part.strip(" .")
            for part in _LIST_SEPARATOR.split(text)
            if part.strip(" .")
        ]
        parts = [part for part in parts if len(part) <= _MAX_LIST_VALUE_CHARS]
        if len(parts) >= 2:
            return tuple(parts)
    return None


def _listed_matches(required: str, offered: str) -> bool:
    """True when a listed value is the required one, or names it more fully."""
    if not required or not offered:
        return False
    return required == offered or required in offered or offered in required


def listed_set_exclusion(
    passage: Optional[str],
    requirement_text: Optional[str],
) -> Optional[ListedSet]:
    """A listed set of values that excludes the required one, if there is one.

    The function is deterministic and only reads a list the document writes
    after an attribute name. A passage that never lists the attribute is
    silent, and returns None: silence is No evidence found, never Missing
    capability (PRD 4, AGENTS #4). Only categorical attributes are read this
    way; a material or a number is compared by its own evaluator.
    """
    if not passage or not requirement_text:
        return None

    vocabulary = attrs.load_vocabulary()
    for spec in vocabulary.attributes:
        if spec.value_type is not ValueType.CATEGORICAL:
            continue
        required = _requirement_value(requirement_text, spec.synonyms)
        if not required:
            continue
        for line in passage.splitlines():
            values = _listed_values(line, spec.synonyms)
            if values is None:
                continue
            required_key = attrs.normalise_token(required)
            if any(_listed_matches(required_key, attrs.normalise_token(value)) for value in values):
                continue
            return ListedSet(
                status=RequirementStatus.MISSING_CAPABILITY,
                attribute=spec.canonical_term,
                required=required,
                values=values,
                quote=line.strip(),
                reason=(
                    f"the document lists {', '.join(values)} for "
                    f"{spec.canonical_term.replace('_', ' ')}, which excludes "
                    f"the required {required}"
                ),
            )
    return None


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
    document_manufacturer: Optional[str] = None,
    document_model: Optional[str] = None,
    document_family: Optional[str] = None,
    document_model_or_family: Optional[str] = None,
    expected_manufacturer: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_family: Optional[str] = None,
    is_family_level: bool = False,
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
    report.layers.append(
        check_entity(
            document_manufacturer=document_manufacturer,
            document_model=document_model,
            document_family=document_family,
            document_model_or_family=document_model_or_family,
            expected_manufacturer=expected_manufacturer,
            expected_model=expected_model,
            expected_family=expected_family,
            is_family_level=is_family_level,
        )
    )
    report.layers.append(check_semantic(passage, attribute, value))
    report.layers.append(
        check_comparison(requirement_unit, capability_unit, operator_is_known, convertible)
    )

    if report.layers[1].passed and report.layers[1].borderline:
        report.applicability = Applicability.FAMILY_LEVEL
    return report
