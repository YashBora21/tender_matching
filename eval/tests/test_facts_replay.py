"""Tests for the stored-facts spot check (facts_replay.py).

Everything here is synthetic: the requirement, manufacturer, values and quotes
are made up, so these tests prove the row shape and the two guards - that a
pair no fact answers stays silence, and that nothing reaches the provider.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import facts_replay  # noqa: E402
from app.schemas.enums import RequirementStatus  # noqa: E402
from app.services.match.match import MatchResult  # noqa: E402

REASON = "SYNTHETIC: the class on one side is missing"
QUOTE = "SYNTHETIC DATA - Class 150 RF"


def test_a_row_that_asserts_carries_the_quote_and_no_database_ids() -> None:
    match = MatchResult(
        requirement_text="SYNTHETIC: ball valve, class 150",
        manufacturer="acme",
        model="A-100",
        status=RequirementStatus.FULL_MATCH,
        score=1.0,
        quote=QUOTE,
        page=3,
        document_id="11111111-1111-1111-1111-111111111111",
    )

    row = facts_replay._row(
        "acme",
        match,
        {"document": "acme-a100.pdf", "sha256": "ab" * 32},
    )

    assert row["status"] == "full_match"
    assert row["evidence"] == {
        "page": 3,
        "quote": QUOTE,
        "document": "acme-a100.pdf",
        "sha256": "ab" * 32,
    }
    assert "fact_id" not in row["evidence"]
    assert "document_id" not in row["evidence"]


def test_a_pair_no_fact_answers_is_flagged_silence() -> None:
    """Absence of a stored fact is never a verdict about the product."""
    match = facts_replay._silence("SYNTHETIC: what is the class", "acme", REASON)

    assert match.status.value == "no_evidence_found"
    assert match.review_required is True
    assert match.review_reason == REASON
    assert facts_replay._row("acme", match, None).get("evidence") is None


def test_the_client_the_replay_hands_the_matcher_refuses_to_read() -> None:
    with pytest.raises(facts_replay.ReplayError):
        facts_replay._NoModel().complete_json("system", "user")
