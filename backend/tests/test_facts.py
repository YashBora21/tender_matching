"""Fact extraction: strict JSON, and a quote that can be checked."""

from __future__ import annotations

import pytest

from app.services.facts import ExtractedFact, extract_fact


class FakeClient:
    """A client that answers with what it is given."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_fact_is_read_from_a_passage():
    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "SS316",
                    "unit": None,
                    "operator": "eq",
                    "model": "Series 75/76",
                    "quote": "Body of body shall be SS316",
                }
            ]
        }
    )

    result = extract_fact("Body shall be SS316", "Body of body shall be SS316", client=client)

    assert result.found is True
    assert result.facts[0].value == "SS316"
    assert result.facts[0].quote == "Body of body shall be SS316"
    assert client.calls == 1


def test_a_passage_that_states_nothing_is_not_a_fact():
    """A passage about something else is a false, not a guess."""
    client = FakeClient(
        {
            "requirements": [
                {
                    "found": False,
                    "attribute": None,
                    "value": None,
                    "unit": None,
                    "operator": None,
                    "model": None,
                    "quote": None,
                }
            ]
        }
    )

    result = extract_fact("Body shall be SS316", "A gearbox for the pump", client=client)

    assert result.found is False


def test_an_answer_that_is_not_json_is_a_failed_extraction():
    """There is no safe way to read a requirement out of a sentence of
    explanation."""
    from app.services.llm import LLMError

    client = FakeClient(LLMError("the model's answer was not JSON"))

    result = extract_fact("Body shall be SS316", "Body of body shall be SS316", client=client)

    assert result.found is False
    assert result.error


def test_an_answer_with_an_unknown_field_is_refused():
    """The answer is validated, so a model that invents a field is refused
    rather than half-read."""
    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "SS316",
                    "confidence": 0.9,
                }
            ]
        }
    )

    result = extract_fact("Body shall be SS316", "Body of body shall be SS316", client=client)

    assert result.found is False
    assert result.error


def test_an_empty_passage_is_not_read():
    result = extract_fact("Body shall be SS316", "   ", client=FakeClient({}))

    assert result.found is False
    assert result.error


def test_a_rate_limit_is_not_a_passage_that_states_nothing():
    """The provider's limit is an unanswered question, so it stops the run and
    is never recorded as a fact that was not found (Phase 12 rule)."""
    import pytest

    from app.services.llm import LLMRateLimited

    client = FakeClient(LLMRateLimited("the model is rate limited", retry_after=24.9))

    with pytest.raises(LLMRateLimited):
        extract_fact(
            "Body shall be SS316", "Body of body shall be SS316", client=client
        )
