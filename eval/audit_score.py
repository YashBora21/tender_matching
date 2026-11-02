"""Grade a filled audit sheet: what a person checked of what the system said.

`audit_sheet.py` exports a random sample of published results with the
reviewer's three columns - `audit_ok`, `correct_status`, `notes` - left empty.
This reads that sheet back once they are filled and reports what the sample
says about the results it was drawn from.

Four rules keep the number honest:

- **The sheet is input, never output.** Nothing here writes to it. The reviewer's
  file is the record of what a human decided; a tool that edited it would corrupt
  the thing it measures.
- **Rows are joined on the `pair` id**, the id the exporter wrote, so a reviewer
  who sorted or filtered the sheet in Excel is still graded against the row they
  read. A missing or repeated id is refused, never guessed.
- **Every row must be filled.** A blank `audit_ok` is a row nobody looked at, and
  a rate over the rows that happen to be filled reads as a rate over the sample.
  The sheet is refused with the ids still blank.
- **The sample is precision, not recall.** It holds only pairs the system already
  answered, so it measures how good the output was - never what was missed. A
  requirement the run never raised is not in this file and cannot be.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
for _path in (str(REPO_ROOT / "eval"), str(BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from harness import CLAIMS, STATUSES, citation, corpus_index  # noqa: E402

# The columns this grader needs. The rest of the sheet is there for the reviewer.
REQUIRED = (
    "pair",
    "requirement_id",
    "item_id",
    "manufacturer",
    "status",
    "review_required",
    "document",
    "page",
    "quote",
    "audit_ok",
    "correct_status",
    "notes",
)

YES = {"yes", "y", "true", "1", "ok", "correct"}
NO = {"no", "n", "false", "0", "wrong", "incorrect"}

CAVEAT = (
    "The sample holds only pairs the system already answered, so these numbers "
    "measure the precision of what the output says - not what it missed."
)


class AuditError(Exception):
    """Input the grader refuses, with what to fix."""


def load_sheet(path: Path) -> list[dict]:
    """Read a filled sheet, joined on `pair`, refusing anything not graded."""
    try:
        handle = path.open(encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise AuditError(f"could not read the sheet {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        missing = [name for name in REQUIRED if name not in columns]
        if missing:
            raise AuditError(
                f"{path.name} is not an audit sheet this tool wrote: it has no "
                f"{', '.join(missing)} column. Export one with audit_sheet.py."
            )
        rows = list(reader)

    if not rows:
        raise AuditError(f"{path} has no rows to grade")

    problems: list[str] = []
    unfilled: list[str] = []
    graded: list[dict] = []
    seen: dict[str, int] = {}

    for number, row in enumerate(rows, start=2):  # line 1 is the header
        pair = (row.get("pair") or "").strip()
        if not pair:
            problems.append(f"line {number}: no pair id, so the row cannot be joined")
        elif pair in seen:
            problems.append(
                f"line {number}: pair {pair} was already filled on line {seen[pair]}"
            )
            continue
        else:
            seen[pair] = number

        verdict = (row.get("audit_ok") or "").strip().lower()
        if verdict in YES:
            audit_ok = True
        elif verdict in NO:
            audit_ok = False
        else:
            audit_ok = None
            unfilled.append(pair or f"line {number}")

        correct_status = (row.get("correct_status") or "").strip()
        if correct_status and correct_status not in STATUSES:
            problems.append(
                f"{pair}: correct_status {correct_status!r} is not one of "
                f"{', '.join(STATUSES)}"
            )

        page = (row.get("page") or "").strip()
        if page and not page.isdigit():
            problems.append(f"{pair}: page {page!r} is not a page number")

        status = (row.get("status") or "").strip()
        if status not in STATUSES:
            problems.append(f"{pair}: status {status!r} is not one of {', '.join(STATUSES)}")

        graded.append(
            {
                "pair": pair,
                "requirement_id": (row.get("requirement_id") or "").strip(),
                "item_id": (row.get("item_id") or "").strip(),
                "manufacturer": (row.get("manufacturer") or "").strip(),
                "status": status,
                "review_required": (row.get("review_required") or "").strip().lower()
                in {"true", "yes", "y", "1"},
                "document": (row.get("document") or "").strip(),
                "page": int(page) if page.isdigit() else None,
                "quote": (row.get("quote") or "").strip(),
                "audit_ok": audit_ok,
                "correct_status": correct_status or None,
                "notes": (row.get("notes") or "").strip() or None,
            }
        )

    if unfilled:
        listed = ", ".join(unfilled[:10]) + (
            f" ... and {len(unfilled) - 10} more" if len(unfilled) > 10 else ""
        )
        problems.append(
            f"{len(unfilled)} row(s) have no audit_ok yet, so the sample is not "
            f"graded: {listed}"
        )
    if problems:
        raise AuditError(f"{path.name}:\n  " + "\n  ".join(problems))
    return graded


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def score(rows: list[dict], data_dir: Path) -> dict:
    audited = len(rows)
    wrong = [row for row in rows if not row["audit_ok"]]
    correct = [row for row in rows if row["audit_ok"]]
    false_confident = [row for row in wrong if not row["review_required"]]
    flagged_correct = [row for row in correct if row["review_required"]]

    confusion: dict[str, dict[str, int]] = {}
    for row in wrong:
        # A reviewer who only says "no" has not named the right status; that is
        # recorded rather than guessed at.
        named = row["correct_status"] or "not named"
        row_confusion = confusion.setdefault(row["status"], {})
        row_confusion[named] = row_confusion.get(named, 0) + 1

    index = corpus_index(data_dir)
    counts = {"verified": 0, "failed": 0, "uncited": 0, "unavailable": 0}
    unverified = []
    for row in (item for item in rows if item["status"] in CLAIMS):
        check = citation(
            index,
            {"quote": row["quote"], "page": row["page"], "document": row["document"]},
        )
        counts[check["outcome"]] += 1
        if check["outcome"] != "verified":
            unverified.append(
                {
                    "pair": row["pair"],
                    "manufacturer": row["manufacturer"],
                    "outcome": check["outcome"],
                    "reason": check["reason"],
                }
            )
    judged = counts["verified"] + counts["failed"] + counts["uncited"]

    return {
        "audited": audited,
        "accuracy": _ratio(len(correct), audited),
        "false_confident": {
            "count": len(false_confident),
            "rate": _ratio(len(false_confident), audited),
        },
        "flagged_but_correct": len(flagged_correct),
        "citations": {
            **counts,
            "claims": sum(1 for row in rows if row["status"] in CLAIMS),
            "correctness": _ratio(counts["verified"], judged),
            "unverified": unverified,
        },
        "confusion": confusion,
        "wrong": [
            {
                "pair": row["pair"],
                "item_id": row["item_id"],
                "requirement_id": row["requirement_id"],
                "manufacturer": row["manufacturer"],
                "published": row["status"],
                "reviewer_says": row["correct_status"],
                "review_required": row["review_required"],
                "notes": row["notes"],
            }
            for row in wrong
        ],
        "caveat": CAVEAT,
    }


def _percent(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(summary: dict) -> str:
    citations = summary["citations"]
    lines = [
        "# Audit of a filled sheet",
        "",
        f"{summary['audited']} row(s) audited by hand.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Accuracy among audited | {_percent(summary['accuracy'])} |",
        f"| False-confident rate | {_percent(summary['false_confident']['rate'])} "
        f"({summary['false_confident']['count']} wrong and not flagged) |",
        f"| Flagged but correct | {summary['flagged_but_correct']} "
        "(the system asked for a look and the look found nothing wrong) |",
        f"| Citation correctness | {_percent(citations['correctness'])} "
        f"({citations['verified']} verified, {citations['failed']} failed, "
        f"{citations['uncited']} uncited, {citations['unavailable']} source "
        "unavailable and excluded) |",
        "",
        CAVEAT,
    ]

    if summary["wrong"]:
        lines += [
            "",
            "## Rows the reviewer said wrong",
            "",
            "| Pair | Item | Manufacturer | Published | Reviewer says | Flagged | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in summary["wrong"]:
            lines.append(
                f"| {row['pair']} | {row['item_id']} | {row['manufacturer']} | "
                f"{row['published']} | {row['reviewer_says'] or '—'} | "
                f"{'yes' if row['review_required'] else 'NO'} | "
                f"{row['notes'] or '—'} |"
            )

    if summary["confusion"]:
        lines += [
            "",
            "## What the reviewer replaced a status with",
            "",
            "| Published | Reviewer says | Count |",
            "| --- | --- | --- |",
        ]
        for published, targets in sorted(summary["confusion"].items()):
            for named, count in sorted(targets.items()):
                lines.append(f"| {published} | {named} | {count} |")

    if citations["unverified"]:
        lines += [
            "",
            "## Quotes that were not verified",
            "",
            "| Pair | Manufacturer | Outcome | Reason |",
            "| --- | --- | --- | --- |",
        ]
        for row in citations["unverified"]:
            lines.append(
                f"| {row['pair']} | {row['manufacturer']} | {row['outcome']} | "
                f"{row['reason']} |"
            )

    return "\n".join(lines) + "\n"


def _utf8_console() -> None:
    """Write UTF-8 even when Windows hands the process a cp1252 pipe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Optional[list[str]] = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(
        prog="audit_score.py",
        description="Grade a filled audit sheet. The sheet is read, never written.",
    )
    parser.add_argument("--sheet", type=Path, required=True)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="data directory holding manufacturers/<slug>/manifest.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="summary file; defaults to eval/results/audit-<sheet>.json",
    )
    args = parser.parse_args(argv)

    data_dir = args.corpus
    if data_dir is None:
        from app.core.config import get_settings

        data_dir = Path(get_settings().data_dir)

    try:
        rows = load_sheet(args.sheet)
        summary = score(rows, data_dir)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.out or REPO_ROOT / "eval" / "results" / f"audit-{args.sheet.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(render_markdown(summary), end="")
    print(f"summary -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
