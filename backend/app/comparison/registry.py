"""Comparison registry and evaluators (FR-27, FR-28).

Deterministic, no LLM: each evaluator returns `{status, score, reason}` for one
requirement against one documented capability.

The registry maps a `ValueType` to an evaluator. An unknown attribute or a
missing evaluator produces `needs_review` - the fact is kept, the match is not
guessed (FR-29).

Every threshold and score band comes from `ComparisonPolicy` in config, so the
rules are explicit policy rather than constants buried in the comparison code,
and a report can record the policy it was assessed under (FR-34).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from app.comparison import attributes as attrs
from app.comparison.units import convert, is_compatible
from app.comparison.vocabulary import load_vocabulary
from app.core.config import ComparisonPolicy, get_settings
from app.schemas.enums import Operator, ReasonCode, RequirementStatus, ValueType


def default_policy() -> ComparisonPolicy:
    """The comparison policy the deployment is configured with.

    Read from settings on every call rather than snapshotted at import time, so
    a test (or a deployment) that changes the environment changes the rules.
    """
    return get_settings().comparison


@dataclass
class Evaluation:
    """What an evaluator returns (FR-28)."""

    status: RequirementStatus
    score: float
    reason: str
    # True when the comparison could not be made and needs a human.
    needs_review: bool = False
    # The capability was documented for a family, not for this model.
    family_level: bool = False
    # Why no verdict was reached, when the reason is a limit of this system
    # rather than a finding about the product (D-86).
    code: Optional[ReasonCode] = None


Evaluator = Callable[["ComparisonRequest"], Evaluation]


@dataclass
class ComparisonRequest:
    """One requirement measured against one documented capability."""

    requirement_value: Optional[str]
    requirement_operator: Operator
    requirement_unit: Optional[str]
    requirement_text_value: Optional[str]
    requirement_range: Optional[tuple[float, float]]
    capability_value: Optional[str]
    capability_unit: Optional[str]
    capability_text_value: Optional[str]
    capability_range: Optional[tuple[float, float]]
    # True when the capability is documented for a product family, not the model.
    capability_is_family_level: bool = False
    # True when the capability came from OCR text (PRD 9 - lower confidence).
    capability_from_ocr: bool = False
    # How far short of the requirement a capability may fall and still be a
    # partial match. None means "use the configured value".
    partial_tolerance: Optional[float] = None
    # The comparison rules: thresholds and score bands. None means "use the
    # configured policy", resolved when the comparison runs, not when the
    # request is built, so a change to the environment takes effect.
    policy: Optional[ComparisonPolicy] = None

    @property
    def rules(self) -> ComparisonPolicy:
        """The policy in force for this comparison."""
        return self.policy or default_policy()

    @property
    def tolerance(self) -> float:
        """The partial-match tolerance in force for this comparison."""
        if self.partial_tolerance is not None:
            return self.partial_tolerance
        return self.rules.numeric_partial_tolerance


def _numeric_value(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# Unit conversion is floating point, so 1.5 m3/h becomes 24.999999999999996 l/min
# and would fail a `>= 25` comparison. Comparisons use a relative epsilon.
_EPSILON = 1e-9


def _at_least(offered: float, required: float) -> bool:
    return offered >= required * (1 - _EPSILON) or offered >= required - abs(required) * _EPSILON


def _at_most(offered: float, required: float) -> bool:
    return offered <= required * (1 + _EPSILON) or offered <= required + abs(required) * _EPSILON


def _compare_numeric(
    required: float,
    operator: Operator,
    offered: float,
    tolerance: float,
    policy: ComparisonPolicy,
) -> tuple[RequirementStatus, float, str]:
    """Direction-aware numeric comparison (PRD 10).

    A capability that falls short by more than `tolerance` of the requirement
    is a capability failure, not a partial match: PRD 4 names "1200 RPM max vs
    1500 RPM required" as Missing capability, a 20% shortfall.
    """
    if operator is Operator.GTE:
        if _at_least(offered, required):
            return RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} meets the minimum {required:g}"
        if offered > 0 and required > 0:
            ratio = offered / required
            if ratio >= 1.0 - tolerance:
                # Close but not meeting it: partial, graded by how close it is.
                score = _band_score(ratio, policy)
                return RequirementStatus.PARTIAL_MATCH, score, f"{offered:g} is below the required {required:g}"
        return RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered:g} does not meet the required {required:g}"

    if operator is Operator.LTE:
        if _at_most(offered, required):
            return RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} is within the maximum {required:g}"
        if required > 0:
            ratio = required / offered
            if ratio >= 1.0 - tolerance:
                score = _band_score(ratio, policy)
                return RequirementStatus.PARTIAL_MATCH, score, f"{offered:g} exceeds the maximum {required:g}"
        return RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered:g} exceeds the maximum {required:g}"

    if operator is Operator.EQ:
        if abs(offered - required) <= abs(required) * _EPSILON:
            return RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} equals the required {required:g}"
        if offered > 0 and required > 0:
            ratio = min(offered, required) / max(offered, required)
            if ratio >= 1.0 - policy.equality_tolerance:
                return (
                    RequirementStatus.PARTIAL_MATCH,
                    policy.partial_score_max,
                    f"{offered:g} differs from the required {required:g}",
                )
            return (
                RequirementStatus.PARTIAL_MATCH,
                policy.partial_score_min,
                f"{offered:g} differs from the required {required:g}",
            )
        return RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered:g} does not equal the required {required:g}"

    if operator is Operator.NE:
        if abs(offered - required) > abs(required) * _EPSILON:
            return RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} is not the excluded {required:g}"
        return RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered:g} is the excluded value {required:g}"

    if operator is Operator.APPROX:
        if required == 0:
            return (
                (RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} matches {required:g}")
                if offered == 0
                else (
                    RequirementStatus.PARTIAL_MATCH,
                    policy.family_level_score,
                    f"{offered:g} differs from {required:g}",
                )
            )
        ratio = min(offered, required) / max(offered, required)
        if ratio >= 1.0 - policy.approximation_tolerance:
            return RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} is approximately {required:g}"
        return (
            RequirementStatus.PARTIAL_MATCH,
            policy.partial_score_min,
            f"{offered:g} is not close to {required:g}",
        )

    if operator is Operator.BETWEEN:
        return RequirementStatus.MISSING_CAPABILITY, 0.0, "range comparison needs a range capability"

    return (
        RequirementStatus.UNSUPPORTED_REQUIREMENT,
        0.0,
        f"operator {operator.value} cannot be compared",
    )


def _band_score(ratio: float, policy: ComparisonPolicy) -> float:
    """Grade a partial match by how close it is, inside the configured band."""
    scaled = ratio * policy.partial_score_max
    return max(policy.partial_score_min, min(policy.partial_score_max, scaled))


def evaluate_numeric(request: ComparisonRequest) -> Evaluation:
    # A range requirement against a range capability is a range comparison.
    if request.capability_range is not None:
        return evaluate_range(request)

    required = _numeric_value(request.requirement_value)
    offered = _numeric_value(request.capability_value)
    if required is None or offered is None:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "numeric comparison needs a number on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )

    if request.requirement_unit and request.capability_unit:
        if not is_compatible(request.requirement_unit, request.capability_unit):
            return Evaluation(
                RequirementStatus.NO_EVIDENCE_FOUND,
                0.0,
                f"units {request.requirement_unit} and {request.capability_unit} are not convertible",
                needs_review=True,
                code=ReasonCode.UNIT_NOT_COMPARABLE,
            )
        try:
            offered = convert(offered, request.capability_unit, request.requirement_unit)
        except Exception as exc:  # noqa: BLE001 - conversion failure is reported, not raised
            return Evaluation(
                RequirementStatus.NO_EVIDENCE_FOUND,
                0.0,
                f"unit conversion failed: {type(exc).__name__}",
                needs_review=True,
                code=ReasonCode.UNIT_NOT_COMPARABLE,
            )
    elif request.requirement_unit and not request.capability_unit:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "the capability has no unit, so it cannot be compared",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )

    if request.requirement_operator is Operator.BETWEEN and request.requirement_range:
        low, high = request.requirement_range
        span = max(high - low, abs(high) * _EPSILON)
        if low - abs(low) * _EPSILON <= offered <= high + abs(high) * _EPSILON:
            status, score, reason = RequirementStatus.FULL_MATCH, 1.0, f"{offered:g} is within {low:g}-{high:g}"
        elif offered > 0:
            nearest = min(abs(offered - low), abs(offered - high))
            shortfall = nearest / span
            if shortfall <= request.tolerance:
                score = _band_score(1.0 - shortfall, request.rules)
                status, reason = RequirementStatus.PARTIAL_MATCH, f"{offered:g} is just outside {low:g}-{high:g}"
            else:
                status, score, reason = (
                    RequirementStatus.MISSING_CAPABILITY,
                    0.0,
                    f"{offered:g} is outside {low:g}-{high:g}",
                )
        else:
            status, score, reason = RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered:g} is outside {low:g}-{high:g}"
        return Evaluation(status, score, reason)

    status, score, reason = _compare_numeric(
        required, request.requirement_operator, offered, request.tolerance, request.rules
    )
    if status is RequirementStatus.UNSUPPORTED_REQUIREMENT:
        # An operator this registry cannot apply is a limit of ours, not a
        # clause no document could answer (D-86).
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            score,
            reason,
            needs_review=True,
            code=ReasonCode.OPERATOR_NOT_COMPARABLE,
        )
    return Evaluation(status, score, reason)


def evaluate_range(request: ComparisonRequest) -> Evaluation:
    """A range requirement against a range capability.

    The requirement asks for a value *within* a range. A capability range that
    covers the whole requirement range satisfies it; a narrower one may not
    reach every value the requirement allows.
    """
    if not request.requirement_range or not request.capability_range:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "range comparison needs a range on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )
    req_low, req_high = request.requirement_range
    cap_low, cap_high = request.capability_range
    if cap_low <= req_low and cap_high >= req_high:
        return Evaluation(
            RequirementStatus.FULL_MATCH,
            1.0,
            f"{cap_low:g}-{cap_high:g} covers the required {req_low:g}-{req_high:g}",
        )
    if cap_low >= req_low and cap_high <= req_high:
        return Evaluation(
            RequirementStatus.PARTIAL_MATCH,
            request.rules.family_level_score,
            f"{cap_low:g}-{cap_high:g} is narrower than the required {req_low:g}-{req_high:g}",
        )
    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY, 0.0, f"{cap_low:g}-{cap_high:g} does not cover {req_low:g}-{req_high:g}"
    )


def _categorical_key(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return attrs.normalise_token(text)


# A token that ends in a number: "dn50", "ip67", "2". A token that contains a
# number but does not end in one is not a size: "10 bar rated" is a value with
# a unit, not an identifier.
_TRAILING_NUMBER = re.compile(r"^(.*?)(\d+)$")


def _numeric_part(key: str) -> Optional[str]:
    """The trailing number of a token, or None if it does not end in one."""
    match = _TRAILING_NUMBER.match(key)
    return match.group(2) if match else None


def _same_size_family(req_key: str, off_key: str) -> bool:
    """True when two tokens are identifiers of the same kind.

    "dn50" and "dn500" are both nominal sizes, so one is not a prefix-match of
    the other: they are different sizes. "swing" and "swingcheck" are both
    free text, so a prefix match is a genuine "includes".

    A bare number is treated as the same family as any identifier: a required
    "2" is not satisfied by "dn250", whatever the identifier's prefix is.
    """
    req_digits = _numeric_part(req_key)
    off_digits = _numeric_part(off_key)
    if req_digits is None or off_digits is None:
        return False
    req_prefix = req_key[: len(req_key) - len(req_digits)]
    off_prefix = off_key[: len(off_key) - len(off_digits)]
    if not req_prefix or not off_prefix:
        # One side is a bare number: a number is not a substring of an
        # identifier that happens to contain it.
        return True
    return req_prefix == off_prefix


def evaluate_categorical(request: ComparisonRequest) -> Evaluation:
    """Enum / IP rating / actuation comparison."""
    required = request.requirement_text_value or request.requirement_value
    offered = request.capability_text_value or request.capability_value
    if not required or not offered:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "categorical comparison needs a value on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )

    req_key = _categorical_key(required)
    off_key = _categorical_key(offered)
    if req_key == off_key:
        return Evaluation(RequirementStatus.FULL_MATCH, 1.0, f"{offered} matches the required {required}")

    # IP rating: a higher protection level covers a lower one (IP68 covers IP67).
    if required and offered and _ip_level(required) and _ip_level(offered):
        req_digits = _ip_level(required)
        off_digits = _ip_level(offered)
        if req_digits[0] == off_digits[0] and off_digits[1] >= req_digits[1]:
            return Evaluation(
                RequirementStatus.FULL_MATCH, 1.0, f"{offered} satisfies the required {required}"
            )
        if req_digits[0] == off_digits[0] and off_digits[1] == req_digits[1] - 1:
            return Evaluation(
                RequirementStatus.PARTIAL_MATCH,
                request.rules.family_level_score,
                f"{offered} is one level below the required {required}",
            )

    # A required value that appears in the capability text is still a match -
    # but a token that ends in a number is an identifier, and one identifier
    # is not a prefix of another. "dn50" is a substring of "dn500", and a
    # DN500 valve does not satisfy a DN50 requirement.
    if req_key and req_key in off_key:
        if _same_size_family(req_key, off_key):
            return Evaluation(
                RequirementStatus.MISSING_CAPABILITY,
                0.0,
                f"{offered} is a different size from the required {required}",
            )
        return Evaluation(RequirementStatus.FULL_MATCH, 1.0, f"{offered} includes the required {required}")
    if off_key and off_key in req_key:
        if _same_size_family(req_key, off_key):
            return Evaluation(
                RequirementStatus.PARTIAL_MATCH,
                request.rules.family_level_score,
                f"{offered} is a smaller size than the required {required}",
            )
        return Evaluation(
            RequirementStatus.PARTIAL_MATCH,
            request.rules.family_level_score,
            f"{offered} is narrower than the required {required}",
        )

    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered} does not meet the required {required}"
    )


def _ip_level(text: str) -> Optional[tuple[int, int]]:
    pattern = attrs.ip_pattern()
    match = pattern.search(text) if pattern else None
    if not match:
        return None
    token = match.group(0).upper().replace(" ", "").replace("-", "")
    digits = ""
    for char in token[2:]:
        if char.isdigit():
            digits += char
        else:
            break
    if not digits:
        return None
    if len(digits) == 1:
        return (int(digits), 0)
    return (int(digits[0]), int(digits[1]))


def evaluate_boolean(request: ComparisonRequest) -> Evaluation:
    required = (request.requirement_text_value or "").strip().lower()
    offered = (request.capability_text_value or "").strip().lower()
    if not required or not offered:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "boolean comparison needs a value on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )
    vocabulary = load_vocabulary()
    truthy = vocabulary.boolean_truthy
    falsy = vocabulary.boolean_falsy
    req_bool = required in truthy if required in truthy else required not in falsy
    off_bool = offered in truthy if offered in truthy else offered not in falsy
    if req_bool == off_bool:
        return Evaluation(RequirementStatus.FULL_MATCH, 1.0, f"{offered} meets the requirement")
    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered} does not meet the requirement"
    )


def evaluate_certification(request: ComparisonRequest) -> Evaluation:
    """A cited standard must appear in the capability evidence."""
    required = request.requirement_text_value or request.requirement_value
    offered = request.capability_text_value or request.capability_value
    if not required or not offered:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "certification comparison needs a standard on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )
    req_key = attrs.normalise_token(required)
    off_key = attrs.normalise_token(offered)
    if req_key == off_key or (req_key and req_key in off_key):
        return Evaluation(
            RequirementStatus.FULL_MATCH, 1.0, f"the documented {offered} covers the required {required}"
        )
    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY,
        0.0,
        f"the documented {offered} does not cover the required {required}",
    )


def evaluate_material(request: ComparisonRequest) -> Evaluation:
    """Material comparison follows the alloy groups (SS316 / AISI 316 / CF8M)."""
    required = request.requirement_text_value or request.requirement_value
    offered = request.capability_text_value or request.capability_value
    if not required or not offered:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "material comparison needs a material on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )
    verdict = attrs.material_satisfies(required, offered)
    if verdict is None:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            f"the material {required} or {offered} is not a recognised grade",
            needs_review=True,
            code=ReasonCode.GRADE_NOT_RECOGNISED,
        )
    if verdict:
        return Evaluation(
            RequirementStatus.FULL_MATCH, 1.0, f"{offered} satisfies the required {required}"
        )
    if attrs.material_is_family_level(required, offered):
        # A generic alloy confirms the family, not the grade: uncertain
        # applicability, capped and flagged for review (PRD 4).
        return Evaluation(
            RequirementStatus.PARTIAL_MATCH,
            request.rules.family_level_score,
            f"{offered} is family-level evidence for the required {required}",
            needs_review=True,
            family_level=True,
        )
    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY, 0.0, f"{offered} does not meet the required {required}"
    )


REGISTRY: dict[ValueType, Evaluator] = {
    ValueType.NUMERIC: evaluate_numeric,
    ValueType.RANGE: evaluate_range,
    ValueType.CATEGORICAL: evaluate_categorical,
    ValueType.BOOLEAN: evaluate_boolean,
    ValueType.CERTIFICATION: evaluate_certification,
    ValueType.MATERIAL: evaluate_material,
}


def get_evaluator(value_type: Optional[ValueType]) -> Optional[Evaluator]:
    """The evaluator for a value type, or None when there is none (FR-29)."""
    if value_type is None:
        return None
    return REGISTRY.get(value_type)


def evaluate(request: ComparisonRequest, value_type: Optional[ValueType]) -> Evaluation:
    """Run the registered evaluator. Unknown type means review, never a guess."""
    evaluator = get_evaluator(value_type)
    if evaluator is None:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "no evaluator is registered for this attribute",
            needs_review=True,
            code=ReasonCode.ATTRIBUTE_NOT_MAPPED,
        )
    evaluation = evaluator(request)
    if request.capability_is_family_level:
        # Evidence that covers a family, not the model under test: uncertain
        # applicability. The score is capped and the claim goes to review
        # (PRD 4).
        return Evaluation(
            evaluation.status,
            min(evaluation.score, request.rules.family_level_score),
            evaluation.reason,
            needs_review=True,
            family_level=True,
        )
    return evaluation
