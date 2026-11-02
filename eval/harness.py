"""Score a stored report against hand-labelled pairs (PRD 15, FR-35/FR-36).

The harness is deliberately offline: it reads a report JSON that a run already
produced and a gold file a person wrote, and it computes the metrics the PRD
names. It does not call the API, the database or a model, so the same gold file
scores the same report the same way on any machine.

Two rules keep the numbers honest:

- **A missing prediction is not a correct one.** A label whose pair is absent
  from the report stops the run with the keys listed; a prediction with no
  label is counted as unlabelled, never as a pass.
- **A citation is verified the way the pipeline verifies it.** The quote is
  checked against the cited page with `app.services.match.verify.check_citation`,
  the code that gates publication, rather than a second implementation that
  could be more forgiving than the product.

Unsupported clauses are excluded from accuracy, precision, recall and the
false-confident rate, exactly as they are excluded from the score (PRD 8);
they are reported in their own block so they cannot disappear.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.match.verify import check_citation  # noqa: E402

# The statuses a technical requirement can be scored on, then the one that is
# not scored at all. The order is the PRD's.
VERIFIABLE = (
    "full_match",
    "partial_match",
    "missing_capability",
    "no_evidence_found",
)
UNSUPPORTED = "unsupported_requirement"
STATUSES = VERIFIABLE + (UNSUPPORTED,)
# A status that asserts something about the documents. Those rows owe a quote.
CLAIMS = ("full_match", "partial_match", "missing_capability")

GOLD_KEYS = (
    "tender_sha256",
    "requirement_id",
    "manufacturer",
    "status",
)
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class EvalError(Exception):
    """Input the harness refuses to score, with what to fix."""


@dataclass(frozen=True)
class GoldLabel:
    """One hand-made answer for one requirement × manufacturer pair."""

    tender_sha256: str
    requirement_id: str
    manufacturer: str
    status: str
    document: Optional[str] = None
    page: Optional[int] = None
    quote: Optional[str] = None
    note: Optional[str] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tender_sha256, self.requirement_id, self.manufacturer)


def load_gold(path: Path) -> list[GoldLabel]:
    """Read a gold file, refusing anything that is not a checkable label."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"could not read the gold file {path}: {exc}") from exc

    labels: list[GoldLabel] = []
    problems: list[str] = []
    seen: dict[tuple[str, str, str], int] = {}

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not JSON ({exc.msg})")
            continue
        if not isinstance(raw, dict):
            problems.append(f"line {number}: a label must be a JSON object")
            continue
        label, found = _parse_label(raw, number)
        problems.extend(found)
        if label is None:
            continue
        if label.key in seen:
            problems.append(
                f"line {number}: duplicate label for {_key(label.key)} "
                f"(first on line {seen[label.key]})"
            )
            continue
        seen[label.key] = number
        labels.append(label)

    if problems:
        raise EvalError(
            f"{path}: {len(problems)} problem(s):\n  " + "\n  ".join(problems)
        )
    if not labels:
        raise EvalError(f"{path}: the gold file has no labels")
    return labels


def _parse_label(raw: dict, number: int) -> tuple[Optional[GoldLabel], list[str]]:
    problems: list[str] = []
    for key in GOLD_KEYS:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            problems.append(f"line {number}: {key} must be a non-empty string")
    status = raw.get("status")
    if isinstance(status, str) and status not in STATUSES:
        problems.append(
            f"line {number}: status {status!r} is not one of {', '.join(STATUSES)}"
        )
    sha = raw.get("tender_sha256")
    if isinstance(sha, str) and not SHA256.match(sha):
        problems.append(f"line {number}: tender_sha256 must be 64 hex characters")

    document = raw.get("document")
    page = raw.get("page")
    quote = raw.get("quote")
    note = raw.get("note")

    if status in CLAIMS:
        if not isinstance(document, str) or not document.strip():
            problems.append(f"line {number}: a {status} label needs the document")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            problems.append(f"line {number}: a {status} label needs a page number")
        if not isinstance(quote, str) or not quote.strip():
            problems.append(
                f"line {number}: a {status} label needs the quote that states it"
            )
    else:
        # Silence and unsupported-ness are claims too: they have no quote, so
        # the label has to say where the labeller looked or why it is excluded.
        if not isinstance(note, str) or not note.strip():
            problems.append(
                f"line {number}: a {status} label needs a note saying how it was decided"
            )
        if quote or document or page is not None:
            problems.append(
                f"line {number}: a {status} label must not carry a quote, "
                "document or page"
            )

    if problems:
        return None, problems
    return (
        GoldLabel(
            tender_sha256=str(sha).lower(),
            requirement_id=str(raw["requirement_id"]),
            manufacturer=str(raw["manufacturer"]),
            status=str(status),
            document=str(document) if document else None,
            page=int(page) if isinstance(page, int) and not isinstance(page, bool) else None,
            quote=str(quote) if quote else None,
            note=str(note) if note else None,
        ),
        [],
    )


def load_report(path: Path) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalError(f"could not read the report {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvalError(f"{path} is not JSON: {exc.msg}") from exc
    if not isinstance(report, dict):
        raise EvalError(f"{path}: a report must be a JSON object")
    sha = (report.get("tender") or {}).get("sha256")
    if not sha:
        raise EvalError(
            f"{path}: the report does not record the tender's sha256, so a label "
            "cannot be matched to it. Re-export it with `make report RUN=...`."
        )
    return report


def report_rows(report: dict) -> dict[tuple[str, str, str], dict]:
    """Every requirement × manufacturer row, keyed the way gold labels are."""
    tender = report.get("tender") or {}
    tender_sha = str(tender.get("sha256") or "").lower()
    if not tender_sha:
        raise EvalError(
            "the report does not record the tender's sha256, so a label cannot "
            "be matched to it. Re-export it with `make report RUN=...`."
        )
    rows: dict[tuple[str, str, str], dict] = {}
    requirements = list(report.get("requirements") or [])
    requirements += list(report.get("unsupported_requirements") or [])
    for requirement in requirements:
        requirement_id = requirement.get("requirement_id")
        if not requirement_id:
            raise EvalError("the report has a requirement without a requirement_id")
        for result in requirement.get("results") or []:
            manufacturer = result.get("manufacturer")
            if not manufacturer:
                raise EvalError(
                    f"requirement {requirement_id} has a result without a manufacturer"
                )
            key = (tender_sha, str(requirement_id), str(manufacturer))
            if key in rows:
                raise EvalError(
                    f"the report has two rows for {_key(key)}; one pair, one answer"
                )
            rows[key] = result
    return rows


# --- citations -------------------------------------------------------------


def corpus_index(data_dir: Path) -> dict[tuple[str, str], Path]:
    """Document hashes and filenames from the manifests, without hashing files.

    A manifest is what the collector wrote, so it is the honest index of what
    the corpus holds; walking 157 MB of PDFs to rebuild the same map would be
    slow and would still say nothing about files the manifests do not list.
    """
    index: dict[tuple[str, str], Path] = {}
    for manifest in sorted((data_dir / "manufacturers").glob("*/manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in payload.get("documents") or []:
            path = manifest.parent / str(entry.get("path") or "")
            if not path.is_file():
                continue
            if entry.get("sha256"):
                index[("sha256", str(entry["sha256"]).lower())] = path
            if entry.get("filename"):
                index[("filename", str(entry["filename"]))] = path
    return index


def source_for(index: dict[tuple[str, str], Path], evidence: dict) -> Optional[Path]:
    sha = evidence.get("sha256")
    if sha and ("sha256", str(sha).lower()) in index:
        return index[("sha256", str(sha).lower())]
    name = evidence.get("document")
    if name and ("filename", str(name)) in index:
        return index[("filename", str(name))]
    return None


@lru_cache(maxsize=64)
def _page_text(path: str, page: int) -> Optional[str]:
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise EvalError("PyMuPDF is not installed, so citations cannot be checked") from exc
    try:
        with pymupdf.open(path) as document:
            if page < 1 or page > document.page_count:
                return None
            return document[page - 1].get_text()
    except Exception as exc:
        raise EvalError(f"could not read {Path(path).name}: {exc}") from exc


def citation(index: dict[tuple[str, str], Path], evidence: dict) -> dict:
    """Check one quoted claim against the page it cites.

    Public and taking the evidence row itself, because the audit sheet grades
    the same quotes from a CSV rather than from a report JSON.
    """
    evidence = evidence or {}
    quote = evidence.get("quote")
    page = evidence.get("page")
    if not quote or not str(quote).strip():
        return {"outcome": "uncited", "reason": "the claim carries no quote"}
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        return {"outcome": "uncited", "reason": "the claim cites no page"}

    path = source_for(index, evidence)
    if path is None:
        return {
            "outcome": "unavailable",
            "reason": "the cited document is not in the corpus on disk",
        }
    text = _page_text(str(path), page)
    if text is None:
        return {"outcome": "failed", "reason": f"the document has no page {page}"}
    verdict = check_citation(str(quote), text)
    return {
        "outcome": "verified" if verdict.passed else "failed",
        "reason": verdict.reason,
    }


# --- scoring ---------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


def score(report: dict, labels: list[GoldLabel], data_dir: Path) -> dict:
    rows = report_rows(report)
    labels_by_key = {label.key: label for label in labels}

    missing = [key for key in labels_by_key if key not in rows]
    if missing:
        listed = "\n  ".join(_key(key) for key in missing[:10])
        more = f"\n  ... and {len(missing) - 10} more" if len(missing) > 10 else ""
        raise EvalError(
            f"{len(missing)} label(s) name a pair the report does not contain, so "
            "they cannot be scored. Fix the label or score the report they belong "
            f"to:\n  {listed}{more}"
        )

    labelled = [(labels_by_key[key], rows[key]) for key in labels_by_key]
    verifiable = [pair for pair in labelled if pair[0].status != UNSUPPORTED]
    unsupported = [pair for pair in labelled if pair[0].status == UNSUPPORTED]

    confusion: dict[str, dict[str, int]] = {}
    for label, result in verifiable:
        row = confusion.setdefault(label.status, {})
        row[result["status"]] = row.get(result["status"], 0) + 1

    correct = sum(1 for label, result in verifiable if result["status"] == label.status)
    per_status: dict[str, dict] = {}
    for status in VERIFIABLE:
        true_positive = sum(
            1
            for label, result in verifiable
            if label.status == status and result["status"] == status
        )
        false_positive = sum(
            1
            for label, result in verifiable
            if label.status != status and result["status"] == status
        )
        false_negative = sum(
            1
            for label, result in verifiable
            if label.status == status and result["status"] != status
        )
        precision = _ratio(true_positive, true_positive + false_positive)
        recall = _ratio(true_positive, true_positive + false_negative)
        f1 = (
            round(2 * precision * recall / (precision + recall), 4)
            if precision and recall
            else None
        )
        per_status[status] = {
            "support": true_positive + false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    wrong_confident = [
        (label, result)
        for label, result in verifiable
        if result["status"] != label.status and not result.get("review_required")
    ]
    coverage_rows = [
        pair for pair in verifiable if pair[1]["status"] != "no_evidence_found"
    ]

    index = corpus_index(data_dir)
    checks = [
        (label, result, citation(index, result.get("evidence") or {}))
        for label, result in labelled
        if result["status"] in CLAIMS
    ]
    citation_counts = {"verified": 0, "failed": 0, "uncited": 0, "unavailable": 0}
    for _, _, check in checks:
        citation_counts[check["outcome"]] += 1
    judged = citation_counts["verified"] + citation_counts["failed"] + citation_counts["uncited"]

    unlabelled = [key for key in rows if key not in labels_by_key]

    wrong = [
        {
            "requirement_id": label.requirement_id,
            "manufacturer": label.manufacturer,
            "expected": label.status,
            "predicted": result["status"],
            "review_required": bool(result.get("review_required")),
            "review_reason": result.get("review_reason"),
        }
        for label, result in verifiable
        if result["status"] != label.status
    ]

    return {
        "labelled": len(labelled),
        "verifiable": len(verifiable),
        "unsupported": len(unsupported),
        "unlabelled_predictions": len(unlabelled),
        "accuracy": _ratio(correct, len(verifiable)),
        "confusion": confusion,
        "per_status": per_status,
        "false_confident": {
            "count": len(wrong_confident),
            "rate": _ratio(len(wrong_confident), len(verifiable)),
        },
        "coverage": {
            "with_evidence": len(coverage_rows),
            "rate": _ratio(len(coverage_rows), len(verifiable)),
        },
        "review": {
            "flagged": sum(1 for _, result in labelled if result.get("review_required")),
            "rate": _ratio(
                sum(1 for _, result in labelled if result.get("review_required")),
                len(labelled),
            ),
        },
        "citations": {
            **citation_counts,
            "claims": len(checks),
            "correctness": _ratio(citation_counts["verified"], judged),
            "checks": [
                {
                    "requirement_id": label.requirement_id,
                    "manufacturer": label.manufacturer,
                    "outcome": check["outcome"],
                    "reason": check["reason"],
                }
                for label, _, check in checks
                if check["outcome"] != "verified"
            ],
        },
        "unsupported_block": {
            "labelled": len(unsupported),
            "predicted_unsupported": sum(
                1 for _, result in unsupported if result["status"] == UNSUPPORTED
            ),
            "accuracy": _ratio(
                sum(1 for _, result in unsupported if result["status"] == UNSUPPORTED),
                len(unsupported),
            ),
        },
        "wrong": wrong,
    }


def _key(key: tuple[str, str, str]) -> str:
    tender, requirement, manufacturer = key
    return f"{tender[:12]}…/{requirement}/{manufacturer}"


# --- rendering -------------------------------------------------------------


def _percent(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def render_markdown(summary: dict) -> str:
    lines = [
        "# Evaluation",
        "",
        f"{summary['labelled']} labelled pair(s): {summary['verifiable']} verifiable, "
        f"{summary['unsupported']} unsupported. "
        f"{summary['unlabelled_predictions']} prediction(s) had no label and are "
        "not counted as correct.",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Status accuracy (verifiable) | {_percent(summary['accuracy'])} |",
        f"| False-confident rate | {_percent(summary['false_confident']['rate'])} "
        f"({summary['false_confident']['count']} wrong and unflagged) |",
        f"| Coverage | {_percent(summary['coverage']['rate'])} "
        f"({summary['coverage']['with_evidence']} not reported as no evidence) |",
        f"| Review rate | {_percent(summary['review']['rate'])} "
        f"({summary['review']['flagged']} flagged) |",
        f"| Citation correctness | {_percent(summary['citations']['correctness'])} "
        f"({summary['citations']['verified']} verified, "
        f"{summary['citations']['failed']} failed, "
        f"{summary['citations']['uncited']} uncited, "
        f"{summary['citations']['unavailable']} source unavailable) |",
    ]

    lines += [
        "",
        "## Per status",
        "",
        "| Status | Support | Precision | Recall | F1 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for status in VERIFIABLE:
        row = summary["per_status"][status]
        lines.append(
            f"| {status} | {row['support']} | {_percent(row['precision'])} | "
            f"{_percent(row['recall'])} | {_percent(row['f1'])} |"
        )

    lines += [
        "",
        "## Confusion (gold → predicted)",
        "",
        "| Gold | Predicted | Count |",
        "| --- | --- | --- |",
    ]
    for gold in VERIFIABLE:
        for predicted in VERIFIABLE:
            count = summary["confusion"].get(gold, {}).get(predicted, 0)
            if count:
                lines.append(f"| {gold} | {predicted} | {count} |")

    unsupported = summary["unsupported_block"]
    lines += [
        "",
        "## Unsupported (excluded from the metrics above)",
        "",
        f"{unsupported['labelled']} labelled, "
        f"{unsupported['predicted_unsupported']} predicted unsupported "
        f"({_percent(unsupported['accuracy'])}).",
    ]

    if summary["wrong"]:
        lines += [
            "",
            "## Wrong pairs",
            "",
            "| Requirement | Manufacturer | Expected | Predicted | Flagged |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in summary["wrong"]:
            lines.append(
                f"| {row['requirement_id']} | {row['manufacturer']} | "
                f"{row['expected']} | {row['predicted']} | "
                f"{'yes' if row['review_required'] else 'NO'} |"
            )

    if summary["citations"]["checks"]:
        lines += [
            "",
            "## Citations that were not verified",
            "",
            "| Requirement | Manufacturer | Outcome | Reason |",
            "| --- | --- | --- | --- |",
        ]
        for row in summary["citations"]["checks"]:
            lines.append(
                f"| {row['requirement_id']} | {row['manufacturer']} | "
                f"{row['outcome']} | {row['reason']} |"
            )

    return "\n".join(lines) + "\n"


# --- command line ----------------------------------------------------------


def _data_dir(override: Optional[Path]) -> Path:
    if override is not None:
        return override
    from app.core.config import get_settings

    return Path(get_settings().data_dir)


def _utf8_console() -> None:
    """Write UTF-8 even when Windows hands the process a cp1252 pipe.

    The rendered table uses an em dash for "not measured", and a redirected
    stdout on Windows would otherwise fail on it after a successful score.
    """
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
        prog="harness.py",
        description="Score a stored report against hand-labelled pairs.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser(
        "check", help="validate a gold file without a report"
    )
    check.add_argument("--gold", type=Path, required=True)

    score_command = commands.add_parser("score", help="score a report against a gold file")
    score_command.add_argument("--report", type=Path, required=True)
    score_command.add_argument("--gold", type=Path, required=True)
    score_command.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="data directory holding manufacturers/<slug>/manifest.json",
    )
    score_command.add_argument(
        "--out", type=Path, default=None, help="write the JSON summary here"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            labels = load_gold(args.gold)
            print(f"{args.gold}: {len(labels)} valid label(s)")
            return 0

        report = load_report(args.report)
        labels = load_gold(args.gold)
        summary = score(report, labels, _data_dir(args.corpus))
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.out:
        args.out.write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
    print(render_markdown(summary), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
