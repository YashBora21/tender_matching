"""Retrieval recall@k, and why a label was missed (PRD 15, D-98).

The report scorer in `harness.py` answers "was the published status right".
This module answers the question before it: **did retrieval even offer the
page the answer is on**. A miss is only actionable when it is attributed, so
every label that is not a hit is put in one of four buckets:

| Cause | What it means | What to change |
| --- | --- | --- |
| `corpus_gap` | the gold document is not in the corpus | collect the document |
| `chunking` | the document is in the corpus, but no chunk covers the gold page | the reader/chunker lost the page |
| `retrieval` | the page is indexed, but it is not in the top k | the query, the boost, the filter |
| `verifier` | the gold page *was* in the top k, but the published status is wrong | extraction, the verifier or the comparison |

The last bucket is only filled when the run's results are available; without
them the module measures retrieval alone.

Two recalls are measured and reported apart: **document recall** asks whether
the gold document was offered at all, **page recall** asks whether the gold
page was offered. A document found on the wrong page is a ranking problem
inside a document we already hold, and the two numbers must not be averaged
into one.

Two refusals keep the number honest:

- a label file with no rows, or with no row that names a gold document and
  page, is refused. Recall over an empty set is not 0%, it is not a
  measurement (D-98);
- a row with no gold document/page is counted as *unlabelled*, never as a
  miss. The gold slice that prompted this module had 21 rows and no gold
  columns; scoring it would have published a 0% that meant "nobody labelled
  it".

The gold file is the CSV the review produced, or JSONL with the same field
names: `requirement_id`, `requirement`, `manufacturer`, `item_id`,
`item_title`, `gold_document`, `gold_page`, and optionally `true_status`.
`gold_document`/`gold_page` are the labeller's answer, not the run's cited
`document`/`page`.

A file may also carry `label_state` / `confirmed`. When it does, only rows the
reviewer confirmed are measured; a proposed row is counted as *unconfirmed*
and left out, exactly like an unlabelled one. A file of proposals therefore
cannot publish a recall nobody signed off. A file with neither column is read
as before (all rows are treated as confirmed), so the existing gold slice keeps
working.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

CORPUS_GAP = "corpus_gap"
CHUNKING = "chunking"
RETRIEVAL = "retrieval"
VERIFIER = "verifier"
CAUSES = (CORPUS_GAP, CHUNKING, RETRIEVAL, VERIFIER)

DEFAULT_K = (3, 20)


class RecallError(Exception):
    """Input the harness refuses to measure, with what to fix."""


@dataclass(frozen=True)
class RecallLabel:
    """One requirement and the page that should answer it."""

    requirement_id: str
    requirement: str
    manufacturer: str
    gold_document: str
    gold_page: int
    item_id: Optional[str] = None
    item_title: Optional[str] = None
    true_status: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.requirement_id} x {self.manufacturer}"


@dataclass(frozen=True)
class Ranked:
    """One retrieved chunk, as the ranking saw it."""

    document: Optional[str]
    page: Optional[int]


@dataclass
class RecallRow:
    """One label, its hits, and the cause when it missed.

    `hit_at` is page recall: the gold document *and* the gold page are in the
    top k. `document_hit_at` is document recall: the gold document is in the
    top k on any page. They answer different questions - "did we find the
    datasheet" and "did we offer the page that answers the clause" - and a
    report that only prints the second hides whether the document was found
    at all.
    """

    label: RecallLabel
    hit_at: dict[int, bool]
    document_hit_at: dict[int, bool] = field(default_factory=dict)
    cause: Optional[str] = None
    detail: str = ""


@dataclass
class RecallReport:
    rows: list[RecallRow]
    k_values: tuple[int, ...] = DEFAULT_K
    unlabelled: int = 0
    unconfirmed: int = 0

    @property
    def total(self) -> int:
        return len(self.rows)

    def hits(self, k: int) -> int:
        return sum(1 for row in self.rows if row.hit_at.get(k))

    def recall(self, k: int) -> float:
        if not self.rows:
            return 0.0
        return self.hits(k) / len(self.rows)

    def document_hits(self, k: int) -> int:
        """How many labels had the gold document in the top k, on any page."""
        return sum(1 for row in self.rows if row.document_hit_at.get(k))

    def document_recall(self, k: int) -> float:
        if not self.rows:
            return 0.0
        return self.document_hits(k) / len(self.rows)

    def causes(self) -> dict[str, int]:
        counts = {cause: 0 for cause in CAUSES}
        for row in self.rows:
            if row.cause in counts:
                counts[row.cause] += 1
        return counts

    def misses(self) -> list[RecallRow]:
        return [row for row in self.rows if row.cause]


def document_key(name: Optional[str]) -> str:
    """The form a document name is compared in: the file stem, case-folded.

    A candidate carries the stem of its path; a label may write the file name
    with an extension, a directory or the other slash. The stem is the part
    both can agree on.
    """
    if not name:
        return ""
    return Path(str(name).replace("\\", "/")).stem.casefold()


def _gold_page(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        page = int(text)
    except ValueError:
        return None
    return page if page >= 1 else None


CONFIRMED_VALUES = {"yes", "true", "1", "y", "confirmed"}


def _is_confirmed(raw: dict, declared: bool) -> bool:
    """Whether a row was signed off by the reviewer.

    A file that declares no confirmation column is read as before: every row
    that names a gold page is a label (D-98). A file that does declare one
    only measures the rows that are confirmed; a proposal is not a label.
    """
    if not declared:
        return True
    confirmed = str(raw.get("confirmed") or "").strip().lower()
    state = str(raw.get("label_state") or "").strip().lower()
    return confirmed in CONFIRMED_VALUES or state == "confirmed"


def load_labels(path: Path) -> tuple[list[RecallLabel], int, int]:
    """Read the labelled rows.

    Returns the confirmed labels, the count of rows with no gold page, and the
    count of rows that were not confirmed. Neither count is measured: recall
    over labels nobody confirmed is not a measurement (D-98).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RecallError(f"could not read the labels {path}: {exc}") from exc

    csv_file = path.suffix.lower() == ".csv"
    if csv_file:
        rows = list(csv.DictReader(io.StringIO(text)))
        first_line = 2
    else:
        rows = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecallError(
                    f"{path} line {number}: not JSON ({exc.msg})"
                ) from exc
            if not isinstance(raw, dict):
                raise RecallError(f"{path} line {number}: a label must be an object")
            rows.append(raw)
        first_line = 1

    if not rows:
        raise RecallError(f"{path}: the label file has no rows")

    # The file as a whole decides whether confirmation is required. A file that
    # carries the columns but confirms none of them is a proposal file, and is
    # refused below rather than silently measured.
    declared = any(
        ("confirmed" in raw) or ("label_state" in raw) for raw in rows
    )

    labels: list[RecallLabel] = []
    unlabelled = 0
    unconfirmed = 0
    for offset, raw in enumerate(rows):
        number = first_line + offset
        if not _is_confirmed(raw, declared):
            unconfirmed += 1
            continue
        document = str(raw.get("gold_document") or "").strip()
        page = _gold_page(raw.get("gold_page"))
        if not document and page is None:
            unlabelled += 1
            continue
        if not document or page is None:
            raise RecallError(
                f"{path} row {number}: a recall label needs both gold_document "
                "and gold_page"
            )
        requirement_id = str(raw.get("requirement_id") or "").strip()
        requirement = str(raw.get("requirement") or "").strip()
        manufacturer = str(raw.get("manufacturer") or "").strip()
        if not (requirement_id and requirement and manufacturer):
            raise RecallError(
                f"{path} row {number}: a recall label needs requirement, "
                "requirement_id and manufacturer"
            )
        labels.append(
            RecallLabel(
                requirement_id=requirement_id,
                requirement=requirement,
                manufacturer=manufacturer,
                gold_document=document,
                gold_page=page,
                item_id=str(raw.get("item_id") or "").strip() or None,
                item_title=str(raw.get("item_title") or "").strip() or None,
                true_status=str(
                    raw.get("true_status") or raw.get("status") or ""
                ).strip()
                or None,
            )
        )

    if not labels:
        if unconfirmed:
            raise RecallError(
                f"{path}: none of the {len(rows)} row(s) is confirmed, so "
                "there is nothing to measure; confirm the proposals first"
            )
        raise RecallError(
            f"{path}: none of the {len(rows)} row(s) names the gold document "
            "and page, so there is nothing to measure"
        )
    return labels, unlabelled, unconfirmed


class Ranking(Protocol):
    """What the harness needs from a corpus: rank, pages, and the result."""

    def rank(self, label: RecallLabel, limit: int) -> list[Ranked]:
        ...

    def pages(self, document: str) -> Optional[set[int]]:
        """The pages the document has a chunk on, or None when it is absent."""

    def result_status(self, label: RecallLabel) -> Optional[str]:
        """The published status for the pair, when a run has one."""


def evaluate(
    labels: list[RecallLabel],
    ranking: Ranking,
    *,
    k_values: tuple[int, ...] = DEFAULT_K,
    unlabelled: int = 0,
    unconfirmed: int = 0,
) -> RecallReport:
    """Measure recall@k and attribute every label that was not a hit."""
    if not labels:
        raise RecallError("no labels: recall over an empty set is not a measurement")
    ks = tuple(sorted({int(k) for k in k_values if int(k) > 0}))
    if not ks:
        raise RecallError("no k values to measure")
    deepest = max(ks)

    rows: list[RecallRow] = []
    for label in labels:
        ranked = ranking.rank(label, deepest)
        gold_document = document_key(label.gold_document)
        document_hit_at = {
            k: any(
                document_key(item.document) == gold_document
                for item in ranked[:k]
            )
            for k in ks
        }
        hit_at = {
            k: any(
                document_key(item.document) == gold_document
                and item.page == label.gold_page
                for item in ranked[:k]
            )
            for k in ks
        }

        cause: Optional[str] = None
        detail = ""
        if not hit_at[deepest]:
            pages = ranking.pages(label.gold_document)
            if pages is None:
                cause = CORPUS_GAP
                detail = f"{label.gold_document} is not in the corpus"
            elif label.gold_page not in pages:
                cause = CHUNKING
                detail = (
                    f"{label.gold_document} has no chunk on page {label.gold_page}"
                )
            else:
                cause = RETRIEVAL
                detail = f"the page is indexed but not in the top {deepest}"
        else:
            # The page was offered. If the published answer is still wrong,
            # retrieval is not the problem: the failure is downstream.
            actual = ranking.result_status(label)
            if (
                actual is not None
                and label.true_status
                and actual != label.true_status
            ):
                cause = VERIFIER
                detail = (
                    "the gold page was retrieved, but the result is "
                    f"{actual}, not {label.true_status}"
                )
        rows.append(
            RecallRow(
                label=label,
                hit_at=hit_at,
                document_hit_at=document_hit_at,
                cause=cause,
                detail=detail,
            )
        )

    return RecallReport(
        rows=rows, k_values=ks, unlabelled=unlabelled, unconfirmed=unconfirmed
    )


class DatabaseRanking:
    """The corpus and the run, read from the database.

    It uses the same `build_query`, type inference and `search` the product
    uses, so the recall measured here is the recall the matcher gets. A second
    implementation could be more forgiving than the pipeline.
    """

    def __init__(self, session) -> None:
        self.session = session
        self._index: Optional[dict[str, tuple[str, set[int]]]] = None
        self._requirements: dict[str, dict] = {}

    def _documents(self) -> dict[str, tuple[str, set[int]]]:
        if self._index is None:
            from sqlalchemy import select

            from app.repositories.schema import chunk, document

            rows = self.session.execute(
                select(document.c.id, document.c.path, chunk.c.page)
                .select_from(document)
                .outerjoin(chunk, chunk.c.document_id == document.c.id)
            ).all()
            index: dict[str, list] = {}
            for document_id, path, page in rows:
                entry = index.setdefault(document_key(path), [str(document_id), set()])
                if page is not None:
                    entry[1].add(page)
            self._index = {key: (value[0], value[1]) for key, value in index.items()}
        return self._index

    def pages(self, document: str) -> Optional[set[int]]:
        entry = self._documents().get(document_key(document))
        return None if entry is None else set(entry[1])

    def _requirement(self, label: RecallLabel) -> dict:
        if label.requirement_id in self._requirements:
            return self._requirements[label.requirement_id]
        from sqlalchemy import select

        from app.repositories.schema import requirement, tender_item

        details: dict = {}
        try:
            identifier = uuid.UUID(label.requirement_id)
        except ValueError:
            self._requirements[label.requirement_id] = details
            return details
        row = self.session.execute(
            select(
                requirement.c.attribute,
                requirement.c.value,
                requirement.c.unit,
                requirement.c.item_id,
                requirement.c.tender_id,
            ).where(requirement.c.id == identifier)
        ).first()
        if row is not None:
            details = {
                "attribute": row[0],
                "value": row[1],
                "unit": row[2],
                "item_id": row[3],
                "tender_id": row[4],
            }
            category = self.session.execute(
                select(tender_item.c.category).where(
                    tender_item.c.tender_id == row[4],
                    tender_item.c.item_id == row[3],
                )
            ).first()
            details["category"] = category[0] if category is not None else None
        self._requirements[label.requirement_id] = details
        return details

    def rank(self, label: RecallLabel, limit: int) -> list[Ranked]:
        from app.services.categories import infer_type
        from app.services.matching import build_query
        from app.services.retrieval import search

        details = self._requirement(label)
        category = details.get("category")
        if category == "unknown":
            category = None
        item_title = label.item_title
        product_type = None
        if category and item_title:
            product_type = infer_type(category, item_title).product_type
        query = build_query(
            item_title,
            label.requirement,
            attribute=details.get("attribute"),
            value=details.get("value"),
            unit=details.get("unit"),
        )
        candidates = search(
            self.session,
            query,
            limit=limit,
            manufacturer_name=label.manufacturer,
            category=category,
            product_type=product_type,
        )
        return [Ranked(document=c.document_title, page=c.page) for c in candidates]

    def result_status(self, label: RecallLabel) -> Optional[str]:
        from sqlalchemy import select

        from app.repositories.schema import manufacturer, result, run

        try:
            identifier = uuid.UUID(label.requirement_id)
        except ValueError:
            return None
        row = self.session.execute(
            select(result.c.status)
            .select_from(result)
            .join(run, run.c.id == result.c.run_id)
            .join(manufacturer, manufacturer.c.id == result.c.manufacturer_id)
            .where(
                result.c.requirement_id == identifier,
                manufacturer.c.name == label.manufacturer,
            )
            .order_by(run.c.started_at.desc())
            .limit(1)
        ).first()
        return str(row[0]) if row is not None else None


def render(report: RecallReport) -> str:
    """A short text report: the recall, the buckets, then every miss.

    Document recall and page recall are printed separately: the first says the
    datasheet was found, the second says the page that answers the clause was
    offered. A miss on the second with a hit on the first is a chunking or
    ranking problem inside a document we already have.
    """
    lines: list[str] = []
    for k in report.k_values:
        lines.append(
            f"document recall@{k}: {report.document_recall(k):.1%} "
            f"({report.document_hits(k)}/{report.total})"
        )
        lines.append(
            f"page recall@{k}: {report.recall(k):.1%} "
            f"({report.hits(k)}/{report.total})"
        )
    if report.unlabelled:
        lines.append(
            f"unlabelled: {report.unlabelled} row(s) had no gold document/page "
            "and are not counted"
        )
    if report.unconfirmed:
        lines.append(
            f"unconfirmed: {report.unconfirmed} row(s) were proposals and are "
            "not counted; confirm them first"
        )
    lines.append("")
    lines.append("misses by cause:")
    for cause, count in report.causes().items():
        lines.append(f"  {cause:<11} {count}")
    misses = report.misses()
    if misses:
        lines.append("")
        for row in misses:
            lines.append(
                f"- {row.label.key} [{row.cause}] {row.label.gold_document} "
                f"p.{row.label.gold_page}: {row.detail}"
            )
    return "\n".join(lines)


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
        prog="recall.py",
        description="Measure retrieval recall@k against gold document/page labels.",
    )
    parser.add_argument("--gold", required=True, help="the labelled CSV or JSONL")
    parser.add_argument(
        "--k",
        type=int,
        nargs="+",
        default=list(DEFAULT_K),
        help="the cut-offs to measure (default: 3 20)",
    )
    parser.add_argument("--json-out", help="also write the report as JSON here")
    args = parser.parse_args(argv)

    try:
        labels, unlabelled, unconfirmed = load_labels(Path(args.gold))

        from app.repositories.db import session_scope

        with session_scope() as session:
            report = evaluate(
                labels,
                DatabaseRanking(session),
                k_values=tuple(args.k),
                unlabelled=unlabelled,
                unconfirmed=unconfirmed,
            )
    except RecallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(render(report))
    if args.json_out:
        payload = {
            "k_values": list(report.k_values),
            "total": report.total,
            "unlabelled": report.unlabelled,
            "unconfirmed": report.unconfirmed,
            "recall": {str(k): report.recall(k) for k in report.k_values},
            "hits": {str(k): report.hits(k) for k in report.k_values},
            "document_recall": {
                str(k): report.document_recall(k) for k in report.k_values
            },
            "document_hits": {
                str(k): report.document_hits(k) for k in report.k_values
            },
            "causes": report.causes(),
            "misses": [
                {
                    "requirement_id": row.label.requirement_id,
                    "manufacturer": row.label.manufacturer,
                    "gold_document": row.label.gold_document,
                    "gold_page": row.label.gold_page,
                    "cause": row.cause,
                    "detail": row.detail,
                }
                for row in report.misses()
            ],
        }
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
