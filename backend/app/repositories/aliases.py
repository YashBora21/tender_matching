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

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.comparison.vocabulary import normalise_token
from app.repositories.schema import alias as alias_table
from app.schemas.enums import AliasSource


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


def seed_aliases(session: Session) -> int:
    """Fill the alias table from the vocabulary file.

    The table starts minimal: every synonym of a shipped attribute is a row,
    approved and confident, because the vocabulary file is the reviewed
    source of those mappings. Anything an LLM proposes later is a row too,
    but unapproved and less confident, and it waits for a human.

    Seeding is idempotent: a synonym that is already there is left alone, so
    a reviewer's decision about a row is not overwritten.
    """
    from app.comparison.vocabulary import load_vocabulary

    repo = AliasRepo(session)
    existing = {row["alias"] for row in repo.list_all()}
    added = 0
    for spec in load_vocabulary().attributes:
        for synonym in spec.synonyms:
            if synonym in existing:
                continue
            repo.add(
                spec.canonical_term,
                synonym,
                confidence=1.0,
                source=AliasSource.MANUAL,
                approved=True,
            )
            added += 1
    return added
