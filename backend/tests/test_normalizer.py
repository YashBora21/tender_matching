"""Normalizer tests: operators, units, ranges, mandatory wording (FR-15)."""

from __future__ import annotations

import pytest

from app.schemas.enums import Operator, ValueType
from app.services.normalizer import normalize


@pytest.mark.parametrize(
    ("text", "operator", "value"),
    [
        ("Rotational speed shall be not less than 1500 RPM", Operator.GTE, "1500"),
        ("Rotational speed shall be at least 1500 RPM", Operator.GTE, "1500"),
        ("Speed max 1200 rpm", Operator.LTE, "1200"),
        ("Speed not exceeding 1200 RPM", Operator.LTE, "1200"),
        ("Operating temperature shall not exceed 60 degC", Operator.LTE, "60"),
        ("Operating temperature shall be minimum -20 degC", Operator.GTE, "-20"),
        ("Flow rate between 10 and 20 m3/h", Operator.BETWEEN, "10-20"),
    ],
)
def test_operator_is_read_from_the_wording(text: str, operator: Operator, value: str) -> None:
    result = normalize(text)
    assert result.operator is operator
    assert result.value == value


def test_units_are_resolved_including_specification_spellings() -> None:
    assert normalize("Speed shall be 1500 RPM").unit == "rpm"
    assert normalize("Flow rate shall be 10 m3/h").unit == "m**3/hour"
    assert normalize("Torque shall be 50 Nm").unit == "N*m"
    assert normalize("Operating temperature shall be 60 degC").unit == "degC"


def test_a_range_becomes_a_range_not_a_single_number() -> None:
    result = normalize("Flow rate between 10 and 20 m3/h")
    assert result.range == (10.0, 20.0)
    assert result.operator is Operator.BETWEEN


def test_mandatory_and_optional_wording_is_read() -> None:
    assert normalize("Speed shall be 1500 RPM").mandatory is True
    assert normalize("Speed shall be 1500 RPM").mandatory is True
    assert normalize("Speed may be 1500 RPM").mandatory is False
    assert normalize("Speed preferably 1500 RPM").mandatory is False


def test_an_unknown_attribute_yields_no_usable_requirement() -> None:
    result = normalize("The supplier shall be a good quality organisation")
    assert result.attribute is None
    assert result.is_usable is False
    assert result.warnings


def test_certification_is_recognised_from_a_standard_code() -> None:
    result = normalize("Valve shall comply with IS 1239")
    assert result.attribute == "certification"
    assert result.value_type is ValueType.CERTIFICATION
    assert "IS" in (result.text_value or "").upper()


def test_material_grade_is_recognised() -> None:
    result = normalize("Body shall be SS316")
    assert result.attribute == "material_grade"
    assert result.value_type is ValueType.MATERIAL


def test_digits_inside_a_grade_are_not_read_as_a_value() -> None:
    """SS316 is a grade, not the number 316.

    Without this, "SS316 ... 16 bar" parses the 316 as an operating pressure
    and takes the unit from the wrong word, and the requirement is compared
    against a capability it was never about.
    """
    result = normalize("Material of body shall be SS316 with pressure rating 16 bar")
    assert result.value == "16"
    assert result.unit == "bar"


def test_digits_inside_a_standard_code_are_not_read_as_a_value() -> None:
    result = normalize("Material of body shall be ASTM A351 CF8M with pressure rating 16 bar")
    assert result.value == "16"
    assert result.unit == "bar"


def test_a_grade_with_a_leading_number_is_not_read_as_a_value() -> None:
    result = normalize("Trim shall be 13Cr with hardness 200 HB")
    assert result.attribute == "material_grade"
    assert result.text_value == "13cr"


def test_a_prefixed_nominal_size_keeps_its_number() -> None:
    # "DN50" is a size, so the 50 is the value even though it follows a letter.
    result = normalize("Valve shall be DN50")
    assert result.attribute == "nominal_size"
    assert result.value == "50"
    assert result.unit == "mm"


def test_ip_rating_is_recognised() -> None:
    result = normalize("Enclosure shall have IP68 protection")
    assert result.attribute == "ip_rating"
    assert result.text_value == "IP68"
