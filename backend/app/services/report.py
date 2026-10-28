"""The report of a run (FR-31 to FR-34, PRD 8, PRD 9).

A report is built **from what is stored**, never from an in-memory answer: the
rows it shows are the rows the run wrote, so a report and the results behind it
cannot disagree. Three renderings come from one report:

- **JSON**, the canonical form: every requirement, every manufacturer, every
  quote, every flag;
- **HTML**, the same thing for a bid manager, with the statuses explained and
  the incomplete-ingestion warning where it cannot be missed;
- **CSV**, one flat row per requirement × manufacturer for a spreadsheet.

Two rules shape the content:

- **No evidence is not missing.** A requirement the corpus is silent about is
  reported as *no evidence found* and counts towards coverage, never as a
  capability the manufacturer lacks (PRD 4).
- **A claim is only as good as its quote.** Every row carries the document,
  the page and the sentence, and a row without them says so instead of
  implying them.

Everything here is deterministic and reads the run's stored config snapshot, so
a report says which policy, model and corpus produced it (FR-34) even after the
settings have moved on.
"""

from __future__ import annotations

import csv
import html
import io
from typing import Optional

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.repositories.runs import RunRepo
from app.repositories.tender import TenderRepo
from app.schemas.enums import LedgerStatus, RequirementStatus
from app.services.runs import summarise

# What each status means, in the words of PRD 4. The HTML report carries this so
# a reader cannot misread "no evidence found" as a failing capability. It is
# presentation, not policy.
STATUS_MEANINGS = {
    "full_match": "The documented capability meets or exceeds the requirement, with a verified quote.",
    "partial_match": (
        "Close but not meeting it, met only through an optional feature or "
        "condition, or only part of a compound requirement."
    ),
    "missing_capability": (
        "The documents state that the capability exists with a failing value, "
        "or state that it is not offered."
    ),
    "unsupported_requirement": (
        "Not technically verifiable (commercial, legal, financial, warranty or "
        "vague). Excluded from the score and listed separately."
    ),
    "no_evidence_found": (
        "Technical and verifiable, but the corpus is silent. Neither a pass nor "
        "a fail: it counts towards coverage and is flagged for review."
    ),
}


def _policy_from(snapshot: dict):
    """The scoring policy the run was scored with, from its own snapshot.

    A report that re-read the current settings could describe a run with rules
    that did not exist when it ran. The snapshot is what the run recorded, so
    it is what the report uses (FR-34).
    """
    from app.core.config import get_settings, ScoringPolicy

    stored = (snapshot or {}).get("scoring") or {}
    if not stored:
        return get_settings().scoring
    try:
        return ScoringPolicy(**stored)
    except (TypeError, ValueError):  # pragma: no cover - a snapshot from an older schema
        return get_settings().scoring


def _pages(tender_repo: TenderRepo, tender_id: str, page_count: Optional[int]) -> dict:
    """Every page of the tender, and whether the tender was fully read.

    A page that failed is not a page that was processed, and the tender is
    *incomplete ingestion* while one is unresolved (PRD 4). A ledger shorter
    than the page count is incomplete too: a page nothing accounted for is a
    page nobody looked at.

    The declared count is the specification's, because the ledger's pages
    come from the specification. The wrapper PDF's own page count is only the
    fallback, for a tender read before the ledger existed (D-91).
    """
    pages = tender_repo.list_pages(tender_id)
    declared = _declared_pages(tender_repo, tender_id, pages)
    if declared is None:
        declared = page_count
    counts: dict[str, int] = {}
    unresolved: list[dict] = []
    skipped: list[dict] = []
    for page in pages:
        status = page.get("ledger_status") or ""
        counts[status] = counts.get(status, 0) + 1
        if status == LedgerStatus.FAILED.value:
            unresolved.append({"page": page.get("page")})
        elif status == LedgerStatus.SKIPPED.value:
            skipped.append({"page": page.get("page"), "reason": page.get("skip_reason")})

    accounted = len(pages)
    unaccounted = max(0, (declared or 0) - accounted)
    return {
        "declared": declared,
        "accounted": accounted,
        "unaccounted": unaccounted,
        "counts": counts,
        "unresolved": unresolved,
        "skipped": skipped,
        "incomplete_ingestion": bool(unresolved or unaccounted),
    }


def _declared_pages(
    tender_repo: TenderRepo, tender_id: str, pages: list[dict]
) -> Optional[int]:
    """How many pages the document the ledger describes declares.

    The ledger names the attachment its pages came from, so the count is that
    document's: a two-page specification inside a seventeen-page wrapper is
    two pages, and comparing it with seventeen would report fifteen pages
    nobody read.
    """
    source = next(
        (page.get("attachment_id") for page in pages if page.get("attachment_id")),
        None,
    )
    if source is None:
        return None
    for record in tender_repo.list_attachments(tender_id):
        if record.get("id") == source:
            return record.get("pages")
    return None


def _extraction(tender_repo: TenderRepo, tender_id: str) -> dict:
    """Whether the requirements were read in full.

    The page ledger says what happened to every page; the extraction stage
    says whether the reading of those pages finished. A tender can have a
    complete ledger and an extraction that stopped at the provider's limit,
    and that tender's report covers only the requirements read before the
    stop (D16).
    """
    stage = tender_repo.get_stage(tender_id, "extraction") or {}
    status = stage.get("status") or ""
    return {
        "status": status,
        "detail": stage.get("detail"),
        "complete": status in ("", "done", "skipped"),
    }


def build_report(
    session: Session,
    run_id: str,
    *,
    runs: Optional[RunRepo] = None,
    tenders: Optional[TenderRepo] = None,
) -> dict:
    """The canonical JSON report of a run.

    The repositories are injectable so the report can be built in a test
    without a database; production passes nothing and gets the real ones.
    """
    run_repo = runs or RunRepo(session)
    tender_repo = tenders or TenderRepo(session)

    run = run_repo.get(run_id)
    if run is None:
        raise NotFoundError(f"no run with id {run_id}")

    tender = tender_repo.get_tender(run["tender_id"]) or {}
    requirements = tender_repo.list_requirements(run["tender_id"])
    items = {
        item["item_id"]: item for item in tender_repo.list_items(run["tender_id"])
    }
    results = run_repo.results(run_id)

    by_requirement: dict[str, list[dict]] = {}
    for row in results:
        by_requirement.setdefault(row["requirement_id"], []).append(row)

    summaries = summarise(results, requirements, _policy_from(run["config_snapshot"]))
    # The summary names the manufacturer, so the rows can too. Nothing is
    # inferred from the id.
    names = {row["manufacturer_id"]: row["manufacturer"] for row in results}
    order = {row["id"]: index for index, row in enumerate(requirements)}

    matchable: list[dict] = []
    unsupported: list[dict] = []
    review_queue: list[dict] = []

    for requirement in requirements:
        rows = sorted(
            by_requirement.get(requirement["id"], []),
            key=lambda row: names.get(row["manufacturer_id"], ""),
        )
        item = items.get(requirement.get("item_id")) or {}
        entry = {
            "requirement_id": requirement["id"],
            "item_id": requirement.get("item_id"),
            "item_title": item.get("name"),
            "category": item.get("category"),
            "text": requirement.get("text") or requirement.get("source_text"),
            "source_text": requirement.get("source_text"),
            "type": requirement.get("type"),
            "mandatory": requirement.get("mandatory", True),
            "page": requirement.get("page"),
            "value": requirement.get("value"),
            "unit": requirement.get("unit"),
            "results": [
                {
                    "manufacturer_id": row["manufacturer_id"],
                    "manufacturer": row["manufacturer"],
                    "status": row["status"],
                    "score": row["score"],
                    "verification_outcome": row["verification_outcome"],
                    "applicability": row["applicability"],
                    "capability_text": row["capability_text"],
                    "confidence": row["confidence"],
                    "review_required": bool(row["review_required"]),
                    "review_reason": row["review_reason"],
                    "evidence": row["evidence"],
                }
                for row in rows
            ],
        }

        statuses = {row["status"] for row in rows}
        # A clause is unsupported when the run recorded it as such. The status
        # comes from the requirement's own type, so every manufacturer agrees;
        # a requirement with no result yet is not counted as either.
        if statuses == {RequirementStatus.UNSUPPORTED_REQUIREMENT.value}:
            unsupported.append(entry)
        else:
            matchable.append(entry)

        for row in rows:
            if row["review_required"]:
                review_queue.append(
                    {
                        "requirement_id": requirement["id"],
                        "manufacturer": row["manufacturer"],
                        "status": row["status"],
                        "reason": row["review_reason"],
                    }
                )

    expected = len(requirements) * len(run["manufacturers"])
    pages = _pages(tender_repo, run["tender_id"], tender.get("page_count"))
    extraction = _extraction(tender_repo, run["tender_id"])
    # Either half of the reading can be short, and one is enough to make the
    # report a partial one.
    incomplete = pages["incomplete_ingestion"] or not extraction["complete"]
    pages["incomplete_ingestion"] = incomplete
    return {
        "run": {
            "id": run["id"],
            "tender_id": run["tender_id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "error": run["error"],
            "manufacturers": run["manufacturers"],
            "corpus_hash": run["corpus_hash"],
        },
        "tender": {
            "id": tender.get("id"),
            "filename": tender.get("filename"),
            "sha256": tender.get("sha256"),
            "page_count": tender.get("page_count"),
            "pages": pages,
            "extraction": extraction,
        },
        # A report over a tender that was not read in full is not the final
        # word on that tender: it says so at the top, where a consumer looks
        # before it reads a score (PRD 4, D16).
        "incomplete_ingestion": incomplete,
        # The config is the run's own snapshot: what it was scored with, not
        # what the settings say now (FR-34).
        "config": run["config_snapshot"],
        "results": {
            "stored": len(results),
            "expected": expected,
            "complete": len(results) == expected,
        },
        "manufacturers": summaries,
        "requirements": sorted(
            matchable, key=lambda entry: order.get(entry["requirement_id"], 0)
        ),
        "unsupported_requirements": sorted(
            unsupported, key=lambda entry: order.get(entry["requirement_id"], 0)
        ),
        "review_queue": review_queue,
        "status_meanings": STATUS_MEANINGS,
    }


# --- CSV -------------------------------------------------------------------


def _csv_safe(value) -> str:
    """A cell a spreadsheet reads as text, not as a formula.

    Requirement text and quotes come from the tender and from manufacturers'
    PDFs. A cell that begins `=`, `+`, `-` or `@` is executed as a formula by
    a spreadsheet, so the document would be running in the reader's machine.
    Document text is data, never instructions (PRD 10).
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def render_csv(report: dict) -> str:
    """One row per requirement × manufacturer, plus the unsupported clauses.

    A flat table, because the first thing a bid manager does with a report is
    sort it. Every row keeps its evidence and its review flag, and a row with
    no evidence is still a row: dropping it would hide the gap.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "requirement_id",
            "item_id",
            "requirement",
            "type",
            "mandatory",
            "manufacturer",
            "status",
            "score",
            "verification_outcome",
            "applicability",
            "capability",
            "document",
            "page",
            "quote",
            "confidence",
            "review_required",
            "review_reason",
        ]
    )

    for section in ("requirements", "unsupported_requirements"):
        for requirement in report[section]:
            for row in requirement["results"]:
                evidence = row.get("evidence") or {}
                writer.writerow(
                    [
                        _csv_safe(requirement["requirement_id"]),
                        _csv_safe(requirement.get("item_id")),
                        _csv_safe(requirement.get("text")),
                        _csv_safe(requirement.get("type")),
                        _csv_safe("yes" if requirement.get("mandatory") else "no"),
                        _csv_safe(row.get("manufacturer")),
                        _csv_safe(row.get("status")),
                        _csv_safe(row.get("score")),
                        _csv_safe(row.get("verification_outcome")),
                        _csv_safe(row.get("applicability")),
                        _csv_safe(row.get("capability_text")),
                        _csv_safe(evidence.get("document")),
                        _csv_safe(evidence.get("page")),
                        _csv_safe(evidence.get("quote")),
                        _csv_safe(row.get("confidence")),
                        _csv_safe("yes" if row.get("review_required") else "no"),
                        _csv_safe(row.get("review_reason")),
                    ]
                )
    return buffer.getvalue()


# --- HTML ------------------------------------------------------------------


def _escape(value) -> str:
    """Every value in the HTML is data from a tender or a manufacturer."""
    return html.escape("" if value is None else str(value))


def _evidence_cell(evidence: Optional[dict]) -> str:
    if not evidence:
        return '<span class="muted">no evidence cited</span>'
    page = evidence.get("page")
    document = evidence.get("document") or "an unnamed document"
    quote = evidence.get("quote") or ""
    return (
        f'<span class="muted">{_escape(document)}'
        f"{', p.' + _escape(page) if page is not None else ''}</span>"
        + (f'<blockquote>{_escape(quote)}</blockquote>' if quote else "")
    )


def _status_cell(status: Optional[str]) -> str:
    return f'<span class="status status-{_escape(status)}">{_escape(status)}</span>'


def _manufacturer_table(report: dict) -> str:
    rows = []
    for summary in report["manufacturers"]:
        counts = ", ".join(
            f"{_escape(name)}: {_escape(count)}"
            for name, count in sorted((summary.get("status_counts") or {}).items())
        )
        rows.append(
            "<tr>"
            f"<td class=\"num\">{_escape(summary.get('rank'))}</td>"
            f"<td>{_escape(summary.get('manufacturer'))}</td>"
            f"<td class=\"num\">{_escape(summary.get('compatibility_percent'))}%</td>"
            f"<td class=\"num\">{_escape(summary.get('coverage_percent'))}%</td>"
            f"<td>{_escape(counts)}</td>"
            f"<td>{'<strong class=\"bad\">fails mandatory</strong>' if summary.get('fails_mandatory') else ''}</td>"
            f"<td class=\"num\">{_escape(summary.get('review_count'))}</td>"
            "</tr>"
        )
    return (
        "<h2>Manufacturers</h2>"
        "<p class=\"muted\">Ranked by compatibility %, then coverage %, then fewer "
        "review flags. The two percentages are shown side by side so a high score "
        "on thin evidence is visible. A manufacturer that fails a mandatory "
        "requirement is never presented as bid-compliant, however high its score.</p>"
        "<table><thead><tr><th>#</th><th>Manufacturer</th><th>Compatibility</th>"
        "<th>Coverage</th><th>Results</th><th>Mandatory</th><th>Reviews</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _requirement_tables(report: dict, manufacturer: dict) -> str:
    """One manufacturer's requirement rows: requirement, outcome, evidence."""
    identifier = manufacturer.get("manufacturer_id")
    rows = []
    for requirement in report["requirements"]:
        for row in requirement["results"]:
            if row["manufacturer_id"] != identifier:
                continue
            review = ""
            if row.get("review_required"):
                review = f'<span class="flag">review: {_escape(row.get("review_reason"))}</span>'
            rows.append(
                "<tr>"
                f"<td class=\"small\">{_escape(requirement.get('item_id'))}</td>"
                f"<td>{_escape(requirement.get('text'))}</td>"
                f"<td>{_status_cell(row.get('status'))}"
                f"<div class=\"small\">score {_escape(row.get('score'))} · "
                f"{_escape(row.get('applicability'))} · "
                f"{_escape(row.get('verification_outcome'))}</div></td>"
                f"<td>{_escape(row.get('capability_text')) or '<span class=\"muted\">—</span>'}"
                f"<div>{_evidence_cell(row.get('evidence'))}</div></td>"
                f"<td>{_escape(row.get('confidence'))} {review}</td>"
                "</tr>"
            )
    if not rows:
        return ""
    return (
        f"<h3>{_escape(manufacturer.get('manufacturer'))}</h3>"
        "<table><thead><tr><th>Item</th><th>Requirement</th><th>Status</th>"
        "<th>Capability and evidence</th><th>Confidence</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _unsupported_table(report: dict) -> str:
    rows = []
    for requirement in report["unsupported_requirements"]:
        reason = ""
        for row in requirement["results"]:
            if row.get("review_reason"):
                reason = row["review_reason"]
                break
        rows.append(
            "<tr>"
            f"<td class=\"small\">{_escape(requirement.get('item_id'))}</td>"
            f"<td>{_escape(requirement.get('text'))}</td>"
            f"<td>{_escape(requirement.get('type'))}</td>"
            f"<td>{_escape(reason)}</td>"
            "</tr>"
        )
    if not rows:
        return "<h2>Unsupported requirements</h2><p class=\"muted\">None.</p>"
    return (
        "<h2>Unsupported requirements</h2>"
        "<p class=\"muted\">Not technically verifiable. Excluded from the score and "
        "listed separately, never counted against a manufacturer.</p>"
        "<table><thead><tr><th>Item</th><th>Requirement</th><th>Type</th>"
        f"<th>Why</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _banner(report: dict) -> str:
    parts: list[str] = []
    pages = report["tender"]["pages"]
    extraction = report["tender"].get("extraction") or {}
    if pages["incomplete_ingestion"]:
        detail = []
        if pages["unresolved"]:
            detail.append(
                "failed pages: "
                + ", ".join(str(page["page"]) for page in pages["unresolved"])
            )
        if pages["unaccounted"]:
            detail.append(f"{pages['unaccounted']} page(s) not in the ledger")
        if extraction and not extraction.get("complete", True):
            detail.append(
                "the extraction of requirements stopped short"
                + (
                    f" ({extraction['detail']})"
                    if extraction.get("detail")
                    else ""
                )
            )
        parts.append(
            '<div class="banner bad"><strong>Incomplete ingestion.</strong> '
            "This tender was not read in full ("
            + _escape("; ".join(detail))
            + "), so a requirement marked <em>no evidence found</em> may be a page "
            "nobody read rather than a capability nobody documents. This is not a "
            "final compatibility report.</div>"
        )
    if report["run"].get("error"):
        parts.append(
            '<div class="banner bad"><strong>The run failed:</strong> '
            f"{_escape(report['run']['error'])}</div>"
        )
    if not report["results"]["complete"]:
        parts.append(
            '<div class="banner warn"><strong>Partial results.</strong> '
            f"{_escape(report['results']['stored'])} of "
            f"{_escape(report['results']['expected'])} requirement × manufacturer "
            "assessments were stored. A missing assessment is not a pass or a "
            "fail.</div>"
        )
    return "".join(parts)


def render_html(report: dict) -> str:
    """The readable report: the same facts, with the statuses explained.

    Every document-derived value is escaped: a tender is data, and a quote that
    contains markup must be shown as text, not rendered (PRD 10).
    """
    run = report["run"]
    tender = report["tender"]
    pages = tender["pages"]
    config = report.get("config") or {}

    config_rows = "".join(
        f"<tr><th>{_escape(key)}</th><td><code>{_escape(value)}</code></td></tr>"
        for key, value in sorted(config.items())
        if not isinstance(value, dict)
    )
    config_detail = "".join(
        f"<tr><th>{_escape(key)}</th><td><code>{_escape(value)}</code></td></tr>"
        for key, value in sorted(config.items())
        if isinstance(value, dict)
    )

    status_rows = "".join(
        f"<tr><td>{_status_cell(name)}</td><td>{_escape(meaning)}</td></tr>"
        for name, meaning in STATUS_MEANINGS.items()
    )

    page_counts = ", ".join(
        f"{_escape(name) or 'unknown'}: {_escape(count)}"
        for name, count in sorted(pages["counts"].items())
    ) or "none"

    sections = "".join(
        _requirement_tables(report, manufacturer)
        for manufacturer in report["manufacturers"]
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Tender matching report — {_escape(tender.get("filename"))}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 70rem; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.5rem; margin-bottom: .25rem; }}
  h2 {{ margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: .25rem; }}
  h3 {{ margin-top: 1.75rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: .75rem 0; font-size: .9rem; }}
  th, td {{ text-align: left; vertical-align: top; padding: .4rem .5rem; border-bottom: 1px solid #eee; }}
  thead th {{ background: #f6f6f6; }}
  blockquote {{ margin: .4rem 0 0; padding: .25rem .6rem; border-left: 3px solid #ccc; color: #333; }}
  .muted, .small {{ color: #666; font-size: .85em; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .bad {{ color: #a30000; }}
  .banner {{ padding: .75rem 1rem; border-radius: 4px; margin: 1rem 0; }}
  .banner.bad {{ background: #fdeaea; border: 1px solid #e0a3a3; }}
  .banner.warn {{ background: #fff8e6; border: 1px solid #e5cf8a; }}
  .flag {{ color: #8a4b00; font-size: .85em; }}
  .status {{ font-weight: 600; }}
  .status-full_match {{ color: #0a6b2d; }}
  .status-partial_match {{ color: #8a5a00; }}
  .status-missing_capability {{ color: #a30000; }}
  .status-no_evidence_found {{ color: #4a4a8a; }}
  .status-unsupported_requirement {{ color: #555; }}
</style>
</head>
<body>
<h1>Tender matching report</h1>
<p class="muted">
  {_escape(tender.get("filename"))} ·
  run {_escape(run["id"])} ·
  status {_escape(run["status"])} ·
  started {_escape(run.get("started_at"))}
</p>
<p class="muted">
  Documents are read and extracted with the LLM; whether a capability matches is
  decided by deterministic comparison, and every claim passes a verifier before
  it is published. A claim below carries the document, the page and the sentence
  it was read from.
</p>
{_banner(report)}
<h2>Corpus and pages</h2>
<table>
  <tbody>
    <tr><th>Corpus hash</th><td><code>{_escape(run.get("corpus_hash"))}</code></td></tr>
    <tr><th>Pages read</th><td>{_escape(pages["accounted"])} of {_escape(pages["declared"])} ({_escape(page_counts)})</td></tr>
    <tr><th>Manufacturers</th><td>{_escape(", ".join(run.get("manufacturers") or []))}</td></tr>
    <tr><th>Assessments</th><td>{_escape(report["results"]["stored"])} of {_escape(report["results"]["expected"])}</td></tr>
  </tbody>
</table>
<h2>Configuration</h2>
<p class="muted">What this report was produced with. Two reports with different
values here are not directly comparable (FR-34).</p>
<table><tbody>{config_rows}{config_detail}</tbody></table>
{_manufacturer_table(report)}
<h2>Requirements</h2>
{sections or '<p class="muted">No requirement was assessed.</p>'}
{_unsupported_table(report)}
<h2>Statuses</h2>
<table><thead><tr><th>Status</th><th>Meaning</th></tr></thead>
<tbody>{status_rows}</tbody></table>
</body>
</html>
"""


# --- command line ----------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """Build a run's report without the HTTP API.

    `make report RUN=<run_id> FORMAT=html OUT=report.html` is the same report
    the endpoints serve, from the same stored rows: a demo or a review does not
    need a server, and a report written to a file is a report that can be sent.
    """
    import argparse

    from app.repositories.db import session_scope

    parser = argparse.ArgumentParser(description="Build a matching run's report.")
    parser.add_argument("run_id")
    parser.add_argument(
        "--format", choices=("json", "html", "csv"), default="json"
    )
    parser.add_argument(
        "--out", default=None, help="write here instead of standard output"
    )
    args = parser.parse_args(argv)

    with session_scope() as session:
        report = build_report(session, args.run_id)

    if args.format == "html":
        text = render_html(report)
    elif args.format == "csv":
        text = render_csv(report)
    else:
        import json

        text = json.dumps(report, indent=2)

    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())