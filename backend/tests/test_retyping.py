"""Re-typing: a clause the model called unknown, but which states a value.

Every clause below is synthetic. None of it is taken from a real tender.
"""

from __future__ import annotations

from app.schemas.enums import Operator, RequirementType
from app.services.retyping import retype, retype_technical
from app.services.tenderingest import RequirementRecord


def _unknown(text: str, *, page_tag: str = "technical") -> RequirementRecord:
    return RequirementRecord(
        text=text,
        item_id="1",
        page=1,
        requirement_type=RequirementType.UNKNOWN,
        page_tag=page_tag,
    )


def test_a_size_with_a_limit_is_re_typed_with_its_evidence():
    """"Preferable size of Delivery end 100 mm or more" states a size, a
    direction and a value whatever the model called it."""
    requirement = _unknown("Preferable size of Delivery end 100 mm or more")

    assert retype(requirement) is True
    assert requirement.requirement_type is RequirementType.TECHNICAL
    assert requirement.attribute == "size"
    assert requirement.operator == Operator.GTE.value
    assert requirement.value == "100"
    assert requirement.unit == "mm"
    assert requirement.review_required is False


def test_a_minimum_efficiency_is_re_typed():
    requirement = _unknown("minimum efficiency 85%")

    assert retype(requirement) is True
    assert requirement.requirement_type is RequirementType.TECHNICAL
    assert requirement.attribute == "efficiency"
    assert requirement.operator == Operator.GTE.value
    assert requirement.value == "85"
    assert requirement.unit == "%"


def test_a_maximum_pressure_is_read_as_less_than_or_equal():
    requirement = _unknown("maximum working pressure 10 bar")

    assert retype(requirement) is True
    assert requirement.attribute == "pressure"
    assert requirement.operator == Operator.LTE.value
    assert requirement.value == "10"
    assert requirement.unit == "bar"


def test_an_attribute_with_no_value_is_technical_and_flagged():
    """Nothing can be compared without a value, so the clause is re-typed and
    sent to review rather than silently dropped."""
    requirement = _unknown("The size shall be minimum")

    assert retype(requirement) is True
    assert requirement.requirement_type is RequirementType.TECHNICAL
    assert requirement.attribute == "size"
    assert requirement.operator == Operator.GTE.value
    assert requirement.value is None
    assert requirement.review_required is True
    assert "value could not be read" in requirement.review_reason


def test_a_clause_with_neither_an_attribute_nor_a_value_is_unchanged():
    requirement = _unknown("The bidder shall submit the documents")

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.UNKNOWN
    assert requirement.attribute is None
    assert requirement.value is None


def test_a_bare_value_is_not_a_clause():
    """"300 Mtr." is a value with no attribute and no direction: a cell of a
    table, not a requirement."""
    requirement = _unknown("300 Mtr.")

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.UNKNOWN


def test_a_value_with_no_attribute_is_not_re_typed():
    """A percentage of the bid quantity is a commercial term, not a
    capability, and there is nothing to compare it against."""
    requirement = _unknown(
        "similar Category Products for 80% of bid quantity, in at least one of"
    )

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.UNKNOWN


def test_a_legal_page_is_never_re_typed():
    """A legal page can state a number without stating a capability."""
    requirement = _unknown(
        "The penalty shall be 2% of the contract value", page_tag="commercial_legal"
    )

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.UNKNOWN


def test_a_safety_page_is_never_re_typed():
    requirement = _unknown(
        "The safety harness shall be rated 100 kg", page_tag="safety_hse"
    )

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.UNKNOWN


def test_a_type_the_model_gave_is_not_overridden():
    """Re-typing only ever moves a clause out of unknown."""
    requirement = RequirementRecord(
        text="Preferable size of Delivery end 100 mm or more",
        item_id="1",
        page=1,
        requirement_type=RequirementType.COMMERCIAL,
    )

    assert retype(requirement) is False
    assert requirement.requirement_type is RequirementType.COMMERCIAL
    assert requirement.attribute is None


def test_a_value_the_model_already_read_is_kept():
    """The model's own evidence wins; re-typing only fills what is empty."""
    requirement = _unknown("minimum efficiency 85%")
    requirement.value = "90"
    requirement.unit = "%"

    assert retype(requirement) is True
    assert requirement.value == "90"


def test_an_unknown_operator_is_filled():
    """The extractor never leaves the operator empty: a model that gives none
    gets `unknown`, and that is a slot the re-typing may fill."""
    requirement = _unknown("Preferable size of Delivery end 100 mm or more")
    requirement.operator = Operator.UNKNOWN.value

    assert retype(requirement) is True
    assert requirement.operator == Operator.GTE.value


def test_a_number_far_from_the_attribute_is_not_its_value():
    """The 598 is the standard's number, not a pressure. A wrong value is
    worse than no value."""
    requirement = _unknown("Shell test: 1.5 x rated pressure as per API 598")

    assert retype(requirement) is True
    assert requirement.attribute == "pressure"
    assert requirement.value is None
    assert requirement.review_required is True


def test_a_negative_value_keeps_its_sign():
    requirement = _unknown("minimum temperature -20 °C")

    assert retype(requirement) is True
    assert requirement.attribute == "temperature"
    assert requirement.value == "-20"
    assert requirement.unit == "°c"


def test_a_range_is_not_read_as_one_value():
    requirement = _unknown("Valve size 10-20 mm")

    assert retype(requirement) is True
    assert requirement.attribute == "size"
    assert requirement.value is None
    assert requirement.review_required is True


def test_a_direction_with_no_word_is_equality():
    requirement = _unknown("Valve size 100 mm")

    assert retype(requirement) is True
    assert requirement.operator == Operator.EQ.value
    assert requirement.value == "100"


def test_the_count_is_the_number_of_clauses_that_changed():
    requirements = [
        _unknown("minimum efficiency 85%"),
        _unknown("The bidder shall submit the documents"),
        _unknown("Preferable size of Delivery end 100 mm or more"),
    ]

    assert retype_technical(requirements) == 2
    assert [r.requirement_type for r in requirements] == [
        RequirementType.TECHNICAL,
        RequirementType.UNKNOWN,
        RequirementType.TECHNICAL,
    ]
