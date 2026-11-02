"""Storing what a match found (FR-26, FR-31, FR-32).

A fact is what a document states, with its provenance: the product, the
attribute, the value, the quote, the page and whether the claim was
verified. A result is what a requirement came to against a manufacturer.

The fact is written first, because the result points at it: every result
carries the id of the fact that backs it, so a reviewer can always get from
"full match" back to the sentence that said so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repositories.schema import attribute, fact, product, result
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    FactVerificationStatus,
    Operator,
    ReasonCode,
    RequirementStatus,
    VerificationOutcome,
    ValueType,
)


@dataclass
class StoredFact:
    """A fact, as it is written."""

    product_id: str
    document_id: str
    original_name: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    value_type: Optional[ValueType] = None
    operator: Optional[Operator] = None
    conditions: Optional[str] = None
    page: Optional[int] = None
    quote: Optional[str] = None
    verification_status: FactVerificationStatus = FactVerificationStatus.NEEDS_REVIEW
    extractor_version: Optional[str] = None
    attribute_id: Optional[str] = None


def _attribute_id(session: Session, canonical_term: Optional[str]) -> Optional[str]:
    if not canonical_term:
        return None
    row = session.execute(
        select(attribute.c.id).where(attribute.c.canonical_term == canonical_term)
    ).first()
    return str(row[0]) if row else None


def product_id_for(
    session: Session, manufacturer_name: str, model: str
) -> Optional[str]:
    """The product a fact is about.

    A fact says "this product has this capability", so the product has to
    exist. A fact about a product that is not in the catalogue is still
    worth keeping, but it cannot be written without one.
    """
    from app.repositories.schema import manufacturer

    row = session.execute(
        select(product.c.id)
        .join(manufacturer, manufacturer.c.id == product.c.manufacturer_id)
        .where(
            manufacturer.c.name == manufacturer_name,
            product.c.model == model,
        )
    ).first()
    return str(row[0]) if row else None


def cached_fact(
    session: Session, product_id: str, canonical_term: str
) -> Optional[dict]:
    """The verified fact already held for this product and attribute (FR-21).

    A fact that was verified once is not read again: the extraction is the
    expensive part, and the passage it came from has not changed. Only a
    verified fact is a cache hit - a fact that is waiting for a review is not
    an answer, and it is read again.
    """
    row = session.execute(
        select(
            fact.c.id,
            fact.c.value,
            fact.c.unit,
            fact.c.value_type,
            fact.c.document_id,
            fact.c.page,
            fact.c.quote,
            attribute.c.canonical_term,
            product.c.is_family_level,
        )
        .select_from(fact)
        .join(attribute, attribute.c.id == fact.c.attribute_id)
        .join(product, product.c.id == fact.c.product_id)
        .where(
            fact.c.product_id == product_id,
            attribute.c.canonical_term == canonical_term,
            fact.c.verification_status == FactVerificationStatus.VERIFIED.value,
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "value": row[1],
        "unit": row[2],
        "value_type": row[3],
        "document_id": str(row[4]),
        "page": row[5],
        "quote": row[6],
        "attribute": row[7],
        "is_family_level": bool(row[8]),
    }


def store_fact(session: Session, stored: StoredFact) -> str:
    """Write a fact and return its id."""
    fact_id = uuid.uuid4()
    session.execute(
        fact.insert().values(
            id=fact_id,
            product_id=stored.product_id,
            attribute_id=stored.attribute_id,
            original_name=stored.original_name,
            value=stored.value,
            unit=stored.unit,
            value_type=stored.value_type.value if stored.value_type else None,
            operator=stored.operator.value if stored.operator else None,
            conditions=stored.conditions,
            document_id=stored.document_id,
            page=stored.page,
            quote=stored.quote,
            verification_status=stored.verification_status.value,
            extractor_version=stored.extractor_version,
        )
    )
    return str(fact_id)


def store_result(
    session: Session,
    *,
    run_id: str,
    requirement_id: str,
    manufacturer_id: str,
    status: RequirementStatus,
    score: float,
    verification_outcome: VerificationOutcome,
    applicability: Applicability,
    capability_text: Optional[str] = None,
    evidence_ref: Optional[str] = None,
    confidence: ConfidenceLevel = ConfidenceLevel.LOW,
    review_required: bool = False,
    review_reason: Optional[str] = None,
    reason_code: Optional[ReasonCode] = None,
    mandatory: bool = True,
) -> str:
    """Write a result and return its id."""
    result_id = uuid.uuid4()
    session.execute(
        result.insert().values(
            id=result_id,
            run_id=run_id,
            requirement_id=requirement_id,
            manufacturer_id=manufacturer_id,
            status=status.value,
            verification_outcome=verification_outcome.value,
            applicability=applicability.value,
            score=score,
            capability_text=capability_text,
            evidence_ref=evidence_ref,
            confidence=confidence.value,
            review_required=review_required,
            review_reason=review_reason,
            reason_code=reason_code.value if reason_code is not None else None,
        )
    )
    return str(result_id)
