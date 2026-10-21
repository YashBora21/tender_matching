"""Scoring tests: compatibility, coverage and the mandatory-failure rule (PRD 8)."""

from __future__ import annotations

from app.core.config import ScoringPolicy
from app.schemas.enums import Applicability, RequirementStatus, VerificationOutcome
from app.services.scoring import ScoredResult, rank, score_manufacturer

POLICY = ScoringPolicy()


def result(
    status: RequirementStatus,
    score: float,
    *,
    mandatory: bool = True,
    applicability: Applicability = Applicability.MODEL_LEVEL,
    review_required: bool = False,
) -> ScoredResult:
    return ScoredResult(
        requirement_id="r",
        manufacturer_id="m",
        status=status,
        score=score,
        mandatory=mandatory,
        verification_outcome=VerificationOutcome.PASSED,
        applicability=applicability,
        review_required=review_required,
    )


def test_compatibility_weights_mandatory_requirements_double() -> None:
    # One mandatory full match and one optional full match: (1.0*2 + 1.0*1) / 3.
    summary = score_manufacturer(
        "m",
        [
            result(RequirementStatus.FULL_MATCH, 1.0, mandatory=True),
            result(RequirementStatus.FULL_MATCH, 1.0, mandatory=False),
        ],
        POLICY,
    )
    assert summary.compatibility_percent == 100.0


def test_a_missing_mandatory_requirement_fails_the_manufacturer() -> None:
    summary = score_manufacturer(
        "m",
        [
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.MISSING_CAPABILITY, 0.0),
        ],
        POLICY,
    )
    assert summary.fails_mandatory is True


def test_unsupported_and_no_evidence_are_excluded_from_compatibility() -> None:
    summary = score_manufacturer(
        "m",
        [
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.UNSUPPORTED_REQUIREMENT, 0.0),
            result(RequirementStatus.NO_EVIDENCE_FOUND, 0.0),
        ],
        POLICY,
    )
    # Only the full match counts towards the weighted total.
    assert summary.compatibility_percent == 100.0


def test_coverage_counts_requirements_that_had_evidence() -> None:
    summary = score_manufacturer(
        "m",
        [
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.NO_EVIDENCE_FOUND, 0.0),
        ],
        POLICY,
    )
    assert summary.coverage_percent == 50.0


def test_family_level_evidence_is_capped_at_the_configured_score() -> None:
    summary = score_manufacturer(
        "m",
        [
            result(
                RequirementStatus.PARTIAL_MATCH,
                1.0,
                applicability=Applicability.FAMILY_LEVEL,
            )
        ],
        POLICY,
    )
    assert summary.compatibility_percent == 50.0


def test_ranking_prefers_compatibility_then_coverage() -> None:
    high = score_manufacturer(
        "a",
        [result(RequirementStatus.FULL_MATCH, 1.0)],
        POLICY,
    )
    thin = score_manufacturer(
        "b",
        [
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.NO_EVIDENCE_FOUND, 0.0),
        ],
        POLICY,
    )
    ordered = rank([thin, high])
    assert [s.manufacturer_id for s in ordered] == ["a", "b"]


def test_status_counts_are_reported() -> None:
    summary = score_manufacturer(
        "m",
        [
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.FULL_MATCH, 1.0),
            result(RequirementStatus.NO_EVIDENCE_FOUND, 0.0),
        ],
        POLICY,
    )
    assert summary.status_counts["full_match"] == 2
    assert summary.status_counts["no_evidence_found"] == 1
