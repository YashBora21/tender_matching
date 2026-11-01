"""Comparison evaluator tests: the deterministic decision engine (FR-28)."""

from __future__ import annotations

import pytest

from app.comparison.registry import ComparisonRequest, evaluate
from app.schemas.enums import Operator, ReasonCode, RequirementStatus, ValueType


def numeric(
    required: str,
    operator: Operator,
    offered: str,
    unit: str = "rpm",
    capability_unit: str | None = None,
) -> ComparisonRequest:
    return ComparisonRequest(
        requirement_value=required,
        requirement_operator=operator,
        requirement_unit=unit,
        requirement_text_value=None,
        requirement_range=None,
        capability_value=offered,
        capability_unit=capability_unit if capability_unit is not None else unit,
        capability_text_value=None,
        capability_range=None,
    )


def categorical(required: str, offered: str) -> ComparisonRequest:
    return ComparisonRequest(
        requirement_value=required,
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value=required,
        requirement_range=None,
        capability_value=offered,
        capability_unit=None,
        capability_text_value=offered,
        capability_range=None,
    )


def test_a_value_meeting_a_minimum_is_a_full_match() -> None:
    result = evaluate(numeric("1500", Operator.GTE, "1800"), ValueType.NUMERIC)
    assert result.status is RequirementStatus.FULL_MATCH
    assert result.score == 1.0


def test_a_value_below_a_minimum_is_a_capability_failure_not_an_absence() -> None:
    # PRD 4: a documented value that fails the requirement is Missing capability.
    result = evaluate(numeric("1500", Operator.GTE, "1200"), ValueType.NUMERIC)
    assert result.status is RequirementStatus.MISSING_CAPABILITY
    assert result.score == 0.0


def test_a_close_but_insufficient_value_is_partial() -> None:
    result = evaluate(numeric("1500", Operator.GTE, "1400"), ValueType.NUMERIC)
    assert result.status is RequirementStatus.PARTIAL_MATCH
    assert 0.25 <= result.score <= 0.75


def test_units_are_converted_before_comparing() -> None:
    # 1.5 m3/h is 25 l/min: the same flow rate written in different units.
    result = evaluate(
        numeric("25", Operator.GTE, "1.5", unit="l/min", capability_unit="m**3/hour"),
        ValueType.NUMERIC,
    )
    assert result.status is RequirementStatus.FULL_MATCH


def test_incompatible_units_are_flagged_for_review_not_guessed() -> None:
    result = evaluate(
        numeric("1500", Operator.GTE, "1800", unit="rpm", capability_unit="kg"),
        ValueType.NUMERIC,
    )
    assert result.needs_review is True
    # Not "unsupported": the clause is verifiable, our comparison is what
    # could not run (D-86).
    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.code is ReasonCode.UNIT_NOT_COMPARABLE


def test_an_unknown_value_type_needs_review() -> None:
    result = evaluate(numeric("1500", Operator.GTE, "1800"), None)
    assert result.needs_review is True
    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.code is ReasonCode.ATTRIBUTE_NOT_MAPPED


def test_a_material_the_vocabulary_does_not_know_is_a_reason_code() -> None:
    """A grade the vocabulary does not resolve is not compared - but the
    clause is still a technical requirement."""
    request = ComparisonRequest(
        requirement_value=None,
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="widget alloy",
        requirement_range=None,
        capability_value=None,
        capability_unit=None,
        capability_text_value="widget alloy",
        capability_range=None,
    )
    result = evaluate(request, ValueType.MATERIAL)

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.code is ReasonCode.GRADE_NOT_RECOGNISED
    assert result.needs_review is True


def _material(required: str, offered: str) -> ComparisonRequest:
    return ComparisonRequest(
        requirement_value=required,
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value=required,
        requirement_range=None,
        capability_value=offered,
        capability_unit=None,
        capability_text_value=offered,
        capability_range=None,
    )


def test_cast_carbon_steel_is_the_carbon_steel_alias_group() -> None:
    """The gold slice named "cast carbon steel" against "Carbon Steel, A216
    Gr WCC": the same group, so the comparison can run (D-95)."""
    for offered in ("Carbon Steel", "WCB", "WCC", "A216 WCB", "ASTM A216 Gr WCC"):
        result = evaluate(
            _material("cast carbon steel", offered), ValueType.MATERIAL
        )
        assert result.status is RequirementStatus.FULL_MATCH, offered


def _pressure_class(
    required: str, offered: str, *, policy=None
) -> ComparisonRequest:
    return ComparisonRequest(
        requirement_value=required,
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value=required,
        requirement_range=None,
        capability_value=offered,
        capability_unit=None,
        capability_text_value=offered,
        capability_range=None,
        attribute="pressure_class",
        policy=policy,
    )


def test_a_higher_pressure_class_is_a_partial_match_until_reviewed() -> None:
    """A higher class is not automatically interchangeable (D-99).

    The rating is not the only difference: a Class 300 valve is often heavier
    and longer, and the clause may also fix face-to-face. So the default is a
    partial match that goes to review, not a full match.
    """
    result = evaluate(_pressure_class("150", "300"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.PARTIAL_MATCH
    assert result.score == 0.75
    assert result.needs_review is True


def test_a_deployment_can_say_a_higher_class_covers_a_lower_one() -> None:
    """The rule is policy, not a hard-coded fact: a buyer who accepts a higher
    class sets `comparison_pressure_class_higher_covers_lower` (D-99)."""
    from app.core.config import ComparisonPolicy

    policy = ComparisonPolicy(pressure_class_higher_covers_lower=True)
    result = evaluate(
        _pressure_class("150", "300", policy=policy), ValueType.CATEGORICAL
    )
    assert result.status is RequirementStatus.FULL_MATCH
    assert result.score == 1.0
    assert result.needs_review is False


def test_the_same_pressure_class_meets_the_requirement() -> None:
    result = evaluate(_pressure_class("Class 150", "150"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.FULL_MATCH


def test_a_lower_pressure_class_is_a_missing_capability_not_an_absence() -> None:
    """A Class 100 valve against a Class 150 requirement is a documented
    capability with a failing value (PRD 4)."""
    result = evaluate(_pressure_class("Class 150", "Class 100"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.MISSING_CAPABILITY
    assert result.score == 0.0


def test_pressure_classes_in_different_systems_need_review() -> None:
    """ASME class and PN are different scales. Converting one to the other is
    a fact about a standard, not a guess the evaluator may make."""
    result = evaluate(_pressure_class("Class 150", "PN16"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.needs_review is True


def test_an_attribute_without_an_evaluator_is_review_never_unsupported() -> None:
    """`unsupported_requirement` is for a clause no document could answer, not
    for an attribute this registry has no rule for (D-95)."""
    request = ComparisonRequest(
        requirement_value="widget",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="widget",
        requirement_range=None,
        capability_value="widget",
        capability_unit=None,
        capability_text_value="widget",
        capability_range=None,
        attribute="not_a_canonical_attribute",
    )
    result = evaluate(request, None)

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.needs_review is True
    assert result.code is ReasonCode.ATTRIBUTE_NOT_MAPPED
    assert result.status is not RequirementStatus.UNSUPPORTED_REQUIREMENT


def test_a_comparison_with_no_value_on_one_side_is_a_reason_code() -> None:
    result = evaluate(numeric("1500", Operator.GTE, "", unit="rpm"), ValueType.NUMERIC)

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.code is ReasonCode.VALUE_MISSING


def test_ip68_meets_an_ip67_requirement() -> None:
    request = ComparisonRequest(
        requirement_value="IP67",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="IP67",
        requirement_range=None,
        capability_value="IP68",
        capability_unit=None,
        capability_text_value="IP68",
        capability_range=None,
    )
    result = evaluate(request, ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.FULL_MATCH


def test_ip67_against_an_ip68_requirement_is_partial_not_a_full_match() -> None:
    # PRD 4 names this case: "e.g. IP67 vs IP68" is a Partial match.
    request = ComparisonRequest(
        requirement_value="IP68",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="IP68",
        requirement_range=None,
        capability_value="IP67",
        capability_unit=None,
        capability_text_value="IP67",
        capability_range=None,
    )
    result = evaluate(request, ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.PARTIAL_MATCH


def test_ip65_against_an_ip68_requirement_is_a_capability_failure() -> None:
    # Not one step away, and the dust rating is lower: the capability fails.
    request = ComparisonRequest(
        requirement_value="IP68",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="IP68",
        requirement_range=None,
        capability_value="IP65",
        capability_unit=None,
        capability_text_value="IP65",
        capability_range=None,
    )
    result = evaluate(request, ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.MISSING_CAPABILITY


def test_a_larger_size_does_not_satisfy_a_smaller_size_requirement() -> None:
    """DN50 is a substring of DN500, but a DN500 valve is not a DN50 valve.

    This is a wrong Full match on a dimension that is about to be ordered,
    and it would reach the compatibility score as a good result.
    """
    result = evaluate(categorical("DN50", "DN500"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.MISSING_CAPABILITY
    assert result.score == 0.0


def test_a_size_requirement_is_not_satisfied_by_a_different_size() -> None:
    result = evaluate(categorical("PN16", "PN160"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.MISSING_CAPABILITY


def test_a_bare_number_is_not_satisfied_by_an_identifier_containing_it() -> None:
    result = evaluate(categorical("2", "DN250"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.MISSING_CAPABILITY


def test_a_smaller_offered_size_is_partial_not_a_full_match() -> None:
    result = evaluate(categorical("DN500", "DN50"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.PARTIAL_MATCH


def test_the_same_size_still_matches() -> None:
    result = evaluate(categorical("DN50", "DN50"), ValueType.CATEGORICAL)
    assert result.status is RequirementStatus.FULL_MATCH


def test_free_text_still_matches_by_substring() -> None:
    """The substring rule is load-bearing for categorical text.

    Only a token that ends in a number is an identifier, and only an
    identifier is blocked from matching across a numeric boundary.
    """
    for required, offered in (
        ("swing", "swing check"),
        ("ball", "ball valve"),
        ("pneumatic", "pneumatic actuator"),
    ):
        result = evaluate(categorical(required, offered), ValueType.CATEGORICAL)
        assert result.status is RequirementStatus.FULL_MATCH


def test_equivalent_material_grades_match() -> None:
    # SS316 and AISI 316 are the same alloy (FR-20).
    request = ComparisonRequest(
        requirement_value="SS316",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="SS316",
        requirement_range=None,
        capability_value="AISI 316",
        capability_unit=None,
        capability_text_value="AISI 316",
        capability_range=None,
    )
    result = evaluate(request, ValueType.MATERIAL)
    assert result.status is RequirementStatus.FULL_MATCH


def test_a_generic_material_is_family_level_evidence_not_a_confirmed_match() -> None:
    # "stainless steel" against a requirement of "SS316" is family-level evidence:
    # uncertain applicability, never a confirmed match (PRD 4).
    request = ComparisonRequest(
        requirement_value="SS316",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="SS316",
        requirement_range=None,
        capability_value="stainless steel",
        capability_unit=None,
        capability_text_value="stainless steel",
        capability_range=None,
    )
    result = evaluate(request, ValueType.MATERIAL)
    assert result.status is RequirementStatus.PARTIAL_MATCH
    assert result.family_level is True
    assert result.needs_review is True


def test_an_unrecognised_material_needs_review() -> None:
    request = ComparisonRequest(
        requirement_value="widget alloy",
        requirement_operator=Operator.EQ,
        requirement_unit=None,
        requirement_text_value="widget alloy",
        requirement_range=None,
        capability_value="SS316",
        capability_unit=None,
        capability_text_value="SS316",
        capability_range=None,
    )
    result = evaluate(request, ValueType.MATERIAL)
    assert result.needs_review is True


def test_a_range_capability_covering_the_requirement_is_a_full_match() -> None:
    request = ComparisonRequest(
        requirement_value="10-20",
        requirement_operator=Operator.BETWEEN,
        requirement_unit="m**3/hour",
        requirement_text_value=None,
        requirement_range=(10.0, 20.0),
        capability_value="5-30",
        capability_unit="m**3/hour",
        capability_text_value=None,
        capability_range=(5.0, 30.0),
    )
    result = evaluate(request, ValueType.NUMERIC)
    assert result.status is RequirementStatus.FULL_MATCH


def test_a_single_value_is_compared_against_a_range_requirement() -> None:
    """A `between` clause has no single required number, and that is not a gap.

    The comparison asked for a number on both sides, so a requirement written as
    a pair and a document that states one number inside it - both of which the
    registry can decide - came out as *no evidence found* instead (D-121).
    """
    request = ComparisonRequest(
        requirement_value="10-20",
        requirement_operator=Operator.BETWEEN,
        requirement_unit="m**3/hour",
        requirement_text_value=None,
        requirement_range=(10.0, 20.0),
        capability_value="15",
        capability_unit="m**3/hour",
        capability_text_value=None,
        capability_range=None,
    )
    assert evaluate(request, ValueType.NUMERIC).status is RequirementStatus.FULL_MATCH

    outside = ComparisonRequest(
        requirement_value="10-20",
        requirement_operator=Operator.BETWEEN,
        requirement_unit="m**3/hour",
        requirement_text_value=None,
        requirement_range=(10.0, 20.0),
        capability_value="25",
        capability_unit="m**3/hour",
        capability_text_value=None,
        capability_range=None,
    )
    assert evaluate(outside, ValueType.NUMERIC).status is RequirementStatus.MISSING_CAPABILITY


def test_a_between_clause_with_no_pair_is_not_called_a_capability_failure() -> None:
    """Nothing was read, so nothing is known about the product (AGENTS #4)."""
    result = evaluate(numeric("15", Operator.BETWEEN, "1200"), ValueType.NUMERIC)
    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.code is ReasonCode.OPERATOR_NOT_COMPARABLE


def test_the_declared_score_of_a_verdict_lands_on_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD 8 puts what a verdict is worth in config; the comparison publishes it.

    The evaluators each wrote their own `1.0` and `0.0`, so `SCORING_FULL_SCORE`
    and `SCORING_MISSING_SCORE` were knobs that changed nothing a report showed
    (D-121).
    """
    from app.core.config import get_settings

    monkeypatch.setenv("SCORING_FULL_SCORE", "0.9")
    monkeypatch.setenv("SCORING_MISSING_SCORE", "0.1")
    get_settings.cache_clear()
    try:
        assert evaluate(numeric("1500", Operator.GTE, "1800"), ValueType.NUMERIC).score == 0.9
        assert (
            evaluate(numeric("1500", Operator.GTE, "1200"), ValueType.NUMERIC).score == 0.1
        )
    finally:
        get_settings.cache_clear()


def test_the_partial_tolerance_comes_from_config_not_from_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rules the evaluators apply are policy, not code (PRD 13 - Config).

    The same comparison is a capability failure at the default tolerance and a
    partial match at a looser one, purely because the environment changed.
    """
    from app.core.config import get_settings

    request = numeric("1500", Operator.GTE, "1400")
    assert evaluate(request, ValueType.NUMERIC).status is RequirementStatus.PARTIAL_MATCH

    monkeypatch.setenv("COMPARISON_NUMERIC_PARTIAL_TOLERANCE", "0.01")
    get_settings.cache_clear()
    try:
        result = evaluate(request, ValueType.NUMERIC)
        assert result.status is RequirementStatus.MISSING_CAPABILITY
    finally:
        get_settings.cache_clear()


def test_the_score_band_comes_from_config_not_from_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The partial-match score band is configured, not written into the evaluator."""
    from app.core.config import get_settings

    request = numeric("1500", Operator.GTE, "1400")
    monkeypatch.setenv("COMPARISON_PARTIAL_SCORE_MIN", "0.1")
    monkeypatch.setenv("COMPARISON_PARTIAL_SCORE_MAX", "0.2")
    get_settings.cache_clear()
    try:
        result = evaluate(request, ValueType.NUMERIC)
        assert 0.1 <= result.score <= 0.2
    finally:
        get_settings.cache_clear()
