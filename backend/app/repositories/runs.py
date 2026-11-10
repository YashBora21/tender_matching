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


# What an attempt may spend, as opposed to what it answers. Two runs that
# differ only here produce the same answers, so one can continue the other.
_SPEND_ONLY = ("llm_call_budget", "llm_token_budget")


def _answers_alike(stored: dict, current: dict) -> bool:
    """Do these two snapshots describe the same answering configuration?"""
    def comparable(snapshot: dict) -> dict:
        return {key: value for key, value in (snapshot or {}).items() if key not in _SPEND_ONLY}

    return comparable(stored) == comparable(current)


def tender_inputs(requirements: list[dict], items: list[dict], *, tender_id: str) -> dict:
    """Immutable JSON input binding shared by execution and historical reports."""
    from copy import deepcopy
    return deepcopy({"requirements": requirements, "items": items,
                     "tender_id": str(tender_id), "complete": True,
                     "origin": "run_creation"})


def inputs_are_current(snapshot: dict, requirements: list[dict]) -> bool:
    stored = (snapshot or {}).get("tender_inputs")
    return bool(stored and stored.get("complete") and
                {row["id"] for row in stored["requirements"]} ==
                {row["id"] for row in requirements})


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

    def resumable(
        self,
        tender_id: str,
        manufacturers: list[str],
        snapshot: Optional[dict] = None,
    ) -> Optional[str]:
        """The run this attempt continues, when a limit stopped one (D-110).

        A run that stopped at the provider's limit kept every answer it had
        already found, so starting a second run for the same tender and the
        same manufacturers pays for them twice. The newest such run is
        returned; an answer that was never given is the only thing a resumed
        run still owes.

        `snapshot` is what the new attempt would be scored with. A run read by
        another model, or with a different candidate cap, is not continued:
        one report would then hold answers from two configurations and state a
        single one. What the attempt may spend is not part of that: a budget is
        a limit on a run, not an input to its answer.
        """
        rows = self.session.execute(
            select(run.c.id, run.c.config_snapshot)
            .where(run.c.tender_id == tender_id)
            .where(run.c.status == RunStatus.RATE_LIMITED.value)
            .order_by(run.c.started_at.desc())
        ).all()
        wanted = sorted(manufacturers)
        for row in rows:
            stored = row[1] or {}
            if sorted(stored.get("manufacturers") or []) != wanted:
                continue
            if snapshot is not None and not _answers_alike(stored, snapshot):
                continue
            return str(row[0])
        return None

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
                run.c.metrics,
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
            "metrics": row[7] or {},
            "manufacturers": list(snapshot.get("manufacturers") or []),
        }

    def set_status(self, run_id: str, status: RunStatus, *, error: Optional[str] = None) -> None:
        """Move the run to a state, and record why when it failed.

        The status and the reason are written together, including when there is
        no reason: a run that resumes keeps the message of the attempt that
        stopped, and a resumed run that reports a rate limit it is no longer at
        is a run someone has to disbelieve (D-120).
        """
        self.session.execute(
            update(run)
            .where(run.c.id == run_id)
            .values(status=status.value, error=error)
        )

    def claim(self, run_id: str) -> bool:
        """Take the run for this attempt, or say another pass already holds it.

        The row's state is the lock: the update from `queued` or `rate_limited`
        lands once, because the second attempt finds a row that no longer
        matches its condition. `POST /api/runs` hands the same resumable run to
        two callers (D-130) and schedules the work for both, so without this
        the pairs one pass is still answering get paid for twice and stored
        twice - `result` has no key on a requirement x manufacturer pair.

        A run left `retrieving` or `scoring` by a pass that died is not picked
        up again here: a new attempt starts a new run, which is what happens
        today, because only a `rate_limited` run is ever continued.
        """
        claimed = self.session.execute(
            update(run)
            .where(run.c.id == run_id)
            .where(
                run.c.status.in_(
                    [RunStatus.QUEUED.value, RunStatus.RATE_LIMITED.value]
                )
            )
            .values(status=RunStatus.RETRIEVING.value, error=None)
        ).rowcount
        self.session.commit()
        return bool(claimed)

    def set_metrics(self, run_id: str, metrics: dict) -> None:
        """What the run cost, written with the state it ended in (D-131)."""
        self.session.execute(
            update(run).where(run.c.id == run_id).values(metrics=metrics)
        )

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
                result.c.reason_code,
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
                fact.c.source_context,
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
                    # Why no verdict was reached, when the limit is ours
                    # rather than a finding about the product (D-101).
                    "reason_code": row[12],
                    "evidence": (
                        None
                        if row[13] is None
                        else {
                            "fact_id": str(row[13]),
                            "page": row[14],
                            "quote": row[15],
                            "document_id": str(row[16]) if row[16] else None,
                            "document": _document_name(row[17], row[20]),
                            "doc_type": row[18],
                            "sha256": row[19],
                            # Where the document came from, so the report can
                            # offer the source page rather than only its name.
                            "url": row[20],
                            "source_context": row[21],
                        }
                    ),
                }
            )
        return found

    def answered(self, run_id: str) -> set[tuple[str, str]]:
        """The requirement x manufacturer pairs this run already stores.

        A run stopped by a limit keeps the answers it found, and resuming it
        has to pay only for the pairs that are missing: a key with a hard
        quota cannot afford to read the whole tender again to add the last
        requirement (D-110).
        """
        rows = self.session.execute(
            select(result.c.requirement_id, result.c.manufacturer_id).where(
                result.c.run_id == run_id
            )
        ).all()
        return {(str(row[0]), str(row[1])) for row in rows}

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
