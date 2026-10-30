"""Tests for the fixed-seed audit sheet (step 5).

Everything here is synthetic: the requirements, manufacturers and results are
made up, so the tests prove the sampling, the refusal and the resumable limit,
not anything about a real catalogue.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import audit_sheet  # noqa: E402


def _requirement(item_id: str, requirement_id: str) -> dict:
    return {
        "requirement_id": requirement_id,
        "item_id": item_id,
        "item_title": f"SYNTHETIC item {item_id}",
        "requirement": f"SYNTHETIC clause {requirement_id}",
        "category": "valves",
        "tender_sha256": "a" * 64,
    }


def _result(**overrides) -> SimpleNamespace:
    values = {
        "status": "no_evidence_found",
        "verification_outcome": "not_checked",
        "applicability": "model_level",
        "confidence": "low",
        "review_required": False,
        "review_reason": None,
        "reason_code": None,
        "score": 0.0,
        "model": "Synthetic Model",
        "page": 3,
        "quote": "SYNTHETIC quote",
        "document_id": "00000000-0000-0000-0000-000000000001",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rows(count: int) -> list[dict]:
    requirements = [_requirement("10", "r1"), _requirement("11", "r2")]
    manufacturers = [f"Synthetic {index:02d}" for index in range(count // 2)]
    return audit_sheet.collect(
        requirements,
        manufacturers,
        match=lambda requirement, name: _result(),
        document_for=lambda outcome: {"document": "synthetic.pdf"},
        candidate_cap=3,
    )


def test_the_same_seed_picks_the_same_rows():
    rows = _rows(22)

    first = audit_sheet.sample(rows, 15, seed=7)
    second = audit_sheet.sample(list(reversed(rows)), 15, seed=7)

    assert [row["requirement_id"] + row["manufacturer"] for row in first] == [
        row["requirement_id"] + row["manufacturer"] for row in second
    ]


def test_a_different_seed_can_pick_a_different_set():
    rows = _rows(22)
    seen = {
        tuple(sorted(row["manufacturer"] for row in audit_sheet.sample(rows, 15, seed)))
        for seed in range(10)
    }

    assert len(seen) > 1


def test_a_sample_larger_than_the_rows_keeps_every_row():
    rows = _rows(6)

    picked = audit_sheet.sample(rows, 15, seed=1)

    assert len(picked) == 6


def test_a_sample_size_below_one_is_refused():
    with pytest.raises(audit_sheet.AuditSheetError):
        audit_sheet.sample(_rows(2), 0, seed=1)


def test_the_sheet_leaves_the_reviewer_columns_empty(tmp_path):
    path = tmp_path / "sheet.csv"
    audit_sheet.write_sheet(audit_sheet.sample(_rows(6), 6, seed=1), path)

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 6
    assert [row["pair"] for row in rows] == [f"P{index:02d}" for index in range(1, 7)]
    for row in rows:
        assert row["audit_ok"] == ""
        assert row["correct_status"] == ""
        assert row["notes"] == ""
        assert row["status"]
        assert row["quote"] == "SYNTHETIC quote"
        assert row["document"] == "synthetic.pdf"


def test_a_rate_limit_keeps_the_rows_already_found_and_resumes(tmp_path):
    from app.services.llm import LLMRateLimited

    requirements = [_requirement("10", "r1"), _requirement("11", "r2")]
    manufacturers = ["Synthetic A", "Synthetic B"]
    checkpoint = tmp_path / "sheet.checkpoint.json"
    calls: list[str] = []

    def limited(requirement, name):
        calls.append(name)
        if len(calls) == 3:
            raise LLMRateLimited("the model is rate limited", retry_after=24.9)
        return _result()

    with pytest.raises(audit_sheet.RateLimited) as caught:
        audit_sheet.collect(
            requirements,
            manufacturers,
            match=limited,
            document_for=lambda outcome: {"document": "synthetic.pdf"},
            candidate_cap=3,
            checkpoint=checkpoint,
        )

    assert caught.value.completed == 2
    assert caught.value.expected == 4
    saved = audit_sheet._load_checkpoint(checkpoint)
    assert len(saved) == 2

    # The second attempt asks only the two pairs the first one did not answer.
    asked: list[str] = []

    def resumed(requirement, name):
        asked.append(name)
        return _result()

    rows = audit_sheet.collect(
        requirements,
        manufacturers,
        match=resumed,
        document_for=lambda outcome: {"document": "synthetic.pdf"},
        candidate_cap=3,
        checkpoint=checkpoint,
    )

    assert len(rows) == 4
    assert len(asked) == 2
