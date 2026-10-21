"""Requirement normalizer (FR-15).

Turns requirement text into `{attribute, operator, value, unit, range, mandatory}`.

The normalizer proposes; it does not decide a match. Anything it cannot parse
with confidence comes out with `operator == UNKNOWN` or no attribute, which
routes the requirement to review rather than to a guess (FR-29).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from app.comparison import attributes as attrs
from app.comparison.units import parse_unit
from app.schemas.enums import Operator, ValueType

# Words that mark a requirement as optional, per attributes.OPTIONAL_MARKERS.
_UNDERSCORE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)")

# "<= 40", "<=40", "not exceeding 40", "max 40", "maximum of 40", "up to 40"
_MAX_PATTERNS = (
    r"(?:<=|≤|not\s+exceed(?:ing|s)?|max(?:imum)?(?:\s+of)?|up\s+to|at\s+most|no\s+more\s+than)\s*"
    r"(-?\d+(?:\.\d+)?)",
)
_MIN_PATTERNS = (
    r"(?:>=|≥|at\s+least|min(?:imum)?(?:\s+of)?|not\s+less\s+than|no\s+less\s+than)\s*"
    r"(-?\d+(?:\.\d+)?)",
)
_EXACT_PATTERNS = (
    r"(?:=|==|exactly|equal\s+to)\s*(-?\d+(?:\.\d+)?)",
)
_APPROX_PATTERNS = (r"(?:~|≈|approximately|about|around)\s*(-?\d+(?:\.\d+)?)",)
_NE_PATTERNS = (r"(?:!=|<>|not\s+equal\s+to|other\s+than)\s*(-?\d+(?:\.\d+)?)",)

# A number with an optional unit immediately after it.
# The lookbehind keeps digits that belong to an identifier out of it: in
# "SS316" the 316 is part of the grade, not a value, and in "13Cr" the 13 is
# part of the alloy. Without it, a material grade is read as a number and
# takes the unit from the wrong word in the sentence.
_NUMBER_UNIT = re.compile(
    r"(?<![A-Za-z0-9.])"
    r"(-?\d+(?:\.\d+)?)\s*"
    r"(%|percent|[A-Za-zµ°][A-Za-z0-9µ°\^/·\-\.\*]*)"
)

# A bare number, used when the unit came from the attribute's default.
_BARE_NUMBER = re.compile(r"(?<![A-Za-z0-9.])(-?\d+(?:\.\d+)?)")

# "between 10 and 20", "10 to 20"
_BETWEEN_PATTERNS = (
    r"between\s+(-?\d+(?:\.\d+)?)\s*(?:and|to|-|–)\s*(-?\d+(?:\.\d+)?)",
    r"(-?\d+(?:\.\d+)?)\s*(?:to|–|-)\s*(-?\d+(?:\.\d+)?)",
)

# A nominal size written with its prefix: "DN50", "NPS 2". The number is
# preceded by a letter, so the plain patterns skip it - the size is the value.
_PREFIXED_SIZE = re.compile(
    r"(?<![A-Za-z0-9.])(?:DN|NPS)\s?-?\s?(\d+(?:\.\d+)?)"
)


@dataclass
class NormalizedRequirement:
    """The FR-15 output. `value`/`range` hold what the operator compares against."""

    attribute: Optional[str] = None
    operator: Operator = Operator.UNKNOWN
    value: Optional[str] = None
    unit: Optional[str] = None
    value_type: Optional[ValueType] = None
    mandatory: bool = True
    range: Optional[tuple[float, float]] = None
    # Free text value for categorical / material / certification attributes.
    text_value: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when there is enough here to run a comparison at all."""
        if self.operator is Operator.UNKNOWN:
            return False
        if self.value_type in (ValueType.CATEGORICAL, ValueType.MATERIAL, ValueType.CERTIFICATION):
            return bool(self.text_value)
        if self.operator is Operator.BETWEEN:
            return self.range is not None
        return self.value is not None


def _first_match(text: str, patterns: tuple[str, ...]) -> Optional[tuple]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match
    return None


def _extract_unit(text: str, value: str) -> Optional[str]:
    """Find the unit belonging to `value` in the surrounding text."""
    # Prefer the unit that directly follows the number.
    for match in _NUMBER_UNIT.finditer(text):
        if match.group(1) == value:
            unit = parse_unit(match.group(2))
            if unit:
                return unit
    # Otherwise take the first parseable unit word in the text.
    for match in re.finditer(r"[A-Za-zµ°][A-Za-z0-9µ°\^/·\-\.\*]*", text):
        unit = parse_unit(match.group(0))
        if unit:
            return unit
    return None


def _numeric(text: str) -> Optional[tuple[str, Operator, str, Optional[str]]]:
    """Find (value, operator, unit) for a numeric requirement, if there is one."""
    # A prefixed size ("DN50") is the value itself, and its digits are preceded
    # by a letter, so it is read before the general patterns.
    prefixed = _PREFIXED_SIZE.search(text)
    if prefixed:
        value = prefixed.group(1)
        return (value, Operator.EQ, value, _extract_unit(text, value))

    match = _first_match(text, _BETWEEN_PATTERNS)
    if match:
        low, high = match.group(1), match.group(2)
        if float(low) <= float(high):
            unit = _extract_unit(text, low) or _extract_unit(text, high)
            return (low, Operator.BETWEEN, f"{low}-{high}", unit)

    for pattern, operator in (
        (_MAX_PATTERNS, Operator.LTE),
        (_MIN_PATTERNS, Operator.GTE),
        (_NE_PATTERNS, Operator.NE),
        (_EXACT_PATTERNS, Operator.EQ),
        (_APPROX_PATTERNS, Operator.APPROX),
    ):
        match = _first_match(text, pattern)
        if match:
            value = match.group(1)
            return (value, operator, value, _extract_unit(text, value))

    # A bare number with no direction word: the requirement states a value, and
    # the comparison decides whether it is met. Treated as "at least" only when
    # the attribute reads as a minimum, which the caller resolves.
    match = _NUMBER_UNIT.search(text) or _BARE_NUMBER.search(text)
    if match:
        value = match.group(1)
        return (value, Operator.EQ, value, _extract_unit(text, value))
    return None


def _categorical_value(text: str, spec: attrs.AttributeSpec) -> Optional[str]:
    """Pull the comparable token out of a categorical requirement."""
    if spec.canonical_term == "ip_rating":
        pattern = attrs.ip_pattern()
        match = pattern.search(text) if pattern else None
        return match.group(0).upper().replace(" ", "") if match else None
    if spec.canonical_term == "nominal_size":
        pattern = attrs.dn_pattern()
        match = pattern.search(text) if pattern else None
        if match:
            return match.group(0).upper().replace(" ", "")
    if spec.canonical_term == "certification":
        pattern = attrs.certification_pattern()
        match = pattern.search(text) if pattern else None
        return match.group(0).upper().replace(" ", "") if match else None
    if spec.canonical_term == "material_grade":
        pattern = attrs.material_pattern()
        match = pattern.search(text) if pattern else None
        return match.group(0).lower() if match else None
    # A bare enum value: the word after the attribute, e.g. "actuation: pneumatic".
    for separator in (":", "=", "-"):
        if separator in text:
            tail = text.rsplit(separator, 1)[1].strip()
            if tail:
                return tail
    return None


def normalize(text: str, *, default_unit: Optional[str] = None) -> NormalizedRequirement:
    """Parse requirement text into its comparable parts."""
    result = NormalizedRequirement(mandatory=attrs.is_mandatory(text))
    spec = attrs.find_attribute(text)
    if spec is None:
        result.warnings.append("no attribute could be identified")
        return result

    result.attribute = spec.canonical_term
    result.value_type = spec.value_type

    if spec.value_type is ValueType.NUMERIC:
        numeric = _numeric(text)
        if numeric is None:
            result.warnings.append("no numeric value found")
            return result
        value, operator, raw, unit = numeric
        result.operator = operator
        result.value = raw
        result.unit = unit or parse_unit(default_unit) or parse_unit(spec.default_unit)
        if result.unit is None:
            result.warnings.append("no unit could be determined")
        if operator is Operator.BETWEEN:
            low, _, high = raw.partition("-")
            try:
                result.range = (float(low), float(high))
            except ValueError:
                result.warnings.append("range could not be parsed")
        return result

    # Boolean requirements: "shall be X" where the attribute is a yes/no.
    if spec.value_type is ValueType.BOOLEAN:
        result.operator = Operator.EQ
        result.text_value = "true" if re.search(r"\b(shall\s+be\s+provided|required|with|yes)\b", text, re.I) else "false"
        return result

    value = _categorical_value(text, spec)
    if value is None:
        result.warnings.append("no categorical value found")
        return result
    result.operator = Operator.EQ
    result.text_value = value
    result.value = value
    return result
