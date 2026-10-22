"""Matching: a requirement against the corpus (FR-21 to FR-33).

The flow, for one requirement:

    retrieve -> extract -> verify -> compare -> a result

- **retrieve** finds the passages that could answer it (FR-17, FR-18).
- **extract** asks the LLM what each passage states, with the quote it
  states it in (FR-22).
- **verify** runs the four layers: the quote is on the page, the passage is
  about this product, the passage states this attribute, the units are
  comparable (FR-23, FR-24).
- **compare** is deterministic. The LLM never decides whether a capability
  matches (FR-27, FR-28).

A requirement nothing answers is *no evidence found*: it counts towards
coverage and not towards the score. It is never a missing capability
(PRD 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.comparison.registry import ComparisonRequest, evaluate
from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    FactVerificationStatus,
    RequirementStatus,
    ValueType,
    VerificationOutcome,
)
from app.services.facts import extract_fact
from app.services.llm import LLMClient
from app.services.retrieval import Candidate, search
from app.services.verifier import verify

logger = get_logger(__name__)


@dataclass
class MatchResult:
    """What one requirement came to."""

    requirement_text: str
    status: RequirementStatus = RequirementStatus.NO_EVIDENCE_FOUND
    score: float = 0.0
    verification_outcome: VerificationOutcome = VerificationOutcome.NOT_CHECKED
    applicability: Applicability = Applicability.MODEL_LEVEL
    capability_text: Optional[str] = None
    quote: Optional[str] = None
    page: Optional[int] = None
    document_id: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    # What the passage stated, for the fact row.
    attribute: Optional[str] = None
    fact_value: Optional[str] = None
    fact_unit: Optional[str] = None
    value_type: Optional[ValueType] = None
    review_required: bool = False
    review_reason: Optional[str] = None
    confidence: ConfidenceLevel = ConfidenceLevel.LOW
    # The reason the status is what it is, in words.
    reason: str = ""
    # The candidates that were read.
    candidates: int = 0
    llm_calls: int = 0


def _confidence(
    verification_borderline: bool,
    ocr: bool,
    score: float,
) -> ConfidenceLevel:
    """How sure the answer is (PRD 9).

    The rule-based flags override the score: a borderline verification or a
    quote read from a scanned page is never high confidence, however well the
    capability matched.
    """
    if verification_borderline or ocr:
        return ConfidenceLevel.LOW
    if score >= 0.99:
        return ConfidenceLevel.HIGH
    if score > 0:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def match_requirement(
    session: Session,
    requirement_text: str,
    *,
    requirement_value: Optional[str] = None,
    requirement_unit: Optional[str] = None,
    requirement_operator: Optional[str] = None,
    manufacturer_name: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 20,
    client: Optional[LLMClient] = None,
) -> MatchResult:
    """Answer one requirement from the corpus."""
    result = MatchResult(requirement_text=requirement_text)
    settings = get_settings()

    candidates = search(
        session,
        requirement_text,
        limit=limit,
        manufacturer_name=manufacturer_name,
        category=category,
    )
    result.candidates = len(candidates)
    if not candidates:
        result.reason = "the corpus holds nothing that answers this requirement"
        result.review_required = True
        result.review_reason = result.reason
        return result

    best: Optional[MatchResult] = None
    for candidate in candidates:
        outcome = _match_candidate(
            session,
            candidate,
            requirement_text,
            requirement_value=requirement_value,
            requirement_unit=requirement_unit,
            requirement_operator=requirement_operator,
            client=client,
        )
        result.llm_calls += outcome.llm_calls
        if best is None or _is_better(outcome, best):
            best = outcome

    if best is None:
        result.reason = "no passage stated a capability for this requirement"
        result.review_required = True
        result.review_reason = result.reason
        return result

    return best


def _is_better(candidate: MatchResult, current: MatchResult) -> bool:
    """Whether a candidate beats the one found so far.

    A verified match beats an unverified one, and a match beats no evidence.
    """
    order = [
        RequirementStatus.FULL_MATCH,
        RequirementStatus.PARTIAL_MATCH,
        RequirementStatus.MISSING_CAPABILITY,
        RequirementStatus.UNSUPPORTED_REQUIREMENT,
        RequirementStatus.NO_EVIDENCE_FOUND,
    ]
    try:
        candidate_rank = order.index(candidate.status)
        current_rank = order.index(current.status)
    except ValueError:
        return False
    if candidate_rank != current_rank:
        return candidate_rank < current_rank
    return candidate.score > current.score


def _match_candidate(
    session: Session,
    candidate: Candidate,
    requirement_text: str,
    *,
    requirement_value: Optional[str] = None,
    requirement_unit: Optional[str] = None,
    requirement_operator: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> MatchResult:
    """Read one passage and say what it means for the requirement."""
    result = MatchResult(
        requirement_text=requirement_text,
        manufacturer=candidate.manufacturer,
        model=candidate.model,
        page=candidate.page,
        document_id=candidate.document_id,
    )

    extracted = extract_fact(requirement_text, candidate.text, client=client)
    result.llm_calls = 1
    if extracted.fact is None:
        result.reason = extracted.error or "the passage could not be read"
        result.review_required = True
        result.review_reason = result.reason
        return result

    fact = extracted.fact
    if not fact.found:
        result.reason = "the passage does not state this capability"
        return result

    page_text = _page_text(session, candidate)
    report = verify(
        quote=fact.quote,
        page_text=page_text,
        passage=candidate.text,
        manufacturer=candidate.manufacturer,
        model=fact.model or candidate.model,
        family=candidate.family,
        attribute=fact.attribute,
        value=fact.value,
        requirement_unit=requirement_unit,
        capability_unit=fact.unit,
        operator_is_known=bool(fact.operator),
        convertible=(
            None
            if not (requirement_unit and fact.unit)
            else _convertible(requirement_unit, fact.unit)
        ),
    )

    result.verification_outcome = report.outcome
    result.applicability = report.applicability
    result.quote = fact.quote
    result.capability_text = fact.value
    result.review_required = report.review_required
    result.review_reason = "; ".join(report.reasons) or None

    if not report.passed:
        result.status = RequirementStatus.MISSING_CAPABILITY
        result.reason = "; ".join(report.reasons) or "the claim could not be verified"
        result.confidence = ConfidenceLevel.LOW
        return result

    value_type = _value_type(fact.attribute)
    result.value_type = value_type
    evaluation = evaluate(
        ComparisonRequest(
            requirement_value=requirement_value,
            requirement_operator=_operator(requirement_operator),
            requirement_unit=requirement_unit,
            requirement_text_value=requirement_value,
            requirement_range=None,
            capability_value=fact.value,
            capability_unit=fact.unit,
            capability_text_value=fact.value,
            capability_range=None,
            capability_is_family_level=report.applicability is Applicability.FAMILY_LEVEL,
        ),
        value_type,
    )

    result.status = evaluation.status
    result.score = evaluation.score
    result.reason = evaluation.reason
    if evaluation.needs_review and not result.review_required:
        result.review_required = True
        result.review_reason = evaluation.reason
    result.confidence = _confidence(report.borderline, candidate.ocr, evaluation.score)
    result.attribute = fact.attribute
    result.fact_value = fact.value
    result.fact_unit = fact.unit
    return result


def _operator(value: Optional[str]):
    from app.schemas.enums import Operator

    if not value:
        return Operator.UNKNOWN
    try:
        return Operator(value)
    except ValueError:
        return Operator.UNKNOWN


def store_match(
    session,
    match: MatchResult,
    *,
    run_id: str,
    requirement_id: str,
    manufacturer_id: str,
    extractor_version: Optional[str] = None,
) -> Optional[str]:
    """Write what a match found: the fact, and the result that points at it.

    The fact is written first, because the result carries its id: a reviewer
    who reads "full match" can get back to the sentence that said so.
    """
    from app.services.findings import (
        StoredFact,
        _attribute_id,
        product_id_for,
        store_fact,
        store_result,
    )

    fact_id: Optional[str] = None
    if match.document_id and match.manufacturer and match.model:
        product_id = product_id_for(session, match.manufacturer, match.model)
        if product_id:
            fact_id = store_fact(
                session,
                StoredFact(
                    product_id=product_id,
                    document_id=match.document_id,
                    attribute_id=_attribute_id(session, match.attribute),
                    original_name=match.attribute,
                    value=match.fact_value,
                    unit=match.fact_unit,
                    value_type=match.value_type,
                    conditions=match.model,
                    page=match.page,
                    quote=match.quote,
                    verification_status=(
                        FactVerificationStatus.VERIFIED
                        if match.verification_outcome
                        in (VerificationOutcome.PASSED, VerificationOutcome.FAMILY_LEVEL)
                        else FactVerificationStatus.NEEDS_REVIEW
                    ),
                    extractor_version=extractor_version,
                ),
            )

    return store_result(
        session,
        run_id=run_id,
        requirement_id=requirement_id,
        manufacturer_id=manufacturer_id,
        status=match.status,
        score=match.score,
        verification_outcome=match.verification_outcome,
        applicability=match.applicability,
        capability_text=match.capability_text,
        evidence_ref=fact_id,
        confidence=match.confidence,
        review_required=match.review_required,
        review_reason=match.review_reason,
    )


def _value_type(attribute: Optional[str]):
    """The value type of an attribute, from the vocabulary.

    The type decides which evaluator runs, and it comes from the vocabulary
    rather than from a list here: an attribute the vocabulary does not know
    has no type, and its requirement goes to review (FR-29).
    """
    from app.comparison.attributes import attribute_by_term

    spec = attribute_by_term(attribute)
    return spec.value_type if spec is not None else None


def _convertible(requirement_unit: str, capability_unit: str) -> bool:
    from app.comparison.units import is_compatible

    return is_compatible(requirement_unit, capability_unit)


def _page_text(session: Session, candidate: Candidate) -> Optional[str]:
    """The text of the page a passage came from, for the citation check."""
    from app.repositories.schema import document as document_table

    row = session.execute(
        document_table.c.path.where(document_table.c.id == candidate.document_id)
    ).first()
    if row is None or not row[0]:
        return None
    import pymupdf

    path = _corpus_file(row[0])
    if path is None or not path.exists():
        return None
    try:
        document = pymupdf.open(path)
    except Exception:  # noqa: BLE001 - a file that cannot be opened has no text
        return None
    try:
        if candidate.page is None or candidate.page > document.page_count:
            return None
        return document[candidate.page - 1].get_text() or ""
    finally:
        document.close()


def _corpus_file(relative: str) -> Optional[Path]:
    """The file a document's path names, in the corpus."""
    from app.core.config import get_settings

    data_dir = get_settings().data_dir
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    # The corpus keeps the files under the manufacturer's own directory.
    for base in (data_dir / "manufacturers", data_dir / "tenders"):
        for found in base.glob(f"*/raw/{relative}"):
            return found
        for found in base.glob(f"*/{relative}"):
            return found
    return None
