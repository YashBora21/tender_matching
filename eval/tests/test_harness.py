"""Tests for the offline evaluation harness.

The reports, labels and PDFs here are synthetic and say so: they exist to prove
the arithmetic and the refusals, not to say anything about a manufacturer.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import harness  # noqa: E402

TENDER_SHA = "a" * 64


def build_report(rows: list[dict], sha: str = TENDER_SHA) -> dict:
    supported: dict[str, list[dict]] = {}
    unsupported: dict[str, list[dict]] = {}
    for row in rows:
        target = unsupported if row["status"] == harness.UNSUPPORTED else supported
        target.setdefault(row["requirement_id"], []).append(
            {
                "manufacturer": row["manufacturer"],
                "status": row["status"],
                "score": row.get("score"),
                "review_required": row.get("review_required", False),
                "review_reason": row.get("review_reason"),
                "evidence": row.get("evidence"),
            }
        )

    def as_requirements(mapping: dict[str, list[dict]]) -> list[dict]:
        return [
            {
                "requirement_id": requirement_id,
                "text": "SYNTHETIC requirement",
                "results": results,
            }
            for requirement_id, results in mapping.items()
        ]

    return {
        "run": {"id": "run-1", "status": "done"},
        "tender": {"sha256": sha, "filename": "synthetic-tender.pdf"},
        "requirements": as_requirements(supported),
        "unsupported_requirements": as_requirements(unsupported),
    }


def label(
    requirement_id: str,
    manufacturer: str,
    status: str,
    document: str = "synthetic-datasheet.pdf",
    page: int = 1,
    quote: str = "Body: ASTM A351 CF8M",
    note: str | None = None,
) -> harness.GoldLabel:
    if status in harness.CLAIMS:
        return harness.GoldLabel(
            TENDER_SHA,
            requirement_id,
            manufacturer,
            status,
            document=document,
            page=page,
            quote=quote,
        )
    return harness.GoldLabel(
        TENDER_SHA,
        requirement_id,
        manufacturer,
        status,
        note=note or "looked and found nothing that states it",
    )


def gold_row(**overrides) -> dict:
    row = {
        "tender_sha256": TENDER_SHA,
        "requirement_id": "r1",
        "manufacturer": "Acme",
        "status": "full_match",
        "document": "synthetic-datasheet.pdf",
        "page": 1,
        "quote": "Body: ASTM A351 CF8M",
    }
    row.update(overrides)
    return row


def write_gold(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return path


# --- metrics ---------------------------------------------------------------


def test_accuracy_confusion_and_false_confident_rate(tmp_path):
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "full_match",
            },
            {
                "requirement_id": "r1",
                "manufacturer": "Beta",
                "status": "no_evidence_found",
                "review_required": True,
            },
            {
                "requirement_id": "r2",
                "manufacturer": "Acme",
                "status": "full_match",
            },
            {
                "requirement_id": "r2",
                "manufacturer": "Beta",
                "status": "full_match",
            },
        ]
    )
    labels = [
        label("r1", "Acme", "full_match"),
        label("r1", "Beta", "partial_match"),
        label("r2", "Acme", "no_evidence_found"),
        label("r2", "Beta", "full_match"),
    ]

    summary = harness.score(report, labels, tmp_path)

    # 2 of 4 verifiable pairs are right.
    assert summary["accuracy"] == pytest.approx(0.5)
    assert summary["confusion"]["full_match"] == {"full_match": 2}
    assert summary["confusion"]["no_evidence_found"] == {"full_match": 1}
    assert summary["confusion"]["partial_match"] == {"no_evidence_found": 1}
    # One wrong pair was not flagged: r2/Acme was reported as a full match while
    # the corpus is silent, which is exactly the error the metric exists for.
    assert summary["false_confident"] == {"count": 1, "rate": pytest.approx(0.25)}
    # Coverage counts the three pairs where evidence was produced.
    assert summary["coverage"]["with_evidence"] == 3
    assert summary["coverage"]["rate"] == pytest.approx(0.75)


def test_per_status_precision_and_recall(tmp_path):
    report = build_report(
        [
            {"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"},
            {"requirement_id": "r2", "manufacturer": "Acme", "status": "full_match"},
            {"requirement_id": "r3", "manufacturer": "Acme", "status": "full_match"},
        ]
    )
    labels = [
        label("r1", "Acme", "full_match"),
        label("r2", "Acme", "full_match"),
        label("r3", "Acme", "no_evidence_found"),
    ]

    summary = harness.score(report, labels, tmp_path)

    full = summary["per_status"]["full_match"]
    assert full["support"] == 2
    assert full["precision"] == pytest.approx(2 / 3, rel=1e-3)
    assert full["recall"] == pytest.approx(1.0)
    assert full["f1"] == pytest.approx(0.8)
    # A status that is never predicted has no precision, not a precision of 1.
    assert summary["per_status"]["partial_match"]["precision"] is None


def test_unsupported_labels_are_reported_apart_from_the_score(tmp_path):
    report = build_report(
        [
            {"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"},
            {
                "requirement_id": "r2",
                "manufacturer": "Acme",
                "status": "unsupported_requirement",
            },
        ]
    )
    labels = [
        label("r1", "Acme", "full_match"),
        label("r2", "Acme", harness.UNSUPPORTED),
    ]

    summary = harness.score(report, labels, tmp_path)

    assert summary["verifiable"] == 1
    assert summary["unsupported"] == 1
    assert summary["accuracy"] == pytest.approx(1.0)
    assert summary["unsupported_block"] == {
        "labelled": 1,
        "predicted_unsupported": 1,
        "accuracy": pytest.approx(1.0),
    }
    assert summary["wrong"] == []


def test_a_prediction_without_a_label_is_unlabelled_not_correct(tmp_path):
    report = build_report(
        [
            {"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"},
            {"requirement_id": "r2", "manufacturer": "Acme", "status": "full_match"},
        ]
    )

    summary = harness.score(report, [label("r1", "Acme", "full_match")], tmp_path)

    assert summary["labelled"] == 1
    assert summary["unlabelled_predictions"] == 1
    assert summary["accuracy"] == pytest.approx(1.0)


def test_a_label_the_report_does_not_contain_is_refused(tmp_path):
    report = build_report(
        [{"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"}]
    )

    with pytest.raises(harness.EvalError, match="does not contain"):
        harness.score(report, [label("r9", "Nobody", "full_match")], tmp_path)


def test_a_report_without_a_tender_hash_cannot_be_scored(tmp_path):
    report = build_report(
        [{"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"}],
        sha="",
    )

    with pytest.raises(harness.EvalError, match="sha256"):
        harness.score(report, [label("r1", "Acme", "full_match")], tmp_path)


# --- the gold file ---------------------------------------------------------


def test_a_valid_gold_file_loads_every_status(tmp_path):
    path = write_gold(
        tmp_path / "gold.jsonl",
        [
            gold_row(),
            gold_row(
                requirement_id="r2",
                status="no_evidence_found",
                document=None,
                page=None,
                quote=None,
                note="the catalogue has no end-connection table",
            ),
            gold_row(
                requirement_id="r3",
                status="unsupported_requirement",
                document=None,
                page=None,
                quote=None,
                note="warranty is commercial, not technical",
            ),
        ],
    )

    labels = harness.load_gold(path)

    assert [row.status for row in labels] == [
        "full_match",
        "no_evidence_found",
        "unsupported_requirement",
    ]


def test_a_gold_file_is_refused_for_the_reasons_that_make_it_uncheckable(tmp_path):
    path = write_gold(
        tmp_path / "gold.jsonl",
        [
            gold_row(),
            gold_row(),  # duplicate key
            gold_row(requirement_id="r2", quote=None),  # a claim without a quote
            gold_row(requirement_id="r3", status="probably_fine"),
            gold_row(
                requirement_id="r4",
                status="no_evidence_found",
                document=None,
                page=None,
                quote=None,
            ),  # silence with no note
        ],
    )

    with pytest.raises(harness.EvalError) as raised:
        harness.load_gold(path)

    message = str(raised.value)
    assert "duplicate label" in message
    assert "needs the quote" in message
    assert "not one of" in message
    assert "needs a note" in message


def test_a_gold_file_that_is_not_json_names_the_line(tmp_path):
    path = tmp_path / "gold.jsonl"
    path.write_text('{"tender_sha256": "not json"\n', encoding="utf-8")

    with pytest.raises(harness.EvalError, match="line 1"):
        harness.load_gold(path)


def test_check_mode_reports_a_good_file_and_refuses_a_bad_one(tmp_path, capsys):
    good = write_gold(tmp_path / "good.jsonl", [gold_row()])
    bad = write_gold(tmp_path / "bad.jsonl", [gold_row(quote=None)])

    assert harness.main(["check", "--gold", str(good)]) == 0
    assert "1 valid label" in capsys.readouterr().out

    assert harness.main(["check", "--gold", str(bad)]) == 1
    assert "needs the quote" in capsys.readouterr().err


def test_score_mode_reads_files_and_writes_the_json_summary(tmp_path, capsys):
    report = build_report(
        [{"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"}]
    )
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    gold_path = write_gold(tmp_path / "gold.jsonl", [gold_row()])
    out = tmp_path / "summary.json"

    code = harness.main(
        [
            "score",
            "--report",
            str(report_path),
            "--gold",
            str(gold_path),
            "--corpus",
            str(tmp_path / "data"),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert "Status accuracy" in capsys.readouterr().out
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["labelled"] == 1
    assert summary["accuracy"] == pytest.approx(1.0)


# --- citations -------------------------------------------------------------


def make_corpus(tmp_path: Path, pages: list[str]) -> tuple[Path, str, str]:
    """A one-document corpus on disk, so a citation can really be checked."""
    pymupdf = pytest.importorskip("pymupdf")
    data = tmp_path / "data"
    raw = data / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    pdf = raw / "synthetic-datasheet.pdf"
    document = pymupdf.open()
    for text in pages:
        document.new_page().insert_text((72, 90), text)
    document.save(pdf)
    document.close()

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (raw.parent / "manifest.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "filename": pdf.name,
                        "path": f"raw/{pdf.name}",
                        "sha256": digest,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return data, digest, pdf.name


def test_a_quote_on_the_cited_page_is_verified(tmp_path):
    data, digest, name = make_corpus(
        tmp_path, ["SYNTHETIC DATA - Body: ASTM A351 CF8M (SS316)"]
    )
    evidence = {
        "document": name,
        "page": 1,
        "quote": "Body: ASTM A351 CF8M (SS316)",
        "sha256": digest,
    }
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "full_match",
                "evidence": evidence,
            }
        ]
    )
    labels = [
        label(
            "r1",
            "Acme",
            "full_match",
            quote="Body: ASTM A351 CF8M (SS316)",
        )
    ]

    summary = harness.score(report, labels, data)

    assert summary["citations"]["verified"] == 1
    assert summary["citations"]["correctness"] == pytest.approx(1.0)


def test_a_quote_that_is_not_on_the_cited_page_fails(tmp_path):
    data, digest, name = make_corpus(
        tmp_path,
        ["SYNTHETIC DATA - Body: ASTM A351 CF8M (SS316)", "SYNTHETIC DATA - page two"],
    )
    # The quote is real, the page is wrong: a citation that points at the wrong
    # place is not a correct citation.
    evidence = {
        "document": name,
        "page": 2,
        "quote": "Body: ASTM A351 CF8M (SS316)",
        "sha256": digest,
    }
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "full_match",
                "evidence": evidence,
            }
        ]
    )

    summary = harness.score(
        report,
        [label("r1", "Acme", "full_match", quote="Body: ASTM A351 CF8M (SS316)")],
        data,
    )

    assert summary["citations"]["failed"] == 1
    assert summary["citations"]["correctness"] == pytest.approx(0.0)
    assert summary["citations"]["checks"][0]["outcome"] == "failed"


def test_a_claim_without_a_quote_is_uncited_not_verified(tmp_path):
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "full_match",
                "evidence": {"document": "synthetic-datasheet.pdf", "page": 1},
            }
        ]
    )

    summary = harness.score(
        report, [label("r1", "Acme", "full_match")], tmp_path / "empty"
    )

    assert summary["citations"]["uncited"] == 1
    assert summary["citations"]["correctness"] == pytest.approx(0.0)


def test_a_citation_whose_document_is_not_on_disk_is_excluded_not_scored(tmp_path):
    evidence = {
        "document": "absent.pdf",
        "page": 1,
        "quote": "Body: ASTM A351 CF8M",
        "sha256": "b" * 64,
    }
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "full_match",
                "evidence": evidence,
            }
        ]
    )

    summary = harness.score(
        report, [label("r1", "Acme", "full_match")], tmp_path / "empty"
    )

    assert summary["citations"]["unavailable"] == 1
    assert summary["citations"]["correctness"] is None
    # It is still a claim someone has to look at, so it is listed.
    assert summary["citations"]["checks"][0]["outcome"] == "unavailable"


def test_only_labelled_predictions_are_citation_checked(tmp_path):
    evidence = {
        "document": "absent.pdf",
        "page": 1,
        "quote": "Body: ASTM A351 CF8M",
        "sha256": "b" * 64,
    }
    report = build_report(
        [
            {
                "requirement_id": "r1",
                "manufacturer": "Acme",
                "status": "no_evidence_found",
            },
            {
                "requirement_id": "r2",
                "manufacturer": "Acme",
                "status": "full_match",
                "evidence": evidence,
            },
        ]
    )

    summary = harness.score(
        report,
        [label("r1", "Acme", "no_evidence_found")],
        tmp_path / "empty",
    )

    assert summary["citations"]["claims"] == 0
    assert summary["unlabelled_predictions"] == 1


# --- rendering -------------------------------------------------------------


def test_the_markdown_says_what_the_numbers_are(tmp_path):
    labels = [
        label("r1", "Acme", "full_match"),
        label("r2", "Acme", "full_match"),
        label("r3", "Acme", harness.UNSUPPORTED),
    ]
    report = build_report(
        [
            {"requirement_id": "r1", "manufacturer": "Acme", "status": "full_match"},
            {
                "requirement_id": "r2",
                "manufacturer": "Acme",
                "status": "no_evidence_found",
            },
            {
                "requirement_id": "r3",
                "manufacturer": "Acme",
                "status": "unsupported_requirement",
            },
        ]
    )

    text = harness.render_markdown(harness.score(report, labels, tmp_path))

    assert "Status accuracy" in text
    assert "False-confident rate" in text
    assert "Citation correctness" in text
    assert "Unsupported (excluded from the metrics above)" in text
    assert "no_evidence_found" in text


def test_freeze_checks_every_claim_and_drops_the_database_ids(tmp_path):
    """The baseline is re-checked against the PDF, not against a chunk id.

    A report cites `fact_id` and `document_id`, and a re-ingest regenerates
    both, so a frozen baseline that names them cannot be diffed later. The
    citation key is the document hash, the page and the words.
    """
    data, digest, name = make_corpus(
        tmp_path, ["SYNTHETIC DATA - Body: ASTM A351 CF8M", "second page"]
    )

    def evidence(quote, page, **extra):
        return {"sha256": digest, "document": name, "page": page, "quote": quote, **extra}

    report = {
        "requirements": [
            {"results": [{"evidence": evidence("Body: ASTM A351 CF8M", 1, fact_id="f1", document_id="d1")}]},
            # The same words on the same page, cited by another requirement.
            {"results": [{"evidence": evidence("Body: ASTM A351 CF8M", 1, fact_id="f2")}]},
            {"results": [{"evidence": evidence("a quote that is not there", 3)}]},
            # A pair that names no quote is not a claim, so it is not counted.
            {"results": [{"evidence": evidence("", 1)}]},
        ]
    }

    frozen = harness.freeze(report, data)

    assert frozen["citations"]["claims"] == 2
    assert frozen["citations"]["verified"] == 1
    assert frozen["citations"]["failed"] == 1
    assert frozen["citations"]["correctness"] == pytest.approx(0.5)
    assert frozen["citations"]["checks"][0]["predictions"] == 2
    for requirement in frozen["report"]["requirements"]:
        for result in requirement["results"]:
            assert not set(result["evidence"]) & set(harness.TRANSIENT)


def test_a_document_listed_only_in_the_catalogue_csv_is_still_indexed(tmp_path):
    """Two writers fill the corpus: the collector's manifests and the
    hand-gathered catalogue's one CSV. An index that reads only the first calls
    the second's claims "source unavailable" and measures less than the report
    claimed - the excluded rows are invisible in the correctness number.
    """
    pymupdf = pytest.importorskip("pymupdf")
    catalogue = tmp_path / "catalogue" / "delval"
    catalogue.mkdir(parents=True)
    pdf = catalogue / "series-7-8.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((72, 90), "SYNTHETIC - Body Style flanged")
    document.save(pdf)
    document.close()
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (tmp_path / "catalogue" / "manifest.csv").write_text(
        'manufacturer,filename,sha256\ndelval,"series-7-8.pdf",' + digest + "\n",
        encoding="utf-8",
    )

    index = harness.corpus_index(tmp_path)

    assert index[("sha256", digest)] == pdf
    assert index[("filename", "series-7-8.pdf")] == pdf
