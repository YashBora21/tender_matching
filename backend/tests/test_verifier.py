"""Verifier tests: the four layers gate every claim (FR-23, FR-24)."""

from __future__ import annotations

from app.schemas.enums import Applicability, VerificationOutcome
from app.services.verifier import (
    check_citation,
    check_comparison,
    check_entity,
    check_semantic,
    verify,
)

PAGE_TEXT = """
Gate valve body shall be SS316.
Maximum operating temperature is 60 degC.
"""


def test_a_quote_on_the_page_passes_the_citation_layer() -> None:
    result = check_citation("Maximum operating temperature is 60 degC.", PAGE_TEXT)
    assert result.passed is True


def test_a_quote_that_is_not_on_the_page_fails() -> None:
    result = check_citation("The valve is rated at 250 degC.", PAGE_TEXT)
    assert result.passed is False


def test_a_quote_is_matched_even_when_it_is_rewrapped() -> None:
    result = check_citation("Maximum operating\ntemperature    is 60 degC.", PAGE_TEXT)
    assert result.passed is True


def test_an_empty_quote_fails_rather_than_passing() -> None:
    result = check_citation("", PAGE_TEXT)
    assert result.passed is False


def test_a_quote_longer_than_the_page_fails() -> None:
    result = check_citation("x" * 5000, PAGE_TEXT)
    assert result.passed is False


def test_the_entity_layer_fails_when_the_manufacturer_is_absent() -> None:
    result = check_entity("The body is SS316", "Acme Valves", "GV-100")
    assert result.passed is False


def test_the_entity_layer_marks_family_evidence_as_borderline() -> None:
    result = check_entity("The GV series body is SS316", "Acme Valves", "GV-100", "GV")
    assert result.passed is True
    assert result.borderline is True


def test_the_semantic_layer_fails_when_the_value_is_absent() -> None:
    result = check_semantic("The body is SS316", "material_grade", "SS304")
    assert result.passed is False


def test_the_semantic_layer_passes_when_the_value_is_present() -> None:
    result = check_semantic("Maximum operating temperature is 60 degC", "operating_temperature", "60")
    assert result.passed is True


def test_the_comparison_layer_fails_on_incompatible_units() -> None:
    result = check_comparison("rpm", "kg")
    assert result.passed is False


def test_a_failed_layer_makes_the_whole_verification_fail() -> None:
    report = verify(
        quote="The valve is rated at 250 degC.",
        page_text=PAGE_TEXT,
        passage="The body is SS316",
        manufacturer="Acme Valves",
        model="GV-100",
        attribute="operating_temperature",
        value="250",
        requirement_unit="degC",
        capability_unit="degC",
    )
    assert report.passed is False
    assert report.outcome is VerificationOutcome.FAILED
    assert report.review_required is True
    assert report.reasons


def test_family_level_evidence_is_reported_as_family_level_not_as_a_pass() -> None:
    report = verify(
        quote="The GV series body is SS316",
        page_text="The GV series body is SS316",
        passage="The GV series body is SS316",
        manufacturer="Acme Valves",
        model="GV-100",
        family="GV",
        attribute="material_grade",
        value="SS316",
    )
    assert report.applicability is Applicability.FAMILY_LEVEL
    assert report.outcome is VerificationOutcome.FAMILY_LEVEL
    assert report.review_required is True
