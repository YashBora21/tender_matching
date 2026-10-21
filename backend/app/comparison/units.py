"""Unit parsing and conversion (FR-15, PRD 10).

`pint` does the conversion; this module restricts *which* conversions are
allowed, because pint is permissive in ways that are wrong for engineering
specifications:

- `Hz` and `rpm` are both 1/time, so pint converts between them. Physically
  they are different quantities and a 50 Hz motor is not a 477 rpm pump.
- `kW` and `kVA` are both power, so pint converts between them. One is real
  power, the other apparent power, and they are not interchangeable.
- `psig` (gauge) and `psi` (absolute) are different pressures, so `psig` is
  not an alias for `psi`.

The policy is therefore: convert only when the physical dimension is explicit
and known, otherwise surface uncertainty. A unit that cannot be parsed is not
an error - the comparison is flagged for review rather than guessed at
(PRD 9 - "unit conversion was ambiguous").
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

import pint

REGISTRY = pint.UnitRegistry()

# pint does not know the gauge/absolute pressure spellings. They are defined
# here as their own units so they parse, and so that psig never silently
# becomes psi: a gauge pressure and an absolute pressure are different
# quantities and converting between them needs the ambient pressure.
for _name, _definition in (
    ("psig", "psi"),
    ("psia", "psi"),
    ("barg", "bar"),
):
    try:
        REGISTRY.define(f"{_name} = {_definition}")
    except Exception:  # noqa: BLE001 - already defined
        pass

# Units that pint treats as interchangeable but that are not the same quantity.
# Each maps to the dimension it is allowed to convert within.
# Every member of a `_DIMENSION_UNITS` group carries that group's label, so a
# unit that is not listed here falls through to pint's raw dimensionality and
# would not match the group it belongs to.
DIMENSION_OVERRIDES: dict[str, str] = {
    "hz": "frequency",
    "khz": "frequency",
    "mhz": "frequency",
    "rpm": "rotational_speed",
    "rev/min": "rotational_speed",
    "r/min": "rotational_speed",
    "w": "power",
    "kw": "power",
    "mw": "power",
    "hp": "power",
    "bhp": "power",
    "va": "apparent_power",
    "kva": "apparent_power",
    "mva": "apparent_power",
    "psig": "gauge_pressure",
    "barg": "gauge_pressure",
    "psia": "absolute_pressure",
    "psi": "absolute_pressure",
    "bar": "absolute_pressure",
    "mpa": "absolute_pressure",
    "kpa": "absolute_pressure",
    "pa": "absolute_pressure",
}

# The dimension each override belongs to, and the units that share it.
_DIMENSION_UNITS: dict[str, frozenset[str]] = {
    "frequency": frozenset({"hz", "khz", "mhz"}),
    "rotational_speed": frozenset({"rpm", "rev/min", "r/min"}),
    "power": frozenset({"w", "kw", "mw", "hp", "bhp"}),
    "apparent_power": frozenset({"va", "kva", "mva"}),
    "gauge_pressure": frozenset({"psig", "barg"}),
    "absolute_pressure": frozenset({"psi", "psia", "bar", "mpa", "kpa", "pa"}),
}

# Spellings that appear in specifications but that pint does not know directly.
UNIT_ALIASES = {
    "rpm": "rpm",
    "rev/min": "rpm",
    "r/min": "rpm",
    "revs/min": "rpm",
    "nm": "N*m",
    "n.m": "N*m",
    "n-m": "N*m",
    "newton-metre": "N*m",
    "m3/h": "m**3/hour",
    "m3/hr": "m**3/hour",
    "m^3/h": "m**3/hour",
    "cumec": "m**3/hour",
    "lpm": "liter/minute",
    "l/min": "liter/minute",
    "lph": "liter/hour",
    "l/hr": "liter/hour",
    "m/sec": "meter/second",
    "m/s": "meter/second",
    "sq mm": "mm**2",
    "sqmm": "mm**2",
    "mm2": "mm**2",
    "mm3": "mm**3",
    "cc": "cm**3",
    "°c": "degC",
    "deg c": "degC",
    "degree c": "degC",
    "degree celsius": "degC",
    "celsius": "degC",
    "°f": "degF",
    "deg f": "degF",
    "degree fahrenheit": "degF",
    "fahrenheit": "degF",
    "k": "kelvin",
    "mca": "meter",
    "bar g": "bar",
    "percentage": "percent",
    "pct": "percent",
    "db": "decibel",
    "db(a)": "decibel",
}

# Units that are deliberately not aliased to anything else, because the
# distinction matters: gauge vs absolute pressure.
_KEEP_AS_IS = {"psig", "psia", "barg"}


@lru_cache(maxsize=1024)
def parse_unit(text: Optional[str]) -> Optional[str]:
    """Return a pint-parseable spelling of `text`, or None if it is not a unit.

    A trailing plural is tried as a fallback, because "16 bars" and "20 mm" both
    occur in specifications.
    """
    if not text:
        return None
    candidate = text.strip().strip("()[]").strip()
    if not candidate:
        return None

    for attempt in _candidates(candidate):
        try:
            REGISTRY.Unit(attempt)
        except Exception:  # noqa: BLE001 - pint raises several unrelated types
            continue
        return attempt
    return None


def _candidates(text: str):
    yield text
    if text.lower() in _KEEP_AS_IS:
        # psig stays psig: it is not an alias for psi.
        return
    alias = UNIT_ALIASES.get(text) or UNIT_ALIASES.get(text.lower())
    if alias:
        yield alias
    if text.lower().endswith("s") and len(text) > 1:
        singular = text[:-1]
        yield singular
        alias = UNIT_ALIASES.get(singular) or UNIT_ALIASES.get(singular.lower())
        if alias:
            yield alias


def dimension_of(unit: Optional[str]) -> Optional[str]:
    """The comparison dimension a unit belongs to, or None if it has none."""
    if not unit:
        return None
    key = unit.lower().strip()
    if key in DIMENSION_OVERRIDES:
        return DIMENSION_OVERRIDES[key]
    try:
        return str(REGISTRY.Unit(unit).dimensionality)
    except Exception:  # noqa: BLE001 - an unparseable unit has no dimension
        return None


def is_compatible(left: str, right: str) -> bool:
    """True when the two units measure the same thing and can be converted.

    This is stricter than pint's own check: units that share a dimensionality
    but a different physical quantity (Hz and rpm, kW and kVA) are not
    compatible, and neither are gauge and absolute pressure.
    """
    left_dimension = dimension_of(left)
    right_dimension = dimension_of(right)
    if left_dimension is None or right_dimension is None:
        return False
    if left_dimension != right_dimension:
        return False

    # Same override dimension: check the unit actually belongs to it.
    for dimension, units in _DIMENSION_UNITS.items():
        if dimension == left_dimension:
            if left.lower() in units and right.lower() in units:
                return True
            # One of them has no explicit override, so fall back to pint for
            # units that share the dimension unambiguously.
            break

    try:
        REGISTRY.Quantity(1, left).to(right)
    except Exception:  # noqa: BLE001 - incompatible dimensionality
        return False
    return True


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert `value` between units. Raises pint.DimensionalityError if it cannot."""
    return REGISTRY.Quantity(value, from_unit).to(to_unit).magnitude
