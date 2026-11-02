"""Type-driven requirement resolution (FR-15, FR-20, FR-27, FR-29).

The LLM extracts the attribute, the operator, the value and the unit. This
tests that the attribute resolves through the alias table, that the value
type selects the evaluator, and that an attribute that does not resolve goes
to review rather than to a guess.
"""

from __future__ import annotations

import pytest

from app.comparison.registry import evaluate
from app.repositories.aliases import AliasRepo, seed_aliases
from app.schemas.enums import Operator, ValueType
from app.services.requirement.resolve import resolve


class FakeAliasRepo(AliasRepo):
    """An alias repo backed by a dict instead of the database."""

    def __init__(self, session, rows: dict[str, tuple[str, float, bool]] | None = None):
        super().__init__(session)
        self.rows = rows or {}

    def _lookup(self, name):
        return self.rows.get(name)


class EmptySession:
    """A session that answers every query with no rows.

    Enough for the real alias repo to run against: an alias that is not in
    the table is not found.
    """

    def execute(self, statement, *args, **kwargs):
        class Result:
            def first(self):
                return None

            def all(self):
                return []

        return Result()


def _session(rows=None):
    """A session the fake alias repo accepts."""
    return EmptySession()


def _resolve(session, attribute, **kwargs):
    defaults = dict(
        operator=Operator.GTE,
        value="1500",
        unit="rpm",
        text_value=None,
        value_range=None,
        mandatory=True,
    )
    defaults.update(kwargs)
    return resolve(attribute=attribute, session=session, **defaults)


def test_a_canonical_attribute_resolves_to_itself():
    resolved = _resolve(_session(), "rotational_speed")
    assert resolved.canonical_term == "rotational_speed"
    assert resolved.value_type is ValueType.NUMERIC
    assert resolved.needs_review is False


def test_an_alias_resolves_to_its_canonical_term(monkeypatch):
    monkeypatch.setattr(
        "app.services.requirement.resolve.AliasRepo",
        lambda session: FakeAliasRepo(session, {"spindle speed": ("rotational_speed", 0.9, True)}),
    )
    resolved = _resolve(_session(), "spindle speed")
    assert resolved.canonical_term == "rotational_speed"
    assert resolved.value_type is ValueType.NUMERIC
    assert resolved.needs_review is False


def test_an_unknown_attribute_goes_to_review():
    """FR-29: an unknown attribute is never a guess."""
    resolved = _resolve(_session(), "wibble factor")
    assert resolved.needs_review is True
    assert "wibble factor" in (resolved.review_reason or "")
    assert resolved.value_type is None


def test_a_low_confidence_alias_goes_to_review(monkeypatch):
    monkeypatch.setenv("ALIAS_CONFIDENCE_THRESHOLD", "0.95")
    monkeypatch.setattr(
        "app.services.requirement.resolve.AliasRepo",
        lambda session: FakeAliasRepo(session, {"spindle speed": ("rotational_speed", 0.9, True)}),
    )
    resolved = _resolve(_session(), "spindle speed")
    assert resolved.needs_review is True
    assert "confidence" in (resolved.review_reason or "")


def test_an_unapproved_alias_goes_to_review(monkeypatch):
    monkeypatch.setattr(
        "app.services.requirement.resolve.AliasRepo",
        lambda session: FakeAliasRepo(session, {"spindle speed": ("rotational_speed", 0.99, False)}),
    )
    resolved = _resolve(_session(), "spindle speed")
    assert resolved.needs_review is True
    assert "approved" in (resolved.review_reason or "")


def test_the_value_type_selects_the_evaluator():
    """The evaluator is chosen by the shape of the value, not by the category
    of the product."""
    from app.comparison.registry import REGISTRY

    numeric = _resolve(_session(), "rotational_speed")
    categorical = _resolve(_session(), "actuation", value=None, text_value="pneumatic")
    material = _resolve(_session(), "material_grade", value=None, text_value="SS316")

    assert REGISTRY[numeric.value_type] is not None
    assert REGISTRY[categorical.value_type] is not None
    assert REGISTRY[material.value_type] is not None
    assert numeric.value_type is not categorical.value_type


def test_a_resolved_requirement_compares():
    """A resolved requirement runs through the registry."""
    from app.comparison.registry import ComparisonRequest

    resolved = _resolve(_session(), "rotational_speed", operator=Operator.GTE, value="1500", unit="rpm")
    request = ComparisonRequest(
        requirement_value=resolved.value,
        requirement_operator=resolved.operator,
        requirement_unit=resolved.unit,
        requirement_text_value=None,
        requirement_range=None,
        capability_value="1500",
        capability_unit="rpm",
        capability_text_value=None,
        capability_range=None,
    )
    evaluation = evaluate(request, resolved.value_type)
    assert evaluation.status.value == "full_match"


def test_the_alias_table_seeds_from_the_vocabulary(monkeypatch):
    """The table starts minimal: the synonyms the vocabulary ships."""
    from app.comparison.vocabulary import load_vocabulary

    added: list[tuple[str, str]] = []

    class RecordingRepo(AliasRepo):
        def list_all(self):
            return []

        def add(self, canonical_term, alias, **kwargs):
            added.append((canonical_term, alias))

    monkeypatch.setattr("app.repositories.aliases.AliasRepo", RecordingRepo)
    seed_aliases(_session())

    expected = {
        synonym for spec in load_vocabulary().attributes for synonym in spec.synonyms
    }
    assert {alias for _, alias in added} == expected


def test_seeding_does_not_overwrite_a_reviewers_decision(monkeypatch):
    """A synonym that is already there is left alone, so a reviewer's
    decision about a row survives a re-seed."""
    added: list[str] = []

    class RecordingRepo(AliasRepo):
        def list_all(self):
            return [
                {
                    "alias": "speed",
                    "canonical_term": "rotational_speed",
                    "approved": True,
                    "source": "manual",
                }
            ]

        def add(self, canonical_term, alias, **kwargs):
            added.append(alias)

        def set_approval(self, alias, approved):
            added.append(alias)

    monkeypatch.setattr("app.repositories.aliases.AliasRepo", RecordingRepo)
    seed_aliases(_session())

    # "speed" was already there, so it was not re-added. The other synonyms
    # were.
    assert "speed" not in added
    assert "rotational speed" in added
