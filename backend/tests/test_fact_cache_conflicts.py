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
