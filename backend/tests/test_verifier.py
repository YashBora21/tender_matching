"""Verifier tests: the four layers gate every claim (FR-23, FR-24)."""

from __future__ import annotations

from app.schemas.enums import Applicability, RequirementStatus, VerificationOutcome
from app.services.match.verify import (
    check_citation,
    check_comparison,
    check_entity,
    check_semantic,
    listed_set_exclusion,
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


def test_the_entity_layer_uses_the_document_metadata_not_the_passage() -> None:
    """A datasheet page that does not repeat its own manufacturer is still the
    manufacturer's document. The old check searched the passage for the name
    and failed every page that did not say it (D-94)."""
    result = check_entity(
        document_manufacturer="Acme Valves",
        document_model="GV-100",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
    )
    assert result.passed is True
    assert result.borderline is False


def test_the_entity_layer_fails_when_the_document_belongs_to_another_manufacturer() -> None:
    result = check_entity(
        document_manufacturer="Other Valves",
        document_model="GV-100",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
    )
    assert result.passed is False


def test_the_entity_layer_fails_when_the_document_names_another_model() -> None:
    result = check_entity(
        document_manufacturer="Acme Valves",
        document_model="GV-200",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
    )
    assert result.passed is False


def test_the_entity_layer_marks_family_evidence_as_borderline() -> None:
    result = check_entity(
        document_manufacturer="Acme Valves",
        document_family="GV",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
        expected_family="GV",
    )
    assert result.passed is True
    assert result.borderline is True


def test_a_document_with_no_model_declaration_is_family_level_evidence() -> None:
    result = check_entity(
        document_manufacturer="Acme Valves",
        expected_manufacturer="Acme Valves",
    )
    assert result.passed is True
    assert result.borderline is True


def test_a_quote_is_normalised_like_the_chunk_it_came_from() -> None:
    """A control character in the page's text layer does not break a quote.

    The reader strips control characters before storing a chunk, so the
    verifier has to strip them from the page it reads back too (D-94).
    """
    page = "Maximum operating\x0ctemperature is 60 degC."
    result = check_citation("Maximum operating temperature is 60 degC.", page)
    assert result.passed is True


def test_the_semantic_layer_fails_when_the_value_is_absent() -> None:
    result = check_semantic("The body is SS316", "material_grade", "SS304")
    assert result.passed is False


def test_the_semantic_layer_passes_when_the_value_is_present() -> None:
    result = check_semantic("Maximum operating temperature is 60 degC", "operating_temperature", "60")
    assert result.passed is True


def test_the_comparison_layer_fails_on_incompatible_units() -> None:
    result = check_comparison("rpm", "kg")
    assert result.passed is False


def test_a_listed_set_that_excludes_the_required_value_is_missing_capability() -> None:
    """The document states the attribute and lists the values it is offered
    in. The required value is not one of them: that is a failing value, not an
    absence of evidence (PRD 4)."""
    finding = listed_set_exclusion(
        "Body Style: Flanged end / Butt weld end",
        'Three piece ball valves 2" Screwed end ASME class 150',
    )

    assert finding is not None
    assert finding.status is RequirementStatus.MISSING_CAPABILITY
    assert finding.attribute == "connection_type"
    assert finding.required == "screwed end"
    assert finding.values == ("Flanged end", "Butt weld end")
    assert finding.quote == "Body Style: Flanged end / Butt weld end"


def test_a_silent_document_is_not_a_listed_set() -> None:
    """A page that never lists the attribute is silent, and silence stays
    No evidence found: it is never read as Missing capability (AGENTS #4)."""
    finding = listed_set_exclusion(
        "Maximum operating temperature is 60 degC.",
        'Three piece ball valves 2" Screwed end ASME class 150',
    )

    assert finding is None


def test_a_failed_layer_makes_the_whole_verification_fail() -> None:
    report = verify(
        quote="The valve is rated at 250 degC.",
        page_text=PAGE_TEXT,
        passage="The body is SS316",
        document_manufacturer="Acme Valves",
        document_model="GV-100",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
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
        document_manufacturer="Acme Valves",
        document_family="GV",
        expected_manufacturer="Acme Valves",
        expected_model="GV-100",
        expected_family="GV",
        attribute="material_grade",
        value="SS316",
    )
    assert report.applicability is Applicability.FAMILY_LEVEL
    assert report.outcome is VerificationOutcome.FAMILY_LEVEL
    assert report.review_required is True
    # A borderline layer *passes*, so `reasons` alone is empty and the reviewer
    # would be flagged with nothing to look at.
    assert report.review_reasons == [
        "the document covers the GV family, not the model GV-100"
    ]
