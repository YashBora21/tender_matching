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
    # The canonical attribute the comparison is about, when it is known. An
    # attribute can have a rule of its own (a pressure class is ordered, an IP
    # rating is not), so the registry is asked for it before the value type.
    attribute: Optional[str] = None
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

    # Anything else - including a `between` clause with no pair to compare
    # against - is a comparison this registry cannot make. That is a gap in what
    # we read, not a failing value in a document, so it is not a missing
    # capability: `evaluate_numeric` turns it into *no evidence found*.
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

    # A `between` requirement states two numbers, not one, so it has no single
    # `required` value by design; the pair is read from `requirement_range`.
    between = (
        request.requirement_operator is Operator.BETWEEN
        and request.requirement_range is not None
    )

    required = _numeric_value(request.requirement_value)
    offered = _numeric_value(request.capability_value)
    if offered is None or (required is None and not between):
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

    if between:
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
    required_range = request.requirement_range
    if required_range is None and request.requirement_operator is Operator.EQ:
        point = _numeric_value(request.requirement_value)
        if point is not None:
            required_range = (point, point)
    if not required_range or not request.capability_range:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "range comparison needs a range on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )
    req_low, req_high = required_range
    cap_low, cap_high = request.capability_range
    if bool(request.requirement_unit) != bool(request.capability_unit):
        return Evaluation(RequirementStatus.NO_EVIDENCE_FOUND, 0.0,
                          "range units are incomplete", needs_review=True)
    if request.requirement_unit and request.capability_unit:
        if not is_compatible(request.capability_unit, request.requirement_unit):
            return Evaluation(RequirementStatus.NO_EVIDENCE_FOUND, 0.0,
                              "range units are incompatible", needs_review=True)
        cap_low = convert(cap_low, request.capability_unit, request.requirement_unit)
        cap_high = convert(cap_high, request.capability_unit, request.requirement_unit)
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

    # The corpus writes one connection two ways: a requirement that says
    # "screwed end" is answered by a document that says "Threaded". Only the
    # alias group answers this - a substring would read "dn50" inside "dn500".
    group = attrs.value_group(req_key)
    if group is not None and group == attrs.value_group(off_key):
        return Evaluation(
            RequirementStatus.FULL_MATCH,
            1.0,
            f"{offered} is the required {required} under another name",
        )

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

    # A specification line that lists the values a product is offered in is a
    # statement of what exists. The required value being absent from that list
    # is a failing value - Missing capability - and not an absence of evidence
    # (PRD 4, D-103). Membership reads the alias group, so a list of "Threaded"
    # answers a requirement of "Screwed end".
    listed = attrs.split_values(offered)
    if listed:
        if any(attrs.same_value(required, value) for value in listed):
            return Evaluation(
                RequirementStatus.FULL_MATCH,
                1.0,
                f"{offered} lists the required {required}",
            )
        return Evaluation(
            RequirementStatus.MISSING_CAPABILITY,
            0.0,
            f"{offered} lists {', '.join(listed)}, which excludes the required {required}",
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


# Pressure classes are ordered, but they are not numbers in one scale: an
# ASME class and a PN class are different systems. A token is read as
# (system, value); the evaluator only compares tokens in the same system.
#
# The gap between "ASME" and "CLASS" is optional: the normalizer stores a
# pressure class uppercased with its spaces removed (`ASMECLASS150`), so
# requiring one space here made every such requirement unreadable - four gold
# pairs died on it (D-155, D-160).
_CLASS_TOKEN = re.compile(
    r"\b(?:(?:ANSI|ASME)\s*)?CLASS\s*-?\s*(?P<class>\d{1,4})\b"
    r"|\bPN\s*-?\s*(?P<pn>\d{1,3})\b",
    re.IGNORECASE,
)
_BARE_CLASS = re.compile(r"(?<![\w.])(\d{1,4})(?![\w.])")


def _pressure_classes(text: Optional[str]) -> list[tuple[str, int]]:
    """The pressure classes a value names, as (system, number).

    "Class 150" and a bare "150" are the ASME class system; "PN16" is the PN
    system. A capability that lists several classes names the range the
    product is available in.
    """
    if not text:
        return []
    found: list[tuple[str, int]] = []
    for match in _CLASS_TOKEN.finditer(text):
        if match.group("class") is not None:
            found.append(("class", int(match.group("class"))))
        elif match.group("pn") is not None:
            found.append(("pn", int(match.group("pn"))))
    if found:
        return found
    # The model often answers with the number alone ("150"). The attribute is
    # already known to be a pressure class, so the bare number is one.
    return [("class", int(value)) for value in _BARE_CLASS.findall(text)]


def evaluate_ordered_categorical(request: ComparisonRequest) -> Evaluation:
    """Pressure class: a higher class is not automatically interchangeable.

    A Class 300 valve is rated above a Class 150 requirement, but the rating
    is not the only difference: a higher class is often a heavier, longer
    valve, and the clause may also fix face-to-face or weight. So the default
    policy treats "higher" as a **partial match that goes to review**, not a
    full one (D-99); `comparison_pressure_class_higher_covers_lower` is how a
    deployment says the buyer accepts a higher class outright.

    A lower class is a documented capability with a failing value, so it is
    *missing capability*, not an absence of evidence. Classes in different
    systems (ASME class vs PN) are not converted here: a conversion is a fact
    about a standard, and guessing one would be a comparison nobody can check.
    """
    required_text = request.requirement_text_value or request.requirement_value
    offered_text = request.capability_text_value or request.capability_value
    required = _pressure_classes(required_text)
    offered = _pressure_classes(offered_text)
    if not required or not offered:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "pressure class comparison needs a class on both sides",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )

    required_systems = {system for system, _ in required}
    offered_systems = {system for system, _ in offered}
    if required_systems != offered_systems:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            f"pressure classes {required_text} and {offered_text} are in different systems",
            needs_review=True,
            code=ReasonCode.VALUE_MISSING,
        )

    # The requirement may list the classes it accepts; the least demanding one
    # is the floor. The capability may list the classes it is made in; the
    # highest one is what it can be supplied as.
    required_floor = min(value for _, value in required)
    offered_top = max(value for _, value in offered)
    system = next(iter(required_systems))
    label = "Class" if system == "class" else "PN"
    if offered_top == required_floor:
        return Evaluation(
            RequirementStatus.FULL_MATCH,
            1.0,
            f"{label} {offered_top} meets the required {label} {required_floor}",
        )
    if offered_top > required_floor:
        if request.rules.pressure_class_higher_covers_lower:
            return Evaluation(
                RequirementStatus.FULL_MATCH,
                1.0,
                f"{label} {offered_top} covers the required {label} {required_floor}",
            )
        return Evaluation(
            RequirementStatus.PARTIAL_MATCH,
            request.rules.partial_score_max,
            (
                f"{label} {offered_top} is higher than the required {label} "
                f"{required_floor}; a higher class is not confirmed as "
                "interchangeable without review"
            ),
            needs_review=True,
        )
    return Evaluation(
        RequirementStatus.MISSING_CAPABILITY,
        0.0,
        f"{label} {offered_top} does not meet the required {label} {required_floor}",
    )


# A rule that belongs to one attribute rather than to a whole value type. The
# attribute is the canonical term from the vocabulary, so adding a rule is a
# change here and naming the attribute is a change to the data file.
ATTRIBUTE_EVALUATORS: dict[str, Evaluator] = {
    "pressure_class": evaluate_ordered_categorical,
}


REGISTRY: dict[ValueType, Evaluator] = {
    ValueType.NUMERIC: evaluate_numeric,
    ValueType.RANGE: evaluate_range,
    ValueType.CATEGORICAL: evaluate_categorical,
    ValueType.BOOLEAN: evaluate_boolean,
    ValueType.CERTIFICATION: evaluate_certification,
    ValueType.MATERIAL: evaluate_material,
}


def get_evaluator(
    value_type: Optional[ValueType], attribute: Optional[str] = None
) -> Optional[Evaluator]:
    """The evaluator for an attribute, or its value type, or None (FR-29).

    An attribute with a rule of its own wins over the value type: pressure
    class is categorical, but its values are ordered. An attribute with no
    evaluator has no rule, and `evaluate` sends it to review.
    """
    spec = attrs.attribute_by_term(attribute) if attribute else None
    if spec and getattr(spec, 'normalization', None) == 'nominal':
        return evaluate_nominal
    if attribute and attribute in ATTRIBUTE_EVALUATORS:
        return ATTRIBUTE_EVALUATORS[attribute]
    if value_type is None:
        return None
    return REGISTRY.get(value_type)


def evaluate_nominal(request: ComparisonRequest) -> Evaluation:
    """Nominal labels identify sizes; physical length conversion is invalid here."""
    from app.services.requirement.structured_values import normalize_fields

    sides = []
    for value, unit, bounds in (
        (request.requirement_value, request.requirement_unit, request.requirement_range),
        (request.capability_value, request.capability_unit, request.capability_range),
    ):
        raw = '-'.join(f'{v:g}' for v in bounds) if bounds is not None else value
        side = normalize_fields(request.attribute, raw, unit)
        if side['review_required']:
            return Evaluation(RequirementStatus.NO_EVIDENCE_FOUND, 0.0,
                              side['review_reason'], needs_review=True,
                              code=ReasonCode.UNIT_NOT_COMPARABLE)
        points = side['nominal_dn']
        sides.append((points[0], points[-1]))
    (low, high), (offered_low, offered_high) = sides
    op = request.requirement_operator
    if op in (Operator.EQ, Operator.BETWEEN):
        meets = offered_low <= low and offered_high >= high
    elif op is Operator.GTE and low == high:
        meets = offered_high >= low
    elif op is Operator.LTE and low == high:
        meets = offered_low <= high
    elif op is Operator.NE and low == high:
        meets = not offered_low <= low <= offered_high
    else:
        return Evaluation(RequirementStatus.NO_EVIDENCE_FOUND, 0.0,
                          'nominal operator requires review', needs_review=True,
                          code=ReasonCode.OPERATOR_NOT_COMPARABLE)
    return Evaluation(RequirementStatus.FULL_MATCH if meets else RequirementStatus.MISSING_CAPABILITY,
                      1.0 if meets else 0.0,
                      'configured nominal-size table: documented sizes ' + ('meet' if meets else 'fail') + ' the requirement')


def _apply_declared_scores(evaluation: Evaluation) -> None:
    """Put the score the deployment declares on the verdict the evaluator reached.

    Each evaluator decides its own verdict; what a verdict is worth is one
    question, answered once in `ScoringPolicy` (PRD 8). Without this, a full
    match carried a `1.0` written in the evaluator and `SCORING_FULL_SCORE`
    changed nothing a report published. A partial match keeps the graded score
    the comparison band gave it.
    """
    scoring = get_settings().scoring
    if evaluation.status is RequirementStatus.FULL_MATCH:
        evaluation.score = scoring.full_score
    elif evaluation.status is RequirementStatus.MISSING_CAPABILITY:
        evaluation.score = scoring.missing_score


def evaluate(request: ComparisonRequest, value_type: Optional[ValueType]) -> Evaluation:
    """Run the registered evaluator. Unknown type means review, never a guess."""
    evaluator = get_evaluator(value_type, request.attribute)
    if evaluator is None:
        return Evaluation(
            RequirementStatus.NO_EVIDENCE_FOUND,
            0.0,
            "no evaluator is registered for this attribute",
            needs_review=True,
            code=ReasonCode.ATTRIBUTE_NOT_MAPPED,
        )
    evaluation = evaluator(request)
    _apply_declared_scores(evaluation)
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
