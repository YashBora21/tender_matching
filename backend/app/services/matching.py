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
from pathlib import Path
from typing import Optional

from sqlalchemy import select
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


def build_query(
    item_title: Optional[str] = None,
    requirement_text: Optional[str] = None,
    *,
    attribute: Optional[str] = None,
    value: Optional[str] = None,
    unit: Optional[str] = None,
) -> str:
    """The text a requirement is retrieved with (D-96).

    A stored clause is often a bare fragment - "1500 RPM", "Class 150" - and
    on its own it names no subject: a search for it matches every page that
    mentions the number. The query carries three things instead of one:

    - the BOQ item's title, which names the thing being bought;
    - the clause itself, which names the obligation;
    - the attribute, value and unit the normalizer read, which name what is
      being asked even when the clause is abbreviated.

    A part is added once. The item title and the clause usually repeat each
    other ("GATE VALVE, SIZE-150 MM, CLASS-150" and "Flanged Gate valve class
    150"), and repeating a word would weight it twice in the keyword rank.
    """
    parts: list[str] = []
    seen: set[str] = set()

    def add(text: Optional[str]) -> None:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        key = cleaned.casefold()
        if key in seen:
            return
        seen.add(key)
        parts.append(cleaned)

    add(item_title)
    add(requirement_text)
    add(
        " ".join(
            part
            for part in ((attribute or "").replace("_", " ").strip(), value, unit)
            if part
        )
    )
    return " ".join(parts)


def match_requirement(
    session: Session,
    requirement_text: str,
    *,
    item_title: Optional[str] = None,
    requirement_attribute: Optional[str] = None,
    requirement_value: Optional[str] = None,
    requirement_unit: Optional[str] = None,
    requirement_operator: Optional[str] = None,
    manufacturer_name: Optional[str] = None,
    model: Optional[str] = None,
    category: Optional[str] = None,
    limit: Optional[int] = None,
    client: Optional[LLMClient] = None,
) -> MatchResult:
    """Answer one requirement from the corpus."""
    result = MatchResult(requirement_text=requirement_text)
    settings = get_settings()
    limit = limit or settings.match_max_candidates

    # The fact cache is checked first: a fact that was verified once is the
    # answer, and the passage it came from has not changed (FR-21).
    cached = _cached_fact(session, requirement_text, manufacturer_name, model)
    if cached is not None:
        return _from_cached_fact(
            cached,
            requirement_text,
            requirement_value=requirement_value,
            requirement_unit=requirement_unit,
            requirement_operator=requirement_operator,
            manufacturer_name=manufacturer_name,
            model=model,
        )

    # The search is asked with the item's title and the read attribute, not
    # with the bare clause: a fragment names no subject on its own (D-96).
    query = build_query(
        item_title,
        requirement_text,
        attribute=requirement_attribute,
        value=requirement_value,
        unit=requirement_unit,
    )
    candidates = search(
        session,
        query,
        limit=limit,
        manufacturer_name=manufacturer_name,
        category=category,
        model=model,
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
            manufacturer_name=manufacturer_name,
            model=model,
            client=client,
        )
        result.llm_calls += outcome.llm_calls
        if best is None or _is_better(outcome, best):
            best = outcome
        if best.status is RequirementStatus.FULL_MATCH:
            # A verified full match is the best answer a passage can give, so
            # the remaining passages would only cost calls to confirm it
            # (D19). A weaker result keeps searching: another passage may
            # answer better.
            break

    if best is None:
        result.reason = "no passage stated a capability for this requirement"
        result.review_required = True
        result.review_reason = result.reason
        return result

    # The best candidate's verdict, with the run's counts kept: how many
    # passages were read and how many calls that took are the run's, not
    # one candidate's.
    best.candidates = result.candidates
    best.llm_calls = result.llm_calls
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
    manufacturer_name: Optional[str] = None,
    model: Optional[str] = None,
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
    if extracted.error:
        result.reason = extracted.error or "the passage could not be read"
        result.review_required = True
        result.review_reason = result.reason
        return result

    # A passage may state more than one capability. The one that answers the
    # requirement is the one the comparison runs on; the rest are not this
    # requirement's business.
    fact = next((item for item in extracted.facts if item.found), None)
    if fact is None:
        result.reason = "the passage does not state this capability"
        return result

    page_text = _page_text(session, candidate)
    report = verify(
        quote=fact.quote,
        page_text=page_text,
        passage=candidate.text,
        # Entity validity is decided from the document row, never from the
        # passage: a datasheet page that does not repeat its own manufacturer
        # is still the manufacturer's document (D-94).
        document_manufacturer=candidate.manufacturer,
        document_model=candidate.model,
        document_family=candidate.family,
        document_model_or_family=candidate.model_or_family,
        expected_manufacturer=manufacturer_name,
        expected_model=model,
        is_family_level=candidate.is_family_level,
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
    if candidate.is_family_level:
        # The document is about a family of products, so the evidence is not
        # confirmed for this model: uncertain applicability, not a match
        # (PRD 4).
        report.applicability = Applicability.FAMILY_LEVEL

    result.verification_outcome = report.outcome
    result.applicability = report.applicability
    result.quote = fact.quote
    result.capability_text = fact.value
    result.review_required = report.review_required
    result.review_reason = "; ".join(report.reasons) or None

    if not report.passed:
        # The claim could not be checked against the page, the product or the
        # attribute. That is not the tender saying the capability is absent:
        # an unverified claim proves nothing about the manufacturer, so the
        # requirement is *no evidence found* and goes to review (PRD 4,
        # AGENTS #1, D14). Recording it as missing would turn a failed check
        # into a statement about the product.
        result.status = RequirementStatus.NO_EVIDENCE_FOUND
        result.reason = "the claim could not be verified: " + (
            "; ".join(report.reasons) or "no layer confirmed it"
        )
        result.confidence = ConfidenceLevel.LOW
        result.review_required = True
        result.review_reason = result.reason
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
            attribute=_canonical_term(fact.attribute),
            capability_is_family_level=report.applicability is Applicability.FAMILY_LEVEL,
        ),
        value_type,
    )

    result.status = evaluation.status
    result.score = evaluation.score
    result.reason = evaluation.reason
    if evaluation.code is not None:
        # The comparison could not run, so this requirement was never checked
        # against this product. That is a limit of ours, not a finding about
        # the manufacturer: the status stays *no evidence found* (D-73) and
        # the code says which limit stopped it (D-86).
        result.verification_outcome = VerificationOutcome.NOT_CHECKED
        result.review_required = True
        result.review_reason = "; ".join(
            part
            for part in (
                result.review_reason,
                f"{evaluation.code.value}: {evaluation.reason}",
            )
            if part
        )
        result.reason = result.review_reason
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


def _cached_fact(
    session: Session,
    requirement_text: str,
    manufacturer_name: Optional[str],
    model: Optional[str],
) -> Optional[dict]:
    """The fact already held for this product and attribute (FR-21).

    The cache is keyed on the product and the attribute, so it can only be
    read when both are known: a fact about one product says nothing about
    another.
    """
    from app.comparison.attributes import find_attribute
    from app.services.findings import cached_fact, product_id_for

    if not (manufacturer_name and model):
        return None
    spec = find_attribute(requirement_text)
    if spec is None:
        return None
    product_id = product_id_for(session, manufacturer_name, model)
    if not product_id:
        return None
    return cached_fact(session, product_id, spec.canonical_term)


def _from_cached_fact(
    cached: dict,
    requirement_text: str,
    *,
    requirement_value: Optional[str],
    requirement_unit: Optional[str],
    requirement_operator: Optional[str],
    manufacturer_name: Optional[str],
    model: Optional[str],
) -> MatchResult:
    """The answer a cached fact gives, without reading a passage again.

    The comparison still runs: the fact holds the value the document states,
    and whether that value meets the requirement is decided the same way
    every time (FR-28).
    """
    result = MatchResult(
        requirement_text=requirement_text,
        manufacturer=manufacturer_name,
        model=model,
        page=cached["page"],
        document_id=cached["document_id"],
        quote=cached["quote"],
        capability_text=cached["value"],
        attribute=cached["attribute"],
        fact_value=cached["value"],
        fact_unit=cached["unit"],
    )

    value_type = _value_type(cached["attribute"])
    family_level = cached["is_family_level"]
    evaluation = evaluate(
        ComparisonRequest(
            requirement_value=requirement_value,
            requirement_operator=_operator(requirement_operator),
            requirement_unit=requirement_unit,
            requirement_text_value=requirement_value,
            requirement_range=None,
            capability_value=cached["value"],
            capability_unit=cached["unit"],
            capability_text_value=cached["value"],
            capability_range=None,
            attribute=_canonical_term(cached["attribute"]),
            capability_is_family_level=family_level,
        ),
        value_type,
    )
    result.status = evaluation.status
    result.score = evaluation.score
    result.reason = evaluation.reason
    result.verification_outcome = (
        VerificationOutcome.FAMILY_LEVEL if family_level else VerificationOutcome.PASSED
    )
    result.applicability = (
        Applicability.FAMILY_LEVEL if family_level else Applicability.MODEL_LEVEL
    )
    result.value_type = value_type
    if evaluation.needs_review or family_level:
        result.review_required = True
        result.review_reason = evaluation.reason or (
            "the evidence covers a family of products, not this model"
        )
    result.confidence = _confidence(False, False, evaluation.score)
    return result


def store_fact(
    session,
    match: MatchResult,
    *,
    extractor_version: Optional[str] = None,
) -> Optional[str]:
    """Write the fact a match found, and return its id.

    The fact is filed under the canonical attribute, so the cache can find it
    again by the term (FR-21). The wording the model used is kept as the
    original name.
    """
    from app.services.findings import (
        StoredFact,
        _attribute_id,
        product_id_for,
        store_fact as write_fact,
    )

    if not (match.document_id and match.manufacturer and match.model):
        return None
    product_id = product_id_for(session, match.manufacturer, match.model)
    if not product_id:
        return None

    attribute_term = _canonical_attribute(session, match.attribute)
    return write_fact(
        session,
        StoredFact(
            product_id=product_id,
            document_id=match.document_id,
            attribute_id=_attribute_id(session, attribute_term),
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
    from app.services.findings import store_result

    fact_id = store_fact(session, match, extractor_version=extractor_version)

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

    The model answers in whatever words the document used ("body material"),
    and those are often a synonym of the canonical term. The synonym is
    resolved here, or a correctly read fact would be filed as an attribute
    nothing can evaluate.
    """
    from app.comparison.attributes import attribute_by_term, find_attribute

    if not attribute:
        return None
    spec = attribute_by_term(attribute) or find_attribute(attribute)
    return spec.value_type if spec is not None else None


def _canonical_term(attribute: Optional[str]) -> Optional[str]:
    """The canonical attribute a raw name resolves to, or None.

    The comparison registry may hold a rule for one attribute (pressure class
    is ordered). The rule is keyed on the canonical term, so the raw wording
    the model used has to be resolved first.
    """
    from app.comparison.attributes import attribute_by_term, find_attribute

    if not attribute:
        return None
    spec = attribute_by_term(attribute) or find_attribute(attribute)
    return spec.canonical_term if spec is not None else None


def _canonical_attribute(session, attribute: Optional[str]) -> Optional[str]:
    """The canonical term for an attribute name, or None when it is unknown.

    A fact is filed under the canonical term, so the cache finds it by the
    term a requirement uses, whatever wording the model answered in.
    """
    from app.services.resolution import resolve_attribute

    if not attribute:
        return None
    term, _conf, _needs_review, _reason = resolve_attribute(attribute, session)
    return term


def _convertible(requirement_unit: str, capability_unit: str) -> bool:
    from app.comparison.units import is_compatible

    return is_compatible(requirement_unit, capability_unit)


def _page_text(session: Session, candidate: Candidate) -> Optional[str]:
    """The text of the page a passage came from, for the citation check."""
    from app.repositories.schema import document as document_table

    row = session.execute(
        select(document_table.c.path).where(
            document_table.c.id == candidate.document_id
        )
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
    # The corpus keeps the files under the manufacturer's own directory, in
    # either layout: the collector's `manufacturers/` tree and the catalogue
    # the seed reads. A stored path that is only a file name is looked for in
    # both, because a document whose page cannot be found cannot have its
    # quote checked, and an unchecked quote is not evidence.
    for base in (
        data_dir / "manufacturers",
        data_dir / "catalogue",
        data_dir / "tenders",
    ):
        for found in base.glob(f"*/raw/{relative}"):
            return found
        for found in base.glob(f"*/{relative}"):
            return found
    return None


def main(argv: Optional[list[str]] = None) -> int:
    """Answer one requirement from the corpus and report what it found."""
    import argparse

    parser = argparse.ArgumentParser(description="Answer a requirement.")
    parser.add_argument("requirement", help="the requirement, as it is written")
    parser.add_argument("--manufacturer", help="the manufacturer to answer for")
    parser.add_argument("--model", help="the model to answer for")
    parser.add_argument("--value", help="the value the requirement asks for")
    parser.add_argument("--unit", help="the unit of that value")
    parser.add_argument("--operator", help="eq | ne | gt | gte | lt | lte | approx")
    parser.add_argument(
        "--store",
        action="store_true",
        help="store the fact that was found, so the next run reads the cache",
    )
    args = parser.parse_args(argv)

    from app.repositories.db import session_scope

    with session_scope() as session:
        result = match_requirement(
            session,
            args.requirement,
            requirement_value=args.value,
            requirement_unit=args.unit,
            requirement_operator=args.operator,
            manufacturer_name=args.manufacturer,
            model=args.model,
        )
        if args.store:
            store_fact(session, result)
            session.commit()

    print(f"status:        {result.status.value}")
    print(f"score:         {result.score:.2f}")
    print(f"applicability: {result.applicability.value}")
    print(f"verification:  {result.verification_outcome.value}")
    print(f"confidence:    {result.confidence.value}")
    if result.manufacturer:
        print(f"product:       {result.manufacturer} {result.model or ''}".rstrip())
    if result.page is not None:
        print(f"page:          {result.page}")
    if result.quote:
        print(f"quote:         {result.quote}")
    print(f"reason:        {result.reason}")
    if result.review_required:
        print(f"review:        {result.review_reason}")
    print(f"candidates:    {result.candidates}")
    print(f"llm calls:     {result.llm_calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
