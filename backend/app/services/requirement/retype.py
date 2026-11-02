"""Re-typing a clause the model could not type (FR-14, FR-15).

The model reads a clause and says what it is. When it answers `unknown`, the
clause is not necessarily non-technical: "Preferable size of Delivery end 100
mm or more" states a size and a limit whatever the model called it. This
module is the second pair of eyes, and it is deterministic: no model is
called, the same clause always gets the same answer, and the answer is a
proposal with its evidence in the fields.

Two rules bound it:

- It only ever moves a clause from `unknown` to `technical`. It never
  overrides a type the model gave, and it never decides a match.
- It never touches a clause from a page that is not a specification. A legal
  or safety page can state a number without stating a capability.

The words below are the configuration: adding an attribute or a unit is a
one-line change here.
"""

from __future__ import annotations

import re
from typing import Optional

from app.schemas.enums import Operator, RequirementType
from app.services.tender.classify import is_non_technical
from app.services.tender.ingest import RequirementRecord

# The attribute a clause names, and the canonical name it is stored under.
# A word that names the same attribute several ways ("dia", "diameter") maps
# to one name, so the comparison registry sees one attribute.
ATTRIBUTE_WORDS: dict[str, str] = {
    "size": "size",
    "diameter": "diameter",
    "dia": "diameter",
    "bore": "diameter",
    "head": "head",
    "flow": "flow",
    "discharge": "flow",
    "capacity": "capacity",
    "pressure": "pressure",
    "rating": "rating",
    "class": "class",
    "material": "material",
    "moc": "material",
    "thickness": "thickness",
    "efficiency": "efficiency",
    "power": "power",
    "speed": "speed",
    "rpm": "speed",
    "voltage": "voltage",
    "frequency": "frequency",
    "temperature": "temperature",
    "weight": "weight",
    "length": "length",
    "width": "width",
    "height": "height",
}

# A number and the unit it is in. The alternatives are ordered longest first,
# so "kg/cm2" is not read as "kg" and "m3/hr" is not read as "m3".
_VALUE = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>kg/cm2|kg/cm²|m3/hr|m3/min|m3/h|m³/h|lpm|lps|gpm|sqmm|mm2|"
    r"percent|meter|metre|mtr|inch|kpa|mpa|ppm|psi|rpm|kwh|watt|volt|amp|"
    r"mm|cm|kg|gm|ton|bar|deg|°c|hz|kw|hp|pa|mt|ml|in|m|v|a|w|g|l|%)(?![a-z0-9])",
    re.IGNORECASE,
)

# The direction a clause states, strongest phrase first: "not less than" must
# be read before "less than", and "or more" before "more than".
_OPERATORS: tuple[tuple[re.Pattern[str], Operator], ...] = (
    (
        re.compile(
            r"\b(?:or\s+more|not\s+less\s+than|at\s+least|minimum|min\.?)\b",
            re.IGNORECASE,
        ),
        Operator.GTE,
    ),
    (
        re.compile(
            r"\b(?:or\s+less|not\s+more\s+than|at\s+most|maximum|max\.?)\b",
            re.IGNORECASE,
        ),
        Operator.LTE,
    ),
    (
        re.compile(r"\b(?:greater\s+than|more\s+than|above|exceeds?)\b", re.IGNORECASE),
        Operator.GT,
    ),
    (re.compile(r"\b(?:less\s+than|below)\b", re.IGNORECASE), Operator.LT),
    (re.compile(r"\bbetween\b", re.IGNORECASE), Operator.BETWEEN),
)

# How far after an attribute word a bare number is still its value:
# "Class 150", "size 100". The number has to follow the word directly: a
# number further away ("... as per API 598") is not the attribute's value.
_VALUE_WINDOW = 24

# What may stand between an attribute word and its bare value: whitespace and
# the punctuation of "Class: 150", "size = 100". A word between the two means
# the number belongs to something else.
_BARE_NUMBER = re.compile(r"[\s:=/(.\-]*(\d+(?:[.,]\d+)?)")


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


def retype(requirement: RequirementRecord) -> bool:
    """Re-type one clause as technical when it names a specification attribute.

    A clause with an attribute but no readable value is still technical - it
    is a technical clause that was read badly - but it is flagged, because
    nothing can be compared without both sides.

    A clause with a value but no attribute is *not* re-typed: "80% of bid
    quantity" and "300 Mtr." state numbers, and a number on its own is as
    likely to be a commercial term as a capability.
    """
    if requirement.requirement_type is not RequirementType.UNKNOWN:
        return False
    if requirement.page_tag and is_non_technical(requirement.page_tag):
        return False

    text = requirement.text or ""
    attribute = _attribute(text)
    value, unit = _value(text)
    operator = _operator(text)

    if attribute is None:
        # Nothing names a property, so there is nothing to compare.
        return False

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
        requirement.review_reason = (
            "re-typed as technical, but the value could not be read"
        )
    return True


def retype_technical(requirements: list[RequirementRecord]) -> int:
    """Re-type every clause that states a value. Returns how many changed."""
    return sum(1 for requirement in requirements if retype(requirement))
