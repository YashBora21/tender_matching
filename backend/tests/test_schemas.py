"""Schema tests: the PRD's status vocabulary and the unified requirement record."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.enums import LedgerStatus, RequirementStatus, RequirementType
from app.schemas.models import Requirement


def test_requirement_status_has_exactly_the_five_prd_statuses() -> None:
    assert {s.value for s in RequirementStatus} == {
        "full_match",
        "partial_match",
        "missing_capability",
        "unsupported_requirement",
        "no_evidence_found",
    }


def test_ledger_status_has_a_failed_state() -> None:
    # FR-T2: a page that could not be extracted or OCRed is not "processed".
    assert LedgerStatus.FAILED in set(LedgerStatus)
    assert {s.value for s in LedgerStatus} == {"parsed", "skipped", "ocr", "failed"}


def test_requirement_type_matches_prd_gate() -> None:
    assert {t.value for t in RequirementType} == {
        "technical",
        "certification",
        "commercial",
        "eligibility",
        "informational",
        "unknown",
    }


def test_requirement_keeps_item_id_and_source_provenance() -> None:
    # FR-T9: item_id is carried from extraction through reporting.
    requirement = Requirement(
        requirement_id="req-1",
        tender_id="tender-1",
        item_id="item-gate-valve",
        source_page=3,
        source_text="Body shall be SS316",
        extraction_method="prose",
        requirement_type=RequirementType.TECHNICAL,
    )
    assert requirement.item_id == "item-gate-valve"
    assert requirement.source_page == 3
    assert requirement.review_required is False


def test_requirement_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id="req-1",
            tender_id="tender-1",
            source_page=1,
            source_text="x",
            extraction_method="prose",
            requirement_type=RequirementType.TECHNICAL,
            invented_spec="must not be silently accepted",
        )
