"""Unit handling tests.

The rules here are the ones a specification actually depends on: convert only
when the physical dimension is explicit, and never silently equate quantities
that merely share a mathematical dimensionality.
"""

from __future__ import annotations

import pytest

from app.comparison.units import convert, is_compatible, parse_unit


@pytest.mark.parametrize(
    "text",
    ["mm", "m", "kg", "kW", "rpm", "degC", "bar", "psi", "V", "A", "Hz", "N*m", "m3/h", "l/min", "Nm"],
)
def test_the_units_that_appear_in_specifications_parse(text: str) -> None:
    assert parse_unit(text) is not None


def test_an_unrecognised_unit_does_not_parse() -> None:
    assert parse_unit("widgets") is None
    assert parse_unit("") is None
    assert parse_unit(None) is None


def test_hertz_and_rpm_are_not_interchangeable() -> None:
    # Both are 1/time, but a 50 Hz motor is not a 477 rpm pump.
    assert is_compatible("Hz", "rpm") is False


def test_real_power_and_apparent_power_are_not_interchangeable() -> None:
    assert is_compatible("kW", "kVA") is False


def test_gauge_and_absolute_pressure_are_not_interchangeable() -> None:
    assert is_compatible("psi", "psig") is False
    assert is_compatible("bar", "barg") is False


def test_gauge_pressure_still_parses() -> None:
    assert parse_unit("psig") == "psig"
    assert parse_unit("barg") == "barg"


def test_genuinely_compatible_units_convert() -> None:
    assert is_compatible("l/min", "m**3/hour") is True
    assert is_compatible("mm", "inch") is True
    assert is_compatible("degC", "degF") is True
    assert is_compatible("bar", "psi") is True


def test_conversion_produces_the_right_number() -> None:
    assert convert(1.5, "m**3/hour", "l/min") == pytest.approx(25.0)
    assert convert(50, "mm", "inch") == pytest.approx(1.9685, abs=1e-4)


@pytest.mark.parametrize(
    "left,right",
    [
        ("kW", "W"),
        ("W", "kW"),
        ("hp", "kW"),
        ("kW", "MW"),
        ("kHz", "MHz"),
        ("VA", "kVA"),
        ("rpm", "r/min"),
        ("bar", "kPa"),
    ],
)
def test_units_inside_one_dimension_convert(left: str, right: str) -> None:
    """Every member of a dimension group carries the group's label.

    Without that, `kW` and `W` get different dimension strings and an exact
    conversion is rejected as "not convertible".
    """
    assert is_compatible(left, right) is True


def test_power_converts_within_its_dimension() -> None:
    assert convert(15, "kW", "W") == pytest.approx(15000.0)
    assert convert(100, "kW", "hp") == pytest.approx(134.1, abs=0.1)
