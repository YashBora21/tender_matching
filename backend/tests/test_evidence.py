"""Evidence consolidation tests: conflicts are detected, not guessed away (FR-25)."""

from __future__ import annotations

from app.services.match.evidence import (
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
    is_secondary: bool = False,
) -> Evidence:
    return Evidence(
        attribute="rotational_speed",
        value=value,
        unit=unit,
        document_class=document_class,
        document_id=f"doc-{value}",
        is_secondary=is_secondary,
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


def test_a_secondary_document_never_overrides_the_manufacturer() -> None:
    """A distributor's datasheet disagrees with the manufacturer's. The
    manufacturer's own document wins (FR-7)."""
    result = consolidate(
        "rotational_speed",
        [
            fact("1200", DocumentClass.DATASHEET, is_secondary=True),
            fact("1500", DocumentClass.DATASHEET),
        ],
    )
    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.value == "1500"
    assert result.selected.is_secondary is False


def test_a_secondary_document_does_not_outrank_a_higher_class() -> None:
    """The secondary flag separates documents of the *same* class only."""
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.DATASHEET, is_secondary=True),
            fact("1200", DocumentClass.COMPLIANCE_SCHEDULE),
        ],
    )
    assert result.conflict is False
    assert result.selected is not None
    assert result.selected.value == "1200"


def test_two_secondary_documents_that_disagree_still_conflict() -> None:
    """Two distributors disagreeing is a conflict, not a guess."""
    result = consolidate(
        "rotational_speed",
        [
            fact("1500", DocumentClass.DATASHEET, is_secondary=True),
            fact("1200", DocumentClass.DATASHEET, is_secondary=True),
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


def test_every_fact_gets_exactly_one_audit_entry() -> None:
    """The audit records each fact once.

    A fact that is written twice - once claiming to have been selected and
    once claiming to have lost - makes the record of how the outcome was
    reached wrong, which is the one thing the audit exists to get right.
    """
    facts = [
        fact("1500", DocumentClass.DATASHEET),
        fact("1200", DocumentClass.COMPLIANCE_SCHEDULE),
    ]
    result = consolidate("rotational_speed", facts)
    assert len(result.audit) == len(facts)
    assert [entry.evidence.value for entry in result.audit] == ["1200", "1500"]
    assert result.audit[0].disposition is Disposition.SELECTED
    assert result.audit[1].disposition is Disposition.LOWER_PRIORITY


def test_a_conflict_audits_every_fact_once() -> None:
    facts = [
        fact("1500", DocumentClass.DATASHEET),
        fact("1200", DocumentClass.DATASHEET),
    ]
    result = consolidate("rotational_speed", facts)
    assert len(result.audit) == len(facts)
    assert all(entry.disposition is Disposition.CONFLICT for entry in result.audit)
