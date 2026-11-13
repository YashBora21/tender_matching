"""Synthetic cache rows: conflicting readings must not be picked by UUID."""
from types import SimpleNamespace

import pytest

from app.services.match.findings import cached_fact


@pytest.mark.parametrize("changed", [1, 2, 3, 9, 10])
def test_conflicting_readings_miss_cache(changed):
    row = ["f1", "150", None, "categorical", "d1", 1,
           "Synthetic pressure class: 150", "pressure_class", False, "eq", None, "valves", "ball", None]
    other = row.copy()
    other[changed] = "different"
    session = SimpleNamespace(execute=lambda stmt: SimpleNamespace(all=lambda: [row, other]))
    assert cached_fact(session, "synthetic-product", "pressure_class") is None


def test_equivalent_readings_keep_cached_answer():
    row = ["f1", "150", None, "categorical", "d1", 1,
           "Synthetic pressure class: 150", "pressure_class", False, "eq", None, "valves", "ball", None]
    session = SimpleNamespace(execute=lambda stmt: SimpleNamespace(all=lambda: [row, row]))
    assert cached_fact(session, "synthetic-product", "pressure_class")["value"] == "150"


def test_only_a_verified_fact_is_a_cache_hit():
    """A fact waiting for review is not an answer.

    The cache is asked before retrieval, so a `needs_review` row that answered
    from here would be republished as a confirmed match without the verifier
    ever seeing it again. The query itself must ask for `verified` only
    (AGENTS #1, D14).
    """
    captured = {}

    class Recorder:
        def execute(self, statement):
            captured["statement"] = statement
            return SimpleNamespace(all=lambda: [])

    assert cached_fact(Recorder(), "synthetic-product", "pressure_class") is None
    asked = [
        clause.right.value
        for clause in captured["statement"].whereclause.clauses
        if getattr(getattr(clause, "left", None), "key", None) == "verification_status"
    ]
    assert asked == ["verified"]
