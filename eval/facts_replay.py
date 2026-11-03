"""What the stored facts alone say about the labelled gold pairs (FR-21, D-133).

The baseline run read a passage for every pair with a model and published
silence for all six. Checking the fact cache before retrieval is the speed-up
that was meant to change that: a verified fact is an answer, and comparing it
with a requirement is deterministic. This script asks the gold pairs that one
question and no other. It makes no model call and writes no row.

Two substitutions, both stated so the numbers are read correctly:

- The requirement's attribute, value and unit are read from the stored clause
  with the deterministic resolver. The stored rows were written before that
  resolver existed and hold those columns as NULL, and a comparison against a
  NULL value is the baseline's own dead end, not a finding.
- The run finds the products to read by retrieval and then asks the cache for
  the product each passage names. No retrieval runs here, so the fact is the
  first product of the manufacturer that holds the attribute, which is not
  necessarily the product the BOQ item is about. The model that answered is
  printed with the row so a reader can see the scope for themselves.

The labelled set is six pairs, so this is a spot check of the speed-up and not
a benchmark. Nothing here is tuned against those six.

Usage (repo root, database up):

    venv/Scripts/python.exe eval/facts_replay.py \
        --gold eval/baseline/gold-items-10-11.jsonl \
        --out eval/replay/facts-only.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
EVAL_DIR = Path(__file__).resolve().parent
for directory in (str(BACKEND), str(EVAL_DIR)):
    if directory not in sys.path:
        sys.path.insert(0, directory)

from audit_sheet import _document, _requirements, _tender  # noqa: E402
from harness import (  # noqa: E402
    CLAIMS,
    _utf8_console,
    load_gold,
    render_markdown,
    score,
)


class ReplayError(Exception):
    """A pair this script cannot answer without a model call."""


class _NoModel:
    """The client the matcher is handed: a call here is a bug, so it raises."""

    def complete_json(self, *args, **kwargs):
        raise ReplayError(
            "the replay reached the extractor; a pair answered from stored "
            "facts must never read a passage"
        )

    chat = complete_json


def _fact_holder(session, manufacturer_name: str, canonical_term: str) -> Optional[str]:
    """The first of this manufacturer's products that holds a verified fact.

    Products are ordered by model name because a fact row has no timestamp to
    choose by, and the same reason `findings.cached_fact` gives: an arbitrary
    order that is at least stable across two replays.
    """
    from sqlalchemy import select

    from app.repositories.schema import attribute, fact, manufacturer, product
    from app.schemas.enums import FactVerificationStatus

    row = session.execute(
        select(product.c.model)
        .select_from(fact)
        .join(product, product.c.id == fact.c.product_id)
        .join(manufacturer, manufacturer.c.id == product.c.manufacturer_id)
        .join(attribute, attribute.c.id == fact.c.attribute_id)
        .where(
            manufacturer.c.name == manufacturer_name,
            attribute.c.canonical_term == canonical_term,
            fact.c.verification_status == FactVerificationStatus.VERIFIED.value,
        )
        .order_by(product.c.model)
        .limit(1)
    ).first()
    return str(row[0]) if row else None


def _silence(requirement_text: str, manufacturer: str, reason: str):
    """A pair no stored fact answers, in the shape the matcher gives silence.

    shortcut: this copies the three fields `match_requirement` sets when no
    passage answered (status stays the dataclass default, both review fields
    carry the reason); upgrade by asking the matcher itself if it grows a
    fourth field there.
    """
    from app.services.match.match import MatchResult

    result = MatchResult(requirement_text=requirement_text, manufacturer=manufacturer)
    result.reason = reason
    result.review_required = True
    result.review_reason = reason
    return result


def _ask(session, requirement: dict, manufacturer: str):
    """What the stored facts say about one pair, and which product said it.

    The cache is asked through `match_requirement`, so the comparison, the
    family-level cap and the review flag are the ones a run would reach. A
    product that holds a verified fact for the clause's attribute is a hit by
    construction, so the matcher returns before it retrieves anything.
    """
    from app.services.match.match import match_requirement
    from app.services.requirement.normalize import normalize

    text = requirement["requirement"]
    read = normalize(text)
    if read.attribute is None:
        return (
            _silence(text, manufacturer, "the clause names no attribute the vocabulary knows"),
            None,
        )
    model = _fact_holder(session, manufacturer, read.attribute)
    if model is None:
        return (
            _silence(
                text,
                manufacturer,
                f"no verified fact states {read.attribute}; answering it would "
                "read a passage, and this replay makes no model call",
            ),
            None,
        )

    match = match_requirement(
        session,
        text,
        manufacturer_name=manufacturer,
        model=model,
        requirement_value=read.value,
        requirement_unit=read.unit,
        requirement_operator=read.operator.value if read.operator else None,
        client=_NoModel(),
    )
    if match.llm_calls:
        raise ReplayError(
            f"{requirement['requirement_id']} x {manufacturer} cost a model call"
        )
    return match, model


def _row(manufacturer: str, match, document: Optional[dict]) -> dict:
    """One replayed pair as a report row, with no database ids in it.

    `evidence` is the fact's own quote, page and document hash, so the
    harness can re-check the claim against the PDF on disk (FR-23). A row of
    silence carries no quote to check and so no evidence block at all.
    """
    status = getattr(match.status, "value", match.status)
    row = {
        "manufacturer": manufacturer,
        "status": status,
        "score": round(float(match.score or 0.0), 4),
        "verification_outcome": getattr(
            match.verification_outcome, "value", match.verification_outcome
        ),
        "applicability": getattr(match.applicability, "value", match.applicability),
        "capability_text": match.capability_text,
        "confidence": getattr(match.confidence, "value", match.confidence),
        "review_required": bool(match.review_required),
        "review_reason": match.review_reason,
        "reason": match.reason,
        "reason_code": getattr(match.reason_code, "value", match.reason_code),
        "model": match.model,
    }
    if status in CLAIMS:
        found = document or {}
        row["evidence"] = {
            "page": match.page,
            "quote": match.quote,
            "document": found.get("document"),
            "sha256": found.get("sha256"),
        }
    return row


def replay(
    labels, requirements: dict[str, dict], *, session
) -> tuple[dict, list[dict]]:
    """The report-shaped payload for every label, and one row per pair."""
    by_requirement: dict[str, list[dict]] = defaultdict(list)
    pairs: list[dict] = []

    for label in labels:
        requirement = requirements[label.requirement_id]
        match, model = _ask(session, requirement, label.manufacturer)
        row = _row(
            label.manufacturer,
            match,
            _document(session, match.document_id) if match.document_id else None,
        )
        by_requirement[label.requirement_id].append(row)
        pairs.append(
            {
                "requirement_id": label.requirement_id,
                "manufacturer": label.manufacturer,
                "gold": label.status,
                "replay": row["status"],
                "review_required": row["review_required"],
                "answered_from": model or "no stored fact",
                "reason": row["reason"] or row["review_reason"] or "",
            }
        )

    report = {
        "tender": {"sha256": labels[0].tender_sha256},
        "requirements": [
            {
                "requirement_id": requirement_id,
                "text": requirements[requirement_id]["requirement"],
                "results": by_requirement[requirement_id],
            }
            for requirement_id in by_requirement
        ],
    }
    return report, pairs


def main(argv: Optional[list[str]] = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(
        prog="facts_replay.py",
        description="Answer gold pairs from stored facts, with no model call.",
    )
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument(
        "--out", type=Path, default=None, help="write the scored summary here"
    )
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.repositories.db import session_scope

    labels = load_gold(args.gold)
    shas = {label.tender_sha256 for label in labels}
    if len(shas) > 1:
        print(f"error: {args.gold} labels more than one tender", file=sys.stderr)
        return 1

    try:
        with session_scope() as session:
            tender = _tender(session, labels[0].tender_sha256)
            wanted = {label.requirement_id for label in labels}
            requirements = {
                row["requirement_id"]: row
                for row in _requirements(
                    session, tender["id"], wanted, labels[0].tender_sha256
                )
            }
            report, pairs = replay(labels, requirements, session=session)
            summary = score(report, labels, Path(get_settings().data_dir))
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("| requirement | manufacturer | gold | replay | flagged | answered from | why |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for pair in pairs:
        print(
            f"| {pair['requirement_id'][:8]} | {pair['manufacturer']} | "
            f"{pair['gold']} | {pair['replay']} | "
            f"{'yes' if pair['review_required'] else 'no'} | "
            f"{pair['answered_from']} | {pair['reason']} |"
        )
    print()
    print(render_markdown(summary), end="")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"\nscored summary -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
