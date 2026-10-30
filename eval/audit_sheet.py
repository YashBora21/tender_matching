"""A random audit sheet of published results (PRD 15, step 5).

The sheet is what a reviewer checks by hand: one row per requirement x
manufacturer, carrying the status the system published, the quote it published
with it, and the document and page that quote is on. Two columns are left
empty on purpose:

- `audit_ok` - the reviewer's yes/no on the row;
- `correct_status` - the status the reviewer says is right, when it is not.

Nothing in this module grades a result. It selects the rows, writes the sheet
and records how it selected them, so a second person can reproduce the sample
and fill the two columns without the export having decided anything.

Two rules keep the sample honest:

- **The rows are the system's own results.** They come from
  `match_requirement`, the same function a run calls, and every row keeps the
  status, verification outcome, applicability, review flag, document, page and
  quote it came back with. A pair that could not be answered is a row, not a
  gap.
- **The sample is random but reproducible.** Rows are ordered by
  (item, requirement, manufacturer) and then sampled with `random.Random(seed)`,
  so the same seed over the same results is the same sheet, and a different
  seed is a different one.

A rate limit is not caught: `collect` stops, keeps the rows it already has in
the checkpoint, and says how many are left. A later run resumes from the
checkpoint rather than asking the provider for the same pairs again.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The reviewer's columns are last and empty: the export does not grade itself.
FIELDS = [
    "pair",
    "tender_sha256",
    "requirement_id",
    "item_id",
    "item_title",
    "requirement",
    "manufacturer",
    "model",
    "status",
    "verification_outcome",
    "applicability",
    "confidence",
    "review_required",
    "review_reason",
    "reason_code",
    "score",
    "document",
    "page",
    "quote",
    "candidate_cap",
    "audit_ok",
    "correct_status",
    "notes",
]

# The columns the reviewer fills. They are written as empty strings, never as
# a guess.
REVIEWER_COLUMNS = ("audit_ok", "correct_status", "notes")


class AuditSheetError(Exception):
    """Input the exporter refuses, with what to fix."""


class RateLimited(Exception):
    """The provider stopped the export; the checkpoint holds what was found."""

    def __init__(self, completed: int, expected: int, detail: str) -> None:
        super().__init__(detail)
        self.completed = completed
        self.expected = expected
        self.detail = detail


def _key(requirement_id: str, manufacturer: str) -> str:
    return f"{requirement_id}|{manufacturer}"


def _sort_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("item_id") or ""),
        str(row.get("requirement_id") or ""),
        str(row.get("manufacturer") or ""),
    )


def sample(rows: list[dict], n: int, seed: int) -> list[dict]:
    """A fixed-seed sample of the rows, in a stable order.

    The rows are ordered before sampling, so the result depends on the seed
    and the rows, never on the order the database returned them in. The picked
    rows are ordered again for the sheet, so a reader sees them by item.
    """
    if n < 1:
        raise AuditSheetError("the sample size must be at least 1")
    ordered = sorted(rows, key=_sort_key)
    picked = random.Random(seed).sample(ordered, min(n, len(ordered)))
    return sorted(picked, key=_sort_key)


def _row(
    requirement: dict,
    manufacturer: str,
    match,
    *,
    document: Optional[dict],
    candidate_cap: int,
) -> dict:
    """One result, as the reviewer sees it. Nothing is added to the result."""
    reason_code = getattr(match, "reason_code", None)
    if reason_code is not None and hasattr(reason_code, "value"):
        reason_code = reason_code.value
    status = getattr(match.status, "value", match.status)
    outcome = getattr(match.verification_outcome, "value", match.verification_outcome)
    applicability = getattr(match.applicability, "value", match.applicability)
    confidence = getattr(match.confidence, "value", match.confidence)
    return {
        "tender_sha256": requirement.get("tender_sha256") or "",
        "requirement_id": requirement["requirement_id"],
        "item_id": requirement.get("item_id") or "",
        "item_title": requirement.get("item_title") or "",
        "requirement": requirement.get("requirement") or "",
        "manufacturer": manufacturer,
        "model": match.model or "",
        "status": status,
        "verification_outcome": outcome,
        "applicability": applicability,
        "confidence": confidence,
        "review_required": bool(match.review_required),
        "review_reason": match.review_reason or "",
        "reason_code": reason_code or "",
        "score": round(float(match.score or 0.0), 4),
        "document": (document or {}).get("document") or "",
        "page": match.page if match.page is not None else "",
        "quote": match.quote or "",
        "candidate_cap": candidate_cap,
        "audit_ok": "",
        "correct_status": "",
        "notes": "",
    }


def collect(
    requirements: list[dict],
    manufacturers: list[str],
    *,
    match: Callable[[dict, str], object],
    document_for: Callable[[object], Optional[dict]],
    candidate_cap: int,
    checkpoint: Optional[Path] = None,
) -> list[dict]:
    """Ask the matcher for every requirement x manufacturer pair.

    The match and document lookups are injected so the flow can be driven
    without a database or a model in a test. A pair already in the checkpoint
    is not asked again: the provider's limit stopped an earlier attempt, and
    the rows it did answer are still good.
    """
    expected = len(requirements) * len(manufacturers)
    done: dict[str, dict] = _load_checkpoint(checkpoint)
    keys: list[str] = []

    for requirement in requirements:
        for manufacturer in manufacturers:
            key = _key(requirement["requirement_id"], manufacturer)
            keys.append(key)
            if key in done:
                continue
            try:
                outcome = match(requirement, manufacturer)
            except Exception as exc:  # noqa: BLE001 - only a limit is resumable
                if _is_rate_limit(exc):
                    _save_checkpoint(checkpoint, done)
                    raise RateLimited(
                        completed=len(done),
                        expected=expected,
                        detail=str(exc),
                    ) from exc
                raise
            done[key] = _row(
                requirement,
                manufacturer,
                outcome,
                document=document_for(outcome),
                candidate_cap=candidate_cap,
            )
            _save_checkpoint(checkpoint, done)

    return [done[key] for key in keys if key in done]


def _is_rate_limit(exc: BaseException) -> bool:
    from app.services.llm import LLMRateLimited

    return isinstance(exc, LLMRateLimited)


def _load_checkpoint(path: Optional[Path]) -> dict[str, dict]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditSheetError(f"could not read the checkpoint {path}: {exc}") from exc
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        raise AuditSheetError(f"{path} is not a checkpoint this exporter wrote")
    return rows


def _save_checkpoint(path: Optional[Path], rows: dict[str, dict]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rows": rows}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def write_sheet(rows: list[dict], path: Path) -> None:
    """Write the CSV, with the reviewer's columns empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            values = {key: row.get(key, "") for key in FIELDS}
            values["pair"] = f"P{index:02d}"
            for column in REVIEWER_COLUMNS:
                values[column] = ""
            writer.writerow(values)


# --- database and model wiring ---------------------------------------------


def _tender(session, sha256: str) -> dict:
    from sqlalchemy import select

    from app.repositories.schema import tender

    row = session.execute(
        select(tender.c.id, tender.c.filename).where(tender.c.sha256 == sha256)
    ).first()
    if row is None:
        raise AuditSheetError(f"no tender has the sha256 {sha256}")
    return {"id": str(row[0]), "filename": row[1]}


def _requirements(
    session, tender_id: str, requirement_ids: Iterable[str], sha256: str
) -> list[dict]:
    from sqlalchemy import and_, select

    from app.repositories.schema import requirement, tender_item

    wanted = [uuid.UUID(str(value)) for value in requirement_ids]
    rows = session.execute(
        select(
            requirement.c.id,
            requirement.c.item_id,
            requirement.c.text,
            tender_item.c.name,
            tender_item.c.category,
        )
        .join(
            tender_item,
            and_(
                tender_item.c.tender_id == requirement.c.tender_id,
                tender_item.c.item_id == requirement.c.item_id,
            ),
        )
        .where(requirement.c.tender_id == uuid.UUID(tender_id))
        .where(requirement.c.id.in_(wanted))
    ).all()

    found = {
        str(row[0]): {
            "requirement_id": str(row[0]),
            "item_id": row[1],
            "requirement": row[2],
            "item_title": row[3],
            "category": row[4],
            "tender_sha256": sha256,
        }
        for row in rows
    }
    missing = [str(value) for value in requirement_ids if str(value) not in found]
    if missing:
        raise AuditSheetError(
            "the tender does not hold these requirement ids: " + ", ".join(missing)
        )
    return [found[str(value)] for value in requirement_ids]


def _document(session, document_id) -> Optional[dict]:
    from sqlalchemy import select

    from app.repositories.schema import document

    if document_id is None:
        return None
    row = session.execute(
        select(document.c.path, document.c.url, document.c.sha256).where(
            document.c.id == uuid.UUID(str(document_id))
        )
    ).first()
    if row is None:
        return None
    return {"document": _document_name(row[0], row[1]), "sha256": row[2]}


def _document_name(path: Optional[str], url: Optional[str]) -> Optional[str]:
    """The document's name, from the file it is stored as or its address."""
    for value in (path, url):
        if not value:
            continue
        name = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if name:
            return name
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_sheet.py",
        description="Export a fixed-seed audit sheet of published results.",
    )
    parser.add_argument("--tender-sha", required=True)
    parser.add_argument(
        "--requirement-id",
        action="append",
        required=True,
        help="a requirement id; repeat it for each requirement",
    )
    parser.add_argument("--n", type=int, default=15)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="candidate cap per pair; defaults to MATCH_MAX_CANDIDATES",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--provenance", type=Path, default=None)
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.repositories.corpus import list_manufacturers
    from app.repositories.db import session_scope
    from app.services.matching import match_requirement

    settings = get_settings()
    cap = args.limit or settings.match_max_candidates

    try:
        with session_scope() as session:
            tender = _tender(session, args.tender_sha)
            requirements = _requirements(
                session, tender["id"], args.requirement_id, args.tender_sha
            )
            manufacturers = [
                row["name"] for row in list_manufacturers(session) if row["documents"]
            ]
            rows = collect(
                requirements,
                manufacturers,
                match=lambda requirement, name: match_requirement(
                    session,
                    requirement["requirement"],
                    item_title=requirement["item_title"],
                    category=requirement["category"],
                    manufacturer_name=name,
                    limit=cap,
                ),
                document_for=lambda outcome: _document(session, outcome.document_id),
                candidate_cap=cap,
                checkpoint=args.checkpoint,
            )
    except RateLimited as exc:
        print(
            f"rate limited after {exc.completed} of {exc.expected} pair(s); "
            f"rerun with the same --checkpoint to resume: {exc.detail}",
            file=sys.stderr,
        )
        return 3
    except AuditSheetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    picked = sample(rows, args.n, args.seed)
    write_sheet(picked, args.out)

    if args.provenance:
        args.provenance.parent.mkdir(parents=True, exist_ok=True)
        args.provenance.write_text(
            json.dumps(
                {
                    "tender_sha256": args.tender_sha,
                    "requirement_ids": args.requirement_id,
                    "manufacturers": manufacturers,
                    "pairs_asked": len(rows),
                    "sample_size": len(picked),
                    "seed": args.seed,
                    "candidate_cap": cap,
                    "model": settings.llm_model,
                    "config": {
                        "embedding_backend": settings.embedding_backend,
                        "embedding_model": settings.embedding_model,
                        "match_max_candidates": settings.match_max_candidates,
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(
        f"{len(picked)} row(s) sampled from {len(rows)} published result(s) "
        f"(seed {args.seed}) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
