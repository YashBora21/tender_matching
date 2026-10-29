"""Tests for the recall@k harness.

Every label and document here is synthetic and says so: the tests prove the
arithmetic, the refusals and the four buckets, not anything about a
manufacturer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import recall  # noqa: E402

HEADER = (
    "requirement_id,requirement,manufacturer,item_id,item_title,"
    "gold_document,gold_page,true_status\n"
)


def _label(
    requirement_id: str = "r1",
    *,
    gold_document: str = "gold.pdf",
    gold_page: int = 3,
    true_status: str | None = None,
) -> recall.RecallLabel:
    return recall.RecallLabel(
        requirement_id=requirement_id,
        requirement="SYNTHETIC body shall be SS316",
        manufacturer="Synthetic Manufacturer",
        gold_document=gold_document,
        gold_page=gold_page,
        item_title="SYNTHETIC GATE VALVE",
        true_status=true_status,
    )


class FakeRanking:
    """A corpus that answers from dictionaries, with no database."""

    def __init__(self, ranked=None, pages=None, statuses=None) -> None:
        self.ranked = ranked or {}
        self._pages = pages or {}
        self.statuses = statuses or {}

    def rank(self, label, limit):
        return list(self.ranked.get(label.key, []))[:limit]

    def pages(self, document):
        return self._pages.get(recall.document_key(document))

    def result_status(self, label):
        return self.statuses.get(label.key)


def test_an_empty_label_file_is_refused(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises(recall.RecallError, match="no rows"):
        recall.load_labels(path)


def test_a_file_with_no_gold_page_is_refused(tmp_path):
    """The gold slice that prompted this module had rows but no labels. A
    recall of 0 over it would be a claim nobody made (D-98)."""
    path = tmp_path / "labels.csv"
    path.write_text(
        HEADER
        + "r1,SYNTHETIC body shall be SS316,Synthetic Manufacturer,1,"
        "SYNTHETIC GATE VALVE,,,\n",
        encoding="utf-8",
    )

    with pytest.raises(recall.RecallError, match="nothing to measure"):
        recall.load_labels(path)


def test_a_row_without_a_gold_page_is_unlabelled_not_a_miss(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text(
        HEADER
        + "r1,SYNTHETIC body shall be SS316,Synthetic Manufacturer,1,"
        "SYNTHETIC GATE VALVE,gold.pdf,3,full_match\n"
        + "r2,SYNTHETIC trim shall be 13Cr,Synthetic Manufacturer,1,"
        "SYNTHETIC GATE VALVE,,,\n",
        encoding="utf-8",
    )

    labels, unlabelled = recall.load_labels(path)

    assert [label.requirement_id for label in labels] == ["r1"]
    assert unlabelled == 1


def test_a_half_labelled_row_is_an_error_not_a_silent_drop(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text(
        HEADER
        + "r1,SYNTHETIC body shall be SS316,Synthetic Manufacturer,1,"
        "SYNTHETIC GATE VALVE,gold.pdf,,\n",
        encoding="utf-8",
    )

    with pytest.raises(recall.RecallError, match="both gold_document"):
        recall.load_labels(path)


def test_the_jsonl_form_is_read(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        json.dumps(
            {
                "requirement_id": "r1",
                "requirement": "SYNTHETIC body shall be SS316",
                "manufacturer": "Synthetic Manufacturer",
                "gold_document": "gold.pdf",
                "gold_page": 3,
            }
        ),
        encoding="utf-8",
    )

    labels, unlabelled = recall.load_labels(path)

    assert len(labels) == 1
    assert labels[0].gold_page == 3
    assert unlabelled == 0


def test_recall_at_three_and_twenty():
    """One label is first, one is tenth, one is absent: @3 is one third,
    @20 is two thirds."""
    first = _label("r1")
    tenth = _label("r2")
    absent = _label("r3")
    ranking = FakeRanking(
        ranked={
            first.key: [recall.Ranked("gold", 3)] + [
                recall.Ranked("other", page) for page in range(1, 5)
            ],
            tenth.key: [
                recall.Ranked("other", page) for page in range(1, 10)
            ]
            + [recall.Ranked("gold", 3)],
            absent.key: [recall.Ranked("other", page) for page in range(1, 25)],
        },
        pages={
            "gold": {3},
            "other": set(range(1, 30)),
        },
    )

    report = recall.evaluate([first, tenth, absent], ranking)

    assert report.hits(3) == 1
    assert report.hits(20) == 2
    assert report.recall(3) == pytest.approx(1 / 3)
    assert report.recall(20) == pytest.approx(2 / 3)


def test_a_document_that_is_not_in_the_corpus_is_a_corpus_gap():
    label = _label(gold_document="absent.pdf")
    ranking = FakeRanking(ranked={label.key: []}, pages={})

    report = recall.evaluate([label], ranking)

    assert report.rows[0].cause == recall.CORPUS_GAP
    assert "not in the corpus" in report.rows[0].detail


def test_a_page_the_reader_lost_is_a_chunking_miss():
    label = _label(gold_page=7)
    ranking = FakeRanking(ranked={label.key: []}, pages={"gold": {1, 2}})

    report = recall.evaluate([label], ranking)

    assert report.rows[0].cause == recall.CHUNKING
    assert "page 7" in report.rows[0].detail


def test_an_indexed_page_outside_the_cut_off_is_a_retrieval_miss():
    label = _label(gold_page=3)
    ranking = FakeRanking(
        ranked={
            label.key: [recall.Ranked("other", page) for page in range(1, 25)]
        },
        pages={"gold": {3}, "other": set(range(1, 25))},
    )

    report = recall.evaluate([label], ranking)

    assert report.rows[0].cause == recall.RETRIEVAL


def test_a_retrieved_page_with_the_wrong_status_is_a_verifier_miss():
    """Retrieval did its job; the answer downstream is still wrong."""
    label = _label(true_status="full_match")
    ranking = FakeRanking(
        ranked={label.key: [recall.Ranked("gold", 3)]},
        pages={"gold": {3}},
        statuses={label.key: "no_evidence_found"},
    )

    report = recall.evaluate([label], ranking)

    assert report.rows[0].hit_at[3] is True
    assert report.rows[0].cause == recall.VERIFIER
    assert "no_evidence_found" in report.rows[0].detail


def test_a_retrieved_page_with_the_right_status_is_not_a_miss():
    label = _label(true_status="full_match")
    ranking = FakeRanking(
        ranked={label.key: [recall.Ranked("gold", 3)]},
        pages={"gold": {3}},
        statuses={label.key: "full_match"},
    )

    report = recall.evaluate([label], ranking)

    assert report.rows[0].cause is None
    assert report.misses() == []


def test_a_document_name_is_compared_by_its_stem():
    """The label may write a path with an extension; a candidate carries the
    stem of its own path."""
    label = _label(gold_document="docs/Gold-Datasheet.pdf")
    ranking = FakeRanking(
        ranked={label.key: [recall.Ranked("Gold-Datasheet", 3)]},
        pages={"gold datasheet": {3}},
    )

    report = recall.evaluate([label], ranking)

    assert report.rows[0].hit_at[3] is True


def test_the_cause_table_counts_every_miss():
    retrieval = _label("r1", gold_document="indexed.pdf")
    chunking = _label("r2", gold_document="empty.pdf")
    gap = _label("r3", gold_document="absent.pdf")
    ranking = FakeRanking(
        ranked={label.key: [] for label in (retrieval, chunking, gap)},
        pages={"indexed": {3}, "empty": set()},
    )

    report = recall.evaluate([retrieval, chunking, gap], ranking)

    causes = report.causes()
    assert causes[recall.RETRIEVAL] == 1
    assert causes[recall.CHUNKING] == 1
    assert causes[recall.CORPUS_GAP] == 1
    assert causes[recall.VERIFIER] == 0
    assert len(report.misses()) == 3
