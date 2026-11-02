"""Scoring and manufacturer summary (PRD 8, FR-31, FR-32).

Scoring is deterministic and reads its constants from the `ScoringPolicy` in
config, never from literals here (D-13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.core.config import ScoringPolicy
from app.schemas.enums import Applicability, RequirementStatus, VerificationOutcome

# The version of the scoring rules below. It is written into every report so
# a score can be read against the rules that produced it (FR-34): a report
# that does not name its policy cannot be compared with one that does, and a
# change here is a change to what every number means.
SCORING_POLICY_VERSION = "1"


@dataclass
class ScoredResult:
    """One requirement assessed against one manufacturer."""

    requirement_id: str
    manufacturer_id: str
    status: RequirementStatus
    score: float
    mandatory: bool = True
    verification_outcome: VerificationOutcome = VerificationOutcome.PASSED
    applicability: Applicability = Applicability.MODEL_LEVEL
    review_required: bool = False
    review_reason: Optional[str] = None
    capability_text: Optional[str] = None
    evidence_ref: Optional[str] = None
    confidence: Optional[str] = None


@dataclass
class ManufacturerSummary:
    """The PRD 8 per-manufacturer summary (FR-32)."""

    manufacturer_id: str
    compatibility_percent: float
    coverage_percent: float
    status_counts: dict[str, int] = field(default_factory=dict)
    fails_mandatory: bool = False
    review_count: int = 0
    ranking_score: tuple = ()


def weight_for(mandatory: bool, policy: ScoringPolicy) -> int:
    return policy.weight_mandatory if mandatory else policy.weight_optional


def is_verifiable(status: RequirementStatus) -> bool:
    """True when the requirement is technical and verifiable.

    An unsupported requirement is not verifiable at all - it is commercial,
    legal or vague, and is listed separately (PRD 4). A no-evidence requirement
    *is* verifiable: it is technical and checkable, the corpus is simply silent.
    It counts towards coverage but not towards the score.
    """
    return status is not RequirementStatus.UNSUPPORTED_REQUIREMENT


def score_manufacturer(
    manufacturer_id: str,
    results: Iterable[ScoredResult],
    policy: ScoringPolicy,
) -> ManufacturerSummary:
    """Compatibility %, coverage % and the mandatory-failure flag for one manufacturer.

    Compatibility weighs the scored requirements (mandatory weight 2, optional 1).
    Coverage counts how many verifiable requirements actually had evidence.
    """
    results = list(results)
    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status.value] = status_counts.get(result.status.value, 0) + 1

    weighted_total = 0.0
    weight_total = 0
    with_evidence = 0
    verifiable = 0
    fails_mandatory = False
    review_count = 0

    for result in results:
        if result.review_required:
            review_count += 1
        if result.status is RequirementStatus.MISSING_CAPABILITY and result.mandatory:
            fails_mandatory = True
        if not is_verifiable(result.status):
            continue
        verifiable += 1
        if result.status is RequirementStatus.NO_EVIDENCE_FOUND:
            continue
        with_evidence += 1
        score = result.score
        # Family-level evidence is capped and never published as a confirmed match.
        if result.applicability is Applicability.FAMILY_LEVEL:
            score = min(score, policy.family_level_cap)
        weighted_total += score * weight_for(result.mandatory, policy)
        weight_total += weight_for(result.mandatory, policy)

    compatibility = (weighted_total / weight_total) if weight_total else 0.0
    coverage = (with_evidence / verifiable) if verifiable else 0.0

    return ManufacturerSummary(
        manufacturer_id=manufacturer_id,
        compatibility_percent=round(compatibility * 100, 2),
        coverage_percent=round(coverage * 100, 2),
        status_counts=status_counts,
        fails_mandatory=fails_mandatory,
        review_count=review_count,
        ranking_score=(compatibility, coverage, -review_count),
    )


def rank(summaries: Iterable[ManufacturerSummary]) -> list[ManufacturerSummary]:
    """Sort by compatibility %, then coverage %, then fewer review flags (PRD 8)."""
    return sorted(summaries, key=lambda s: s.ranking_score, reverse=True)
