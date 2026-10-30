"""The alias table: names mapped to canonical terms (FR-20).

The alias table is the editable mapping from a name used in a document to the
canonical term the comparison engine understands. "SS316", "AISI 316" and
"CF8M" all name the same thing; the table says so, and a reviewer can change
it without a code edit.

The table starts minimal: it is seeded from the shipped vocabulary, and every
row carries a confidence. A row below the confidence threshold is not used to
resolve a name - the requirement goes to review instead (PRD 9).

An alias whose canonical term is not in the vocabulary is not a mapping to a
known attribute, so it is kept but not used: it says "we saw this name", not
"we know what it means".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.comparison.vocabulary import normalise_token
from app.core.logging import get_logger
from app.repositories.schema import alias as alias_table
from app.schemas.enums import AliasSource

logger = get_logger(__name__)


@dataclass
class AliasResolution:
    """What an alias lookup found."""

    canonical_term: Optional[str] = None
    confidence: float = 0.0
    # True when the name is a canonical term itself.
    is_canonical: bool = False
    # True when the alias is known but not approved, or its confidence is too
    # low to resolve on. The caller sends the requirement to review.
    needs_review: bool = False
    reason: Optional[str] = None


class AliasRepo:
    """Read and write the alias table."""

    def __init__(self, session: Session):
        self.session = session

    def resolve(self, name: str, *, threshold: float) -> AliasResolution:
        """Resolve a name to its canonical term.

        A name that is already a canonical term resolves to itself. A name in
        the alias table resolves to what it points at, if the row is approved
        and confident enough. Anything else does not resolve.
        """
        from app.comparison.attributes import attribute_by_term

        if not name:
            return AliasResolution(needs_review=True, reason="no attribute name")

        token = normalise_token(name)
        if not token:
            return AliasResolution(needs_review=True, reason="no attribute name")

        spec = attribute_by_term(name) or attribute_by_term(token)
        if spec is not None:
            return AliasResolution(
                canonical_term=spec.canonical_term,
                confidence=1.0,
                is_canonical=True,
            )

        row = self._lookup(name) or self._lookup(token)
        if row is None:
            return AliasResolution(
                needs_review=True,
                reason=f"'{name}' is not a known attribute",
            )

        canonical_term, confidence, approved = row
        confidence = float(confidence or 0.0)
        if not approved:
            return AliasResolution(
                canonical_term=canonical_term,
                confidence=confidence,
                needs_review=True,
                reason=f"the alias '{name}' has not been approved",
            )
        if confidence < threshold:
            return AliasResolution(
                canonical_term=canonical_term,
                confidence=confidence,
                needs_review=True,
                reason=(
                    f"the alias '{name}' has confidence {confidence:.2f}, "
                    f"below the threshold {threshold:.2f}"
                ),
            )
        return AliasResolution(
            canonical_term=canonical_term,
            confidence=confidence,
        )

    def _lookup(self, name: str) -> Optional[tuple]:
        """The alias row for a name, if there is one."""
        return self.session.execute(
            select(
                alias_table.c.canonical_term,
                alias_table.c.confidence,
                alias_table.c.approved,
            ).where(alias_table.c.alias == name)
        ).first()

    def add(
        self,
        canonical_term: str,
        alias: str,
        *,
        confidence: float,
        source: AliasSource = AliasSource.MANUAL,
        approved: bool = False,
    ) -> None:
        """Add or update an alias."""
        existing = self.session.execute(
            select(alias_table.c.id).where(alias_table.c.alias == alias)
        ).first()
        if existing:
            self.session.execute(
                text(
                    "UPDATE alias SET canonical_term = :term, confidence = :confidence, "
                    "source = :source, approved = :approved WHERE id = :id"
                ).bindparams(
                    term=canonical_term,
                    confidence=confidence,
                    source=source.value,
                    approved=approved,
                    id=existing[0],
                )
            )
            return
        self.session.execute(
            alias_table.insert().values(
                canonical_term=canonical_term,
                alias=alias,
                confidence=confidence,
                source=source.value,
                approved=approved,
            )
        )

    def set_approval(self, alias: str, approved: bool) -> None:
        """Mark an alias approved or not.

        The confidence follows the approval: a row that may resolve a name is
        a row that is sure of it, and a row that may not is a row that says
        nothing.
        """
        self.session.execute(
            update(alias_table)
            .where(alias_table.c.alias == alias)
            .values(
                approved=approved,
                confidence=1.0 if approved else 0.0,
            )
        )

    def list_all(self) -> list[dict]:
        """Every alias, for a reviewer to look at."""
        rows = self.session.execute(
            select(
                alias_table.c.canonical_term,
                alias_table.c.alias,
                alias_table.c.confidence,
                alias_table.c.approved,
                alias_table.c.source,
            )
        ).all()
        return [
            {
                "canonical_term": row[0],
                "alias": row[1],
                "confidence": row[2],
                "approved": row[3],
                "source": row[4],
            }
            for row in rows
        ]

    def approved_aliases(self, *, threshold: float) -> dict[str, list[str]]:
        """The names each canonical term is known by, for query expansion.

        Only an approved alias at or above the confidence threshold is
        returned: an unapproved name is a guess, and a query must not be
        widened with guesses (FR-20). The rows are grouped by canonical term,
        because the names of one attribute are what make the same thing
        findable in another document's wording (D-102).
        """
        rows = self.session.execute(
            select(alias_table.c.canonical_term, alias_table.c.alias)
            .where(
                alias_table.c.approved.is_(True),
                alias_table.c.confidence >= threshold,
            )
            .order_by(alias_table.c.canonical_term, alias_table.c.alias)
        ).all()
        grouped: dict[str, list[str]] = {}
        for canonical_term, alias in rows:
            grouped.setdefault(canonical_term, []).append(alias)
        return grouped


def seed_aliases(session: Session) -> int:
    """Fill the alias table from the vocabulary file.

    A row is approved only when the file says so. A synonym the file does
    not approve is written unapproved: it does not resolve a name, and the
    requirement that named it goes to review instead (FR-20, PRD 9).

    Seeding is idempotent. A synonym that is already there keeps its
    approval, because a reviewer's decision about a row is not the
    seeder's to overwrite. A row that came from the vocabulary file is
    brought back in line with the file, because the file is where those
    decisions are recorded.
    """
    from app.comparison.vocabulary import load_vocabulary

    repo = AliasRepo(session)
    existing = {row["alias"]: row for row in repo.list_all()}
    added = 0
    updated = 0
    for spec in load_vocabulary().attributes:
        for term in spec.synonyms:
            approved = term in spec.approved_synonyms
            row = existing.get(term)
            if row is None:
                repo.add(
                    spec.canonical_term,
                    term,
                    confidence=1.0 if approved else 0.0,
                    source=AliasSource.MANUAL,
                    approved=approved,
                )
                added += 1
                continue
            if (
                row["source"] == AliasSource.MANUAL.value
                and row["canonical_term"] == spec.canonical_term
                and bool(row["approved"]) != approved
            ):
                # The row came from the vocabulary file, so the file decides.
                repo.set_approval(term, approved)
                updated += 1
    if updated:
        logger.info("%d alias rows brought in line with the vocabulary file", updated)
    return added


def seed_attributes(session: Session) -> int:
    """Fill the attribute table from the vocabulary file.

    The table is what a fact points at, so it has to hold the canonical
    terms. It was empty schema until now: the terms lived only in the file.
    """
    from app.comparison.vocabulary import load_vocabulary
    from app.repositories.schema import attribute as attribute_table

    existing = {
        row[0]
        for row in session.execute(
            select(attribute_table.c.canonical_term)
        ).all()
    }
    added = 0
    for spec in load_vocabulary().attributes:
        if spec.canonical_term in existing:
            continue
        session.execute(
            attribute_table.insert().values(
                id=uuid.uuid4(),
                canonical_term=spec.canonical_term,
                value_type=spec.value_type.value,
                unit_default=spec.default_unit,
            )
        )
        added += 1
    return added
