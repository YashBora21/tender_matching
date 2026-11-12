"""Correct clause types using source position and subject (FR-14, FR-15).

Deterministic policy can correct a model's type when the source establishes
a product property or a bidder qualification. Ambiguous context requires
review. Technical clauses can also occur in buyer-added terms. No type
decision establishes a match or changes the original source quotation.

Existing numeric parsing only fills absent fields on clauses promoted from
unknown or certification to technical. Semantic cues are versioned data.
"""

from __future__ import annotations

import re
from typing import Optional

from app.schemas.enums import Operator, RequirementType
from app.services.tender.classify import reads_as_legal_clause, clause_policy, classify_clause
from app.services.tender.ingest import RequirementRecord

# The attribute a clause names, and the canonical name it is stored under.
# A word that names the same attribute several ways ("dia", "diameter") maps
# to one name, so the comparison registry sees one attribute.
ATTRIBUTE_WORDS: dict[str, str] = clause_policy()["retype"]["attribute_words"]

# A number and the unit it is in. The alternatives are ordered longest first,
# so "kg/cm2" is not read as "kg" and "m3/hr" is not read as "m3".
_VALUE = re.compile(clause_policy()["retype"]["value_pattern"], re.IGNORECASE)

# The direction a clause states, strongest phrase first: "not less than" must
# be read before "less than", and "or more" before "more than".
_OPERATORS = tuple((re.compile(pattern, re.IGNORECASE), Operator(operator))
                   for pattern, operator in clause_policy()["retype"]["operators"])

# How far after an attribute word a bare number is still its value:
# "Class 150", "size 100". The number has to follow the word directly: a
# number further away ("... as per API 598") is not the attribute's value.
_VALUE_WINDOW = clause_policy()["retype"]["value_window"]

# What may stand between an attribute word and its bare value: whitespace and
# the punctuation of "Class: 150", "size = 100". A word between the two means
# the number belongs to something else.
_BARE_NUMBER = re.compile(clause_policy()["retype"]["bare_number_pattern"])


def _attribute(text: str) -> Optional[str]:
    """The specification attribute a clause names, if it names one."""
    best: Optional[tuple[int, str]] = None
    lowered = text.lower()
    for word, name in ATTRIBUTE_WORDS.items():
        match = re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), name)
    return best[1] if best else None


def _signed(text: str, match: re.Match) -> Optional[str]:
    """The number, with its sign when the sign is a sign and not a range.

    "10-20 mm" states a range, not the value -20; "-20 °C" states a
    temperature below zero. The two are told apart by what comes before the
    sign: a number means a range.
    """
    number = match.group("number").replace(",", ".")
    start = match.start()
    if start == 0:
        return number
    sign = text[start - 1]
    if sign not in "-+":
        return number

    index = start - 2
    while index >= 0 and text[index].isspace():
        index -= 1
    if index >= 0 and text[index].isdigit():
        # "10-20": the sign is the range's dash.
        return None
    return f"-{number}" if sign == "-" else number


def _value(text: str) -> tuple[Optional[str], Optional[str]]:
    """The number a clause states, and its unit when it names one.

    A number written as a range ("10-20 mm") is not one value, so it is not
    read as one: a wrong value is worse than no value.
    """
    match = _VALUE.search(text)
    if match:
        number = _signed(text, match)
        if number is None:
            return None, None
        return number, match.group("unit").lower()

    # A class, a rating or a size is often written without a unit:
    # "Class 150" states the value 150. The number has to follow the
    # attribute word directly, or it belongs to something else.
    for word in ATTRIBUTE_WORDS:
        found = re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text, re.IGNORECASE)
        if not found:
            continue
        tail = text[found.end():found.end() + _VALUE_WINDOW]
        number = _BARE_NUMBER.match(tail)
        if not number:
            continue
        following = tail[number.end():number.end() + 1]
        if following in ("-", "–"):
            # "size 10-20" is a range, not the value 10.
            return None, None
        return number.group(1).replace(",", "."), None
    return None, None


def _operator(text: str) -> Optional[Operator]:
    """The direction a clause states, if it states one."""
    for pattern, operator in _OPERATORS:
        if pattern.search(text):
            return operator
    return None


def apply_clause_type(requirement: RequirementRecord) -> bool:
    """Apply a source-position/subject decision without changing values or quotes."""
    if requirement.page_tag in clause_policy()['non_requirement_tags']:
        return False
    subject = requirement.source_text or requirement.text
    if requirement.table_context and requirement.attribute:
        # Classify the labelled field, not neighbouring columns in the same row.
        subject = ' '.join(str(value) for value in (requirement.attribute, requirement.value, requirement.unit) if value)
    from app.services.requirement.structured_values import resolve_name
    canonical, _ = resolve_name(requirement.attribute)
    decision = classify_clause(subject,
        page_tag=requirement.page_tag, source_section=requirement.source_section,
        attribute=canonical)
    if decision.review_required:
        if requirement.requirement_type in (RequirementType.UNKNOWN, RequirementType.TECHNICAL, RequirementType.CERTIFICATION):
            requirement.review_required = True
            reason = 'clause_type_review: ' + decision.reason
            if reason not in (requirement.review_reason or ''):
                requirement.review_reason = '; '.join(part for part in (
                    requirement.review_reason, reason) if part)
        return False
    target = RequirementType(decision.kind)
    if target is requirement.requirement_type:
        return False
    requirement.requirement_type = target
    return True


def retype(requirement: RequirementRecord) -> bool:
    """Re-type one clause as technical when it names a specification attribute.

    A clause with an attribute but no readable value is still technical - it
    is a technical clause that was read badly - but it is flagged, because
    nothing can be compared without both sides.

    A clause with a value but no attribute is *not* re-typed: "80% of bid
    quantity" and "300 Mtr." state numbers, and a number on its own is as
    likely to be a commercial term as a capability.
    """
    original_type = requirement.requirement_type
    changed = apply_clause_type(requirement)
    if requirement.requirement_type is not RequirementType.TECHNICAL:
        return changed
    if original_type not in (RequirementType.UNKNOWN, RequirementType.CERTIFICATION):
        return changed

    text = requirement.text or ""
    # A mixed page is read as a whole, so the page tag cannot separate its
    # halves: a letterhead clause on a page that also carries a size table
    # would be re-typed as a size requirement on the strength of the word
    # "head". The clause's own words decide (D-139).
    if reads_as_legal_clause(text):
        return changed
    attribute = _attribute(text)
    value, unit = _value(text)
    operator = _operator(text)

    if attribute is None:
        # Nothing names a property, so there is nothing to compare.
        return changed

    requirement.requirement_type = RequirementType.TECHNICAL
    if requirement.attribute is None:
        requirement.attribute = attribute
    # The extractor never leaves the operator empty: a model that gives none
    # gets `unknown`, so an unknown operator is one the re-typing may fill.
    if requirement.operator in (None, Operator.UNKNOWN.value):
        if operator is not None:
            requirement.operator = operator.value
        elif value is not None:
            requirement.operator = Operator.EQ.value
    if requirement.value is None:
        requirement.value = value
    if requirement.unit is None:
        requirement.unit = unit

    if value is None:
        requirement.review_required = True
        requirement.review_reason = "; ".join(reason for reason in (
            requirement.review_reason, "re-typed as technical, but the value could not be read"
        ) if reason)
    return True


def retype_technical(requirements: list[RequirementRecord]) -> int:
    """Apply source typing and technical field recovery; count changed records."""
    return sum(1 for requirement in requirements if retype(requirement))
