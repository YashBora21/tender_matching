"""Type-driven requirement resolution.

The LLM extracts the parts of a requirement - the attribute, the operator,
the value, the unit - and this module decides what to do with them:

1. The attribute name is resolved to a canonical term through the alias
   table. A name that does not resolve is not a guess: the requirement goes
   to review (FR-29).
2. The canonical term's *value type* selects the evaluator. The evaluator is
   chosen by the shape of the value, never by the product category, so a pump
   requirement and a valve requirement are compared the same way.
3. The unit is resolved and checked for convertibility.

The only regex left in this path is for universal format grammars - a number
with a unit, a standard code, an IP rating, a nominal size - and those are
loaded from the vocabulary file, not written here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.comparison.attributes import attribute_by_term
from app.comparison.units import parse_unit
from app.core.config import get_settings
from app.repositories.aliases import AliasRepo
from app.schemas.enums import Operator, ValueType


@dataclass
class ResolvedRequirement:
    """A requirement whose attribute and value type are known."""

    canonical_term: Optional[str] = None
    value_type: Optional[ValueType] = None
    operator: Operator = Operator.UNKNOWN
    value: Optional[str] = None
    unit: Optional[str] = None
    range: Optional[tuple[float, float]] = None
    text_value: Optional[str] = None
    mandatory: bool = True
    # The confidence of the alias that resolved the name.
    alias_confidence: float = 0.0
    # True when the requirement cannot be compared and needs a human.
    needs_review: bool = False
    review_reason: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        """True when there is enough here to run a comparison at all."""
        if self.needs_review:
            return False
        if self.operator is Operator.UNKNOWN:
            return False
        if self.value_type in (ValueType.CATEGORICAL, ValueType.MATERIAL, ValueType.CERTIFICATION):
            return bool(self.text_value)
        if self.operator is Operator.BETWEEN:
            return self.range is not None
        return self.value is not None


def resolve_attribute(name: Optional[str], session) -> tuple[Optional[str], float, bool, Optional[str]]:
    """Resolve an attribute name to a canonical term.

    Returns `(canonical_term, confidence, needs_review, reason)`. The alias
    table is the mapping; a name that is not in it and is not a canonical
    term does not resolve.
    """
    if not name:
        return None, 0.0, True, "the requirement names no attribute"

    threshold = get_settings().alias_confidence_threshold
    resolution = AliasRepo(session).resolve(name, threshold=threshold)
    return (
        resolution.canonical_term,
        resolution.confidence,
        resolution.needs_review,
        resolution.reason,
    )


def resolve(
    *,
    attribute: Optional[str],
    operator: Operator,
    value: Optional[str],
    unit: Optional[str],
    text_value: Optional[str],
    value_range: Optional[tuple[float, float]],
    mandatory: bool,
    session,
) -> ResolvedRequirement:
    """Resolve an extracted requirement into a typed one.

    The value type comes from the canonical term, and it is the value type
    that decides which evaluator runs. An attribute that does not resolve is
    a requirement for review, never a guess (FR-29).
    """
    result = ResolvedRequirement(
        operator=operator,
        value=value,
        unit=unit,
        range=value_range,
        text_value=text_value,
        mandatory=mandatory,
    )

    canonical_term, confidence, needs_review, reason = resolve_attribute(attribute, session)
    result.alias_confidence = confidence
    if needs_review or canonical_term is None:
        result.needs_review = True
        result.review_reason = reason or "the attribute could not be resolved"
        return result

    result.canonical_term = canonical_term

    spec = attribute_by_term(canonical_term)
    if spec is None:
        # The alias points at a term the vocabulary does not define. The
        # mapping is not wrong, the vocabulary is behind.
        result.needs_review = True
        result.review_reason = (
            f"'{canonical_term}' is not a known attribute in the vocabulary"
        )
        return result

    result.value_type = spec.value_type

    # A unit the requirement states wins; otherwise the attribute's default.
    resolved_unit = unit or spec.default_unit
    if resolved_unit:
        parsed = parse_unit(resolved_unit)
        if parsed is None:
            result.warnings.append(f"unit '{resolved_unit}' could not be parsed")
        else:
            result.unit = parsed

    return result
