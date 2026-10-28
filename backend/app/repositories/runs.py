"""Persistence for a matching run (FR-31, FR-32, PRD 11).

A run is one tender assessed against a set of manufacturers. Its own row
carries the state a caller polls and the snapshot of what it was scored
with; each `result` row is one requirement against one manufacturer.

The run is created before any work starts, so `POST /api/runs` can answer
with an id at once and the work happens behind it. Everything that updates
the run writes the state first: a run that died mid-way is `failed` with a
reason, never silently `queued`.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.repositories.schema import document, fact, manufacturer, result, run
from app.schemas.enums import RunStatus


class RunRepo:
    """The runs, and what each of them found."""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        tender_id: str,
        *,
        manufacturers: list[str],
        config_snapshot: dict,
        corpus_hash: str = "",
    ) -> str:
        """The run's row, before any work is done."""
        run_id = uuid.uuid4()
        self.session.execute(
            insert(run).values(
                id=run_id,
                tender_id=tender_id,
                status=RunStatus.QUEUED.value,
                config_snapshot={**config_snapshot, "manufacturers": manufacturers},
                corpus_hash=corpus_hash,
            )
        )
        return str(run_id)

    def get(self, run_id: str) -> Optional[dict]:
        row = self.session.execute(
            select(
                run.c.id,
                run.c.tender_id,
                run.c.status,
                run.c.started_at,
                run.c.config_snapshot,
                run.c.corpus_hash,
                run.c.error,
            ).where(run.c.id == run_id)
        ).first()
        if row is None:
            return None
        snapshot = row[4] or {}
        return {
            "id": str(row[0]),
            "tender_id": str(row[1]),
            "status": row[2],
            "started_at": row[3].isoformat() if row[3] else None,
            "config_snapshot": snapshot,
            "corpus_hash": row[5],
            "error": row[6],
            "manufacturers": list(snapshot.get("manufacturers") or []),
        }

    def set_status(self, run_id: str, status: RunStatus, *, error: Optional[str] = None) -> None:
        """Move the run to a state, and record why when it failed.

        The status and the reason are written together: a failed run without
        its reason is a run someone has to guess about.
        """
        values: dict = {"status": status.value}
        if error is not None:
            values["error"] = error
        self.session.execute(update(run).where(run.c.id == run_id).values(**values))

    def results(self, run_id: str) -> list[dict]:
        """Every result of a run, with the evidence that backs it.

        A result points at the fact it was read from, and the fact points at
        the document and the page. Both are joined here, because a report row
        without its quote is a claim nobody can check (PRD 13).
        """
        rows = self.session.execute(
            select(
                result.c.id,
                result.c.requirement_id,
                result.c.manufacturer_id,
                manufacturer.c.name,
                result.c.status,
                result.c.verification_outcome,
                result.c.applicability,
                result.c.score,
                result.c.capability_text,
                result.c.confidence,
                result.c.review_required,
                result.c.review_reason,
                fact.c.id,
                fact.c.page,
                fact.c.quote,
                document.c.id,
                # A document has no `filename` column: it is stored under a
                # path, or known only by the address it was fetched from. The
                # report names it from whichever of the two it has (D20).
                document.c.path,
                document.c.doc_type,
                document.c.sha256,
                document.c.url,
            )
            .select_from(result)
            .join(manufacturer, manufacturer.c.id == result.c.manufacturer_id)
            .outerjoin(fact, fact.c.id == result.c.evidence_ref)
            .outerjoin(document, document.c.id == fact.c.document_id)
            .where(result.c.run_id == run_id)
        ).all()

        found: list[dict] = []
        for row in rows:
            found.append(
                {
                    "id": str(row[0]),
                    "requirement_id": str(row[1]),
                    "manufacturer_id": str(row[2]),
                    "manufacturer": row[3],
                    "status": row[4],
                    "verification_outcome": row[5],
                    "applicability": row[6],
                    "score": row[7],
                    "capability_text": row[8],
                    "confidence": row[9],
                    "review_required": bool(row[10]),
                    "review_reason": row[11],
                    "evidence": (
                        None
                        if row[12] is None
                        else {
                            "fact_id": str(row[12]),
                            "page": row[13],
                            "quote": row[14],
                            "document_id": str(row[15]) if row[15] else None,
                            "document": _document_name(row[16], row[19]),
                            "doc_type": row[17],
                            "sha256": row[18],
                            # Where the document came from, so the report can
                            # offer the source page rather than only its name.
                            "url": row[19],
                        }
                    ),
                }
            )
        return found

    def count_results(self, run_id: str) -> int:
        return len(
            self.session.execute(select(result.c.id).where(result.c.run_id == run_id)).all()
        )


def _document_name(path: Optional[str], url: Optional[str]) -> Optional[str]:
    """The document's name, from the file it is stored as or its address.

    A document row has no name of its own: the collector stores where the file
    is and where it came from. The report shows the file name a person
    recognises, and invents nothing when neither is set.
    """
    for value in (path, url):
        if not value:
            continue
        name = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name:
            return name
    return None
