"""Evidence consolidation tests: conflicts are detected, not guessed away (FR-25)."""

from __future__ import annotations

from app.services.evidence import (
    Disposition,
    DocumentClass,
    Evidence,
    classify_document,
    consolidate,
)


def fact(
    value: str,
    document_class: DocumentClass = DocumentClass.DATASHEET,
    unit: str | None = None,
) -> Evidence:
    return Evidence(
        attribute="rotational_speed",
        value=value,
        unit=unit,
        document_class=document_class,
        document_id=f"doc-{value}",
    )


def test_documents_are_classified_by_their_name() -> None:
    assert classify_document("GV-datasheet.pdf") is DocumentClass.DATASHEET
    assert classify_document("GV-compliance-schedule.xlsx") is DocumentClass.COMPLIANCE_SCHEDULE
    assert classify_document("GV-technical-offer.pdf") is DocumentClass.TECHNICAL_OFFER
    assert classify_document("tender-specification.pdf") is DocumentClass.SPECIFICATION
    assert classify_document("GV-pricing.xlsx") is DocumentClass.IGNORED


def test_an_unmarked_pdf_defaults_to_a_technical_offer() -> None:
    assert classify_document("GV.pdf") is DocumentClass.TECHNICAL_OFFER


def test_an_unsupported_extension_is_ignored() -> None:
    assert classify_document("notes.txt") is DocumentClass.IGNORED


def test_equivalent_facts_collapse_onto_one() -> None:
    result = consolidate("rotational_speed", [fact("1500"), fact("1500")])
    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.value == "1500"
    dispositions = [entry.disposition for entry in result.audit]
    assert dispositions.count(Disposition.EQUIVALENT_DUPLICATE) == 1


def test_equivalent_values_in_different_units_collapse() -> None:
    result = consolidate(
        "flow_rate",
        [
            Evidence(attribute="flow_rate", value="25", unit="l/min"),
            Evidence(attribute="flow_rate", value="1.5", unit="m**3/hour"),
        ],
    )
    assert result.conflict is False
    assert result.selected is not None


def test_conflicting_values_produce_a_conflict_not_a_winner() -> None:
    result = consolidate("rotational_speed", [fact("1500"), fact("1200")])
    assert result.conflict is True
    assert result.selected is None
    assert "1500" in result.conflicting_values
    assert "1200" in result.conflicting_values
    assert result.review_reason


def test_a_higher_priority_class_resolves_a_conflict() -> None:
    # A compliance schedule outranks a datasheet.
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.DATASHEET),
            fact("1200", DocumentClass.COMPLIANCE_SCHEDULE),
        ],
    )
    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.value == "1200"


def test_a_conflict_inside_the_top_class_is_not_resolved() -> None:
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.COMPLIANCE_SCHEDULE),
            fact("1200", DocumentClass.COMPLIANCE_SCHEDULE),
        ],
    )
    assert result.conflict is True
    assert result.selected is None


def test_a_specification_is_never_technical_evidence() -> None:
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.SPECIFICATION),
            fact("1200", DocumentClass.DATASHEET),
        ],
    )
    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.value == "1200"


def test_an_ignored_document_is_never_evidence() -> None:
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.IGNORED),
            fact("1200", DocumentClass.DATASHEET),
        ],
    )
    assert result.selected is not None
    assert result.selected.value == "1200"
