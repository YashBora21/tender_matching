"""The report of a run: JSON, HTML and CSV (FR-31 to FR-34).

The report is built from stored rows through fake repositories, so what is
tested is the shape of the answer, what it does with missing evidence, and that
a document's text is shown as text in every rendering.
"""

from __future__ import annotations

import json

from app.services.output.report import build_report, render_csv, render_html

RUN_ID = "11111111-1111-4111-8111-111111111111"
TENDER_ID = "22222222-2222-4222-8222-222222222222"


class FakeRunRepo:
    def __init__(self, run: dict, results: list[dict]) -> None:
        self.run = run
        self.stored = results

    def get(self, run_id):
        return dict(self.run) if run_id == RUN_ID else None

    def results(self, run_id):
        return list(self.stored)


class FakeTenderRepo:
    def __init__(
        self,
        requirements,
        items,
        pages=None,
        tender=None,
        stage=None,
        attachments=None,
        requirements_stage=None,
    ) -> None:
        self.requirements = requirements
        self.items = items
        self.pages = pages or []
        self.attachments = attachments or []
        self.stage = stage
        self.requirements_stage = requirements_stage
        self.tender = tender if tender is not None else {
            "id": TENDER_ID,
            "filename": "tender.pdf",
            "sha256": "a" * 64,
            "page_count": 2,
        }

    def get_tender(self, tender_id):
        return dict(self.tender)

    def get_stage(self, tender_id, section):
        stage = self.requirements_stage if section == "requirements" else self.stage
        return dict(stage) if stage is not None else None

    def list_requirements(self, tender_id):
        return list(self.requirements)

    def list_items(self, tender_id):
        return list(self.items)

    def list_pages(self, tender_id):
        return list(self.pages)

    def list_attachments(self, tender_id):
        return list(self.attachments)


REQUIREMENTS = [
    {
        "id": "r1",
        "item_id": "1",
        "text": "Body shall be SS316",
        "source_text": "Body shall be SS316",
        "type": "technical",
        "mandatory": True,
        "page": 3,
        "value": None,
        "unit": None,
    },
    {
        "id": "r2",
        "item_id": "1",
        "text": "Delivery within 180 days",
        "source_text": "Delivery within 180 days",
        "type": "commercial",
        "mandatory": True,
        "page": 3,
        "value": None,
        "unit": None,
    },
]

ITEMS = [{"item_id": "1", "name": "GATE VALVE SIZE 150 MM", "category": "valves"}]


def _result(
    requirement_id,
    manufacturer_id,
    manufacturer,
    status,
    *,
    score=1.0,
    evidence=None,
    review_required=False,
    review_reason=None,
    reason_code=None,
    capability="Body material SS316",
    verification="passed",
    applicability="model_level",
):
    return {
        "id": f"result-{requirement_id}-{manufacturer_id}",
        "requirement_id": requirement_id,
        "manufacturer_id": manufacturer_id,
        "manufacturer": manufacturer,
        "status": status,
        "verification_outcome": verification,
        "applicability": applicability,
        "score": score,
        "capability_text": capability,
        "confidence": "high",
        "review_required": review_required,
        "review_reason": review_reason,
        "reason_code": reason_code,
        "evidence": evidence,
    }


EVIDENCE = {
    "fact_id": "fact-1",
    "page": 12,
    "quote": "Body: ASTM A351 CF8M (SS316)",
    "document_id": "doc-1",
    "document": "circor-d-series.pdf",
    "doc_type": "catalogue",
    "sha256": "b" * 64,
    # Where the document came from: the report shows a source link, not only
    # a filename.
    "url": "https://manufacturer.test/d-series.pdf",
}

RUN = {
    "id": RUN_ID,
    "tender_id": TENDER_ID,
    "status": "done",
    "started_at": "2026-10-25T10:00:00+00:00",
    "error": None,
    "manufacturers": ["Acme"],
    "corpus_hash": "c" * 64,
    "config_snapshot": {
        "llm_model": "the-run-model",
        "prompt_version": "1",
        "scoring": {},
    },
}


def _report(
    results=None,
    pages=None,
    tender=None,
    requirements=None,
    stage=None,
    attachments=None,
    requirements_stage=None,
):
    results = (
        results
        if results is not None
        else [
            _result("r1", "mfr-a", "Acme", "full_match", evidence=EVIDENCE),
            _result(
                "r2",
                "mfr-a",
                "Acme",
                "unsupported_requirement",
                score=0.0,
                capability=None,
                verification="not_checked",
                applicability="unknown",
            ),
        ]
    )
    if pages is None:
        pages = [
            {"page": 1, "ledger_status": "parsed", "skip_reason": None},
            {"page": 2, "ledger_status": "ocr", "skip_reason": None},
        ]
    return build_report(
        object(),
        RUN_ID,
        runs=FakeRunRepo(RUN, results),
        tenders=FakeTenderRepo(
            requirements if requirements is not None else REQUIREMENTS,
            ITEMS,
            pages,
            tender,
            stage,
            attachments,
            requirements_stage,
        ),
    )


def test_a_requirement_row_carries_its_evidence_and_its_flags():
    report = _report()

    row = report["requirements"][0]
    assert row["requirement_id"] == "r1"
    assert row["item_id"] == "1"
    assert row["category"] == "valves"
    assert row["mandatory"] is True

    result = row["results"][0]
    assert result["manufacturer"] == "Acme"
    assert result["status"] == "full_match"
    # A claim without its quote is a claim nobody can check (PRD 13).
    assert result["evidence"]["page"] == 12
    assert result["evidence"]["quote"].startswith("Body: ASTM A351")
    # The document's own address travels with the quote, so the UI can offer
    # the source page as a link instead of inventing one.
    assert result["evidence"]["url"] == "https://manufacturer.test/d-series.pdf"


def test_an_unsupported_clause_is_listed_separately_and_not_scored():
    report = _report()

    assert [row["requirement_id"] for row in report["requirements"]] == ["r1"]
    assert [row["requirement_id"] for row in report["unsupported_requirements"]] == ["r2"]
    # The manufacturer summary scores only the verifiable requirement.
    assert report["manufacturers"][0]["compatibility_percent"] == 100.0
    assert report["manufacturers"][0]["status_counts"]["unsupported_requirement"] == 1


def test_a_failed_page_labels_the_tender_incomplete_ingestion():
    report = _report(
        pages=[
            {"page": 1, "ledger_status": "parsed", "skip_reason": None},
            {"page": 2, "ledger_status": "failed", "skip_reason": None},
        ]
    )

    pages = report["tender"]["pages"]
    assert pages["incomplete_ingestion"] is True
    assert pages["unresolved"] == [{"page": 2}]
    assert "Incomplete ingestion" in render_html(report)


def test_a_ledger_short_of_the_page_count_is_incomplete_too():
    """A page nothing accounted for is a page nobody looked at."""
    report = _report(
        pages=[{"page": 1, "ledger_status": "parsed", "skip_reason": None}],
        tender={
            "id": TENDER_ID,
            "filename": "tender.pdf",
            "sha256": "a" * 64,
            "page_count": 3,
        },
    )

    pages = report["tender"]["pages"]
    assert pages["accounted"] == 1
    assert pages["unaccounted"] == 2
    assert pages["incomplete_ingestion"] is True


def test_a_fully_read_tender_is_not_incomplete():
    pages = _report()["tender"]["pages"]
    assert pages["accounted"] == 2
    assert pages["unaccounted"] == 0
    assert pages["incomplete_ingestion"] is False


def test_the_declared_count_is_the_specifications_not_the_wrappers():
    """The ledger's pages come from the specification. A two-page
    specification inside a seventeen-page wrapper is two pages, and comparing
    it with seventeen would report fifteen pages nobody read (D-91)."""
    report = _report(
        pages=[
            {
                "page": 1,
                "ledger_status": "parsed",
                "skip_reason": None,
                "attachment_id": "attachment-1",
            },
            {
                "page": 2,
                "ledger_status": "parsed",
                "skip_reason": None,
                "attachment_id": "attachment-1",
            },
        ],
        tender={
            "id": TENDER_ID,
            "filename": "tender.pdf",
            "sha256": "a" * 64,
            "page_count": 17,
        },
        attachments=[{"id": "attachment-1", "url": "upload://spec.pdf", "pages": 2}],
    )

    pages = report["tender"]["pages"]
    assert pages["declared"] == 2
    assert pages["unaccounted"] == 0
    assert pages["incomplete_ingestion"] is False


def test_an_extraction_that_stopped_short_is_not_a_final_report():
    """A complete ledger over a tender whose requirements were only half read
    is still an incomplete ingestion.

    Every page parsed, so the ledger looks whole, but the extraction hit the
    provider's limit. A report produced from that tender covers the
    requirements read before the stop, and has to say so (D16).
    """
    report = _report(
        stage={
            "status": "rate_limited",
            "detail": "the model is rate limited",
            "key": "k",
        }
    )

    assert report["tender"]["pages"]["incomplete_ingestion"] is True
    assert report["tender"]["extraction"]["complete"] is False
    assert report["incomplete_ingestion"] is True
    page = render_html(report)
    assert "the extraction of requirements stopped short" in page
    assert "the model is rate limited" in page
    assert "This is not a final compatibility report" in page


def test_a_skipped_page_keeps_its_reason():
    report = _report(
        pages=[
            {"page": 1, "ledger_status": "parsed", "skip_reason": None},
            {
                "page": 2,
                "ledger_status": "skipped",
                "skip_reason": "the page is a duplicate of page 1",
            },
        ]
    )

    assert report["tender"]["pages"]["skipped"] == [
        {"page": 2, "reason": "the page is a duplicate of page 1"}
    ]


def test_the_report_records_the_runs_own_config():
    """FR-34: the policy, models and corpus a report was produced with.

    The run's snapshot, not the settings now: a report that described the
    current settings could describe rules that did not exist when it ran.
    """
    report = _report()

    assert report["config"]["llm_model"] == "the-run-model"
    assert report["run"]["corpus_hash"] == "c" * 64
    assert "the-run-model" in render_html(report)


def test_incomplete_results_are_reported_not_hidden():
    report = _report(
        results=[_result("r1", "mfr-a", "Acme", "full_match", evidence=EVIDENCE)]
    )

    assert report["results"] == {"stored": 1, "expected": 2, "complete": False}
    assert "Partial results" in render_html(report)


def test_the_review_queue_names_every_flagged_result():
    report = _report(
        results=[
            _result(
                "r1",
                "mfr-a",
                "Acme",
                "no_evidence_found",
                score=0.0,
                capability=None,
                review_required=True,
                review_reason="the corpus is silent",
            ),
        ]
    )

    assert report["review_queue"] == [
        {
            "requirement_id": "r1",
            "manufacturer": "Acme",
            "status": "no_evidence_found",
            "reason": "the corpus is silent",
            "reason_code": None,
        }
    ]


def test_the_csv_has_a_row_per_requirement_and_manufacturer():
    report = _report(
        results=[
            _result("r1", "mfr-a", "Acme", "full_match", evidence=EVIDENCE),
            _result("r1", "mfr-b", "Beta", "no_evidence_found", score=0.0, capability=None),
            _result(
                "r2", "mfr-a", "Acme", "unsupported_requirement", score=0.0, capability=None
            ),
        ]
    )

    lines = render_csv(report).strip().splitlines()

    assert lines[0].startswith("requirement_id,item_id,requirement")
    assert len(lines) == 4  # header + three rows
    assert "circor-d-series.pdf" in lines[1]
    # The unsupported clause is exported too, with its status.
    assert "unsupported_requirement" in lines[3]


def test_a_quote_that_looks_like_a_spreadsheet_formula_is_not_executed():
    """Document text is data, never instructions - including in a spreadsheet."""
    report = _report(
        results=[
            _result(
                "r1",
                "mfr-a",
                "Acme",
                "full_match",
                evidence=dict(EVIDENCE, quote="=HYPERLINK(\"http://evil\",\"click\")"),
            )
        ],
        requirements=[
            dict(REQUIREMENTS[0], text="+SUM(1,1)"),
        ],
    )

    text = render_csv(report)

    assert "'+SUM(1,1)" in text
    assert '"\'=HYPERLINK' in text or "'=HYPERLINK" in text
    # The raw formula is never the first character of a cell.
    assert "\n=HYPERLINK" not in text


def test_the_html_shows_document_text_as_text():
    """A tender is data: markup in a quote is escaped, not rendered."""
    report = _report(
        results=[
            _result(
                "r1",
                "mfr-a",
                "Acme",
                "full_match",
                evidence=dict(EVIDENCE, quote="<script>alert('x')</script>"),
            )
        ]
    )

    page = render_html(report)

    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_the_html_explains_every_status():
    page = render_html(_report())

    for status in (
        "full_match",
        "partial_match",
        "missing_capability",
        "unsupported_requirement",
        "no_evidence_found",
    ):
        assert status in page
    # The distinction the whole system rests on is spelled out.
    assert "Neither a pass nor a fail" in page


def _unknown(requirement_id="r3", *, reason=None, page=4):
    return {
        "id": requirement_id,
        "item_id": "1",
        "text": "The bidder shall submit the documents",
        "source_text": "The bidder shall submit the documents",
        "type": "unknown",
        "mandatory": True,
        "page": page,
        "value": None,
        "unit": None,
        "review_required": True,
        "review_reason": reason,
    }


def test_an_unknown_clause_is_listed_as_not_assessed_and_not_as_a_result():
    """A clause nothing could type was not assessed. It is not a pass, not a
    missing capability and not a stored result (PRD 4, D-92)."""
    report = _report(
        requirements=[REQUIREMENTS[0], _unknown()],
        results=[_result("r1", "mfr-a", "Acme", "full_match", evidence=EVIDENCE)],
    )

    assert [row["requirement_id"] for row in report["not_assessed"]] == ["r3"]
    assert [row["requirement_id"] for row in report["requirements"]] == ["r1"]
    # The unknown clause is not an assessment a caller waits for.
    assert report["results"]["expected"] == 1
    assert report["results"]["complete"] is True
    page = render_html(report)
    assert "<h2>Not assessed</h2>" in page
    assert "The bidder shall submit the documents" in page
    assert "not a missing capability and not a pass" in page


def test_the_unknown_line_splits_what_could_not_be_typed():
    """The line accounts for clauses, so a dropped fragment is counted with
    the unknown requirements: it was read and then thrown away (D-92)."""
    report = _report(
        requirements=[
            REQUIREMENTS[0],
            _unknown("r3", reason="page classified as commercial_legal"),
            _unknown("r4"),
        ],
        results=[_result("r1", "mfr-a", "Acme", "full_match", evidence=EVIDENCE)],
        requirements_stage={
            "status": "done",
            "detail": json.dumps({"dropped": 5, "merged": 2}),
        },
    )

    unknown = report["unknown_requirements"]
    # Two stored unknown clauses plus five dropped fragments, over eight
    # clauses read in all.
    assert unknown["unknown"] == 7
    assert unknown["total"] == 8
    assert unknown["percent"] == 87.5
    assert unknown["non_technical_pages"] == 1
    assert unknown["fragments"] == 5
    assert unknown["fragments_merged"] == 2
    assert unknown["unparsed"] == 1

    page = render_html(report)
    assert "Requirements typed unknown:" in page
    assert "7 of 8" in page
    assert "non-technical pages: 1" in page
    assert "fragments: 5" in page
    assert "unparsed: 1" in page
    assert "not counted as missing capabilities" in page


def test_a_report_with_no_unknown_clause_has_no_unknown_banner():
    report = _report()

    assert report["unknown_requirements"]["unknown"] == 0
    assert report["not_assessed"] == []
    page = render_html(report)
    assert "Requirements typed unknown:" not in page
    assert "<h2>Not assessed</h2><p class=\"muted\">None.</p>" in page
