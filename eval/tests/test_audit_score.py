"""Tests for the filled-sheet grader (audit_score.py).

Everything here is synthetic: the requirements, manufacturers, statuses, quotes
and reviewer answers are made up, so these tests prove the join, the refusal and
the arithmetic - nothing about a real catalogue.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import audit_score  # noqa: E402
import audit_sheet  # noqa: E402
from test_harness import make_corpus  # noqa: E402

QUOTED_PAGE = ["SYNTHETIC DATA - Body: ASTM A351 CF8M (SS316)", "SYNTHETIC DATA - page two"]
QUOTE = "Body: ASTM A351 CF8M (SS316)"

# pair id -> (audit_ok, correct_status, notes)
ANSWERS = {
    "P01": ("yes", "", ""),
    "P02": ("yes", "", ""),
    "P03": ("no", "no_evidence_found", "SYNTHETIC: the list says brass"),
    "P04": ("no", "full_match", ""),
}


def _requirement(item_id: str, requirement_id: str) -> dict:
    return {
        "requirement_id": requirement_id,
        "item_id": item_id,
        "item_title": f"SYNTHETIC item {item_id}",
        "requirement": f"SYNTHETIC clause {requirement_id}",
        "category": "valves",
        "tender_sha256": "a" * 64,
    }


def _outcome(**overrides) -> SimpleNamespace:
    values = {
        "status": "no_evidence_found",
        "verification_outcome": "not_checked",
        "applicability": "model_level",
        "confidence": "low",
        "review_required": False,
        "review_reason": None,
        "reason_code": None,
        "score": 0.0,
        "model": "SYNTHETIC Model",
        "page": None,
        "quote": None,
        "document_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# (requirement, manufacturer) -> the published result the reviewer was shown.
PUBLISHED = {
    ("r1", "Acme"): _outcome(
        status="full_match", score=1.0, page=1, quote=QUOTE
    ),
    ("r1", "Beta"): _outcome(
        status="no_evidence_found", review_required=True, review_reason="nothing to check"
    ),
    ("r2", "Acme"): _outcome(
        status="missing_capability",
        page=2,
        quote="SYNTHETIC quote that is not on the page",
    ),
    ("r2", "Beta"): _outcome(status="partial_match", score=0.5, review_required=True),
}


def _sheet(tmp_path: Path, name: str = "synthetic-audit.csv") -> Path:
    """A real sheet, written by the exporter, with the reviewer's columns empty."""
    requirements = [_requirement("10", "r1"), _requirement("11", "r2")]

    def match(requirement, manufacturer):
        return PUBLISHED[(requirement["requirement_id"], manufacturer)]

    rows = audit_sheet.collect(
        requirements,
        ["Acme", "Beta"],
        match=match,
        document_for=lambda outcome: {"document": "synthetic-datasheet.pdf"},
        candidate_cap=2,
    )
    path = tmp_path / name
    audit_sheet.write_sheet(rows, path)
    return path


def _fill(sheet: Path, answers: dict[str, tuple[str, str, str]]) -> Path:
    """Fill the three reviewer columns by pair id, as a person would in Excel."""
    with sheet.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for row in rows:
        verdict, correct_status, notes = answers.get(row["pair"], ("", "", ""))
        row["audit_ok"] = verdict
        row["correct_status"] = correct_status
        row["notes"] = notes
    filled = sheet.with_name(f"{sheet.stem}-filled.csv")
    with filled.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return filled


def test_a_filled_sheet_is_graded_on_the_rows_a_human_checked(tmp_path):
    data, _, _ = make_corpus(tmp_path, QUOTED_PAGE)
    sheet = _sheet(tmp_path)

    summary = audit_score.score(audit_score.load_sheet(_fill(sheet, ANSWERS)), data)

    assert summary["audited"] == 4
    assert summary["accuracy"] == pytest.approx(0.5)
    # P03 is wrong and nobody flagged it; P04 is wrong but was flagged.
    assert summary["false_confident"] == {"count": 1, "rate": pytest.approx(0.25)}
    assert summary["flagged_but_correct"] == 1
    assert summary["confusion"] == {
        "missing_capability": {"no_evidence_found": 1},
        "partial_match": {"full_match": 1},
    }
    # The reviewer's note travels with the row it explains.
    assert summary["wrong"][0]["notes"] == "SYNTHETIC: the list says brass"

    cites = summary["citations"]
    assert cites["claims"] == 3
    assert cites["verified"] == 1
    assert cites["failed"] == 1
    assert cites["uncited"] == 1
    assert cites["correctness"] == pytest.approx(1 / 3, abs=1e-4)


def test_the_sample_is_reported_as_precision_not_as_recall(tmp_path):
    data, _, _ = make_corpus(tmp_path, QUOTED_PAGE)
    summary = audit_score.score(audit_score.load_sheet(_fill(_sheet(tmp_path), ANSWERS)), data)

    assert "not what it missed" in summary["caveat"]
    assert "not what it missed" in audit_score.render_markdown(summary)


def test_a_row_nobody_graded_refuses_the_whole_sheet(tmp_path):
    answers = dict(ANSWERS)
    answers["P02"] = ("", "", "")
    sheet = _fill(_sheet(tmp_path), answers)

    with pytest.raises(audit_score.AuditError) as exc:
        audit_score.load_sheet(sheet)

    assert "P02" in str(exc.value)
    assert "audit_ok" in str(exc.value)


def test_the_rows_are_joined_on_the_pair_id_not_their_order(tmp_path):
    data, _, _ = make_corpus(tmp_path, QUOTED_PAGE)
    sheet = _fill(_sheet(tmp_path), ANSWERS)
    with sheet.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    rows[0]["pair"] = rows[1]["pair"]

    with pytest.raises(audit_score.AuditError) as exc:
        audit_score.load_sheet(_rewrite(sheet, fields, rows))

    assert "was already filled" in str(exc.value)


def test_a_sheet_that_is_not_one_of_these_is_refused(tmp_path):
    path = tmp_path / "someone-elses.csv"
    path.write_text("pair,requirement_id\nP01,r1\n", encoding="utf-8")

    with pytest.raises(audit_score.AuditError) as exc:
        audit_score.load_sheet(path)

    assert "not an audit sheet this tool wrote" in str(exc.value)


def test_a_status_the_vocabulary_does_not_have_is_refused(tmp_path):
    answers = dict(ANSWERS)
    answers["P04"] = ("no", "definitely_wrong", "")
    sheet = _fill(_sheet(tmp_path), answers)

    with pytest.raises(audit_score.AuditError) as exc:
        audit_score.load_sheet(sheet)

    assert "definitely_wrong" in str(exc.value)


def test_the_command_writes_the_summary_and_leaves_the_sheet_untouched(tmp_path, capsys):
    data, _, _ = make_corpus(tmp_path, QUOTED_PAGE)
    sheet = _fill(_sheet(tmp_path), ANSWERS)
    before = sheet.read_bytes()
    out = tmp_path / "results" / "audit.json"

    code = audit_score.main(
        [
            "--sheet",
            str(sheet),
            "--corpus",
            str(data),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    assert out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["audited"] == 4
    assert "Accuracy among audited" in capsys.readouterr().out
    # The reviewer's file is the record: the grader never edits it.
    assert sheet.read_bytes() == before


def _rewrite(path: Path, fields: list[str], rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path
