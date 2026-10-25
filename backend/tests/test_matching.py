"""Matching: retrieve, extract, verify, compare."""

from __future__ import annotations

import pytest

from app.schemas.enums import RequirementStatus
from app.services.matching import match_requirement


class FakeSession:
    """A session that answers nothing, so the retrieval finds nothing."""


class FakeClient:
    """A client that answers with what it is given."""

    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user, **kwargs):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_requirement_nothing_answers_is_no_evidence_found(monkeypatch):
    """No evidence is not a missing capability (PRD 4)."""
    from app.services import matching

    monkeypatch.setattr(matching, "search", lambda *a, **k: [])

    result = match_requirement(FakeSession(), "Body shall be SS316", client=FakeClient({}))

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.score == 0.0
    assert result.review_required is True
    assert result.reason


def test_a_passage_that_states_nothing_is_no_evidence_found(monkeypatch):
    from app.services import matching
    from app.services.retrieval import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="A gearbox for the pump", page=1
    )
    monkeypatch.setattr(matching, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(matching, "_page_text", lambda s, c: "A gearbox for the pump")

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

    result = match_requirement(FakeSession(), "Body shall be SS316", client=client)

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND


def test_a_quote_that_is_not_on_the_page_fails_the_citation_layer(monkeypatch):
    """A claim whose quote is not on the page it names is not verified
    (FR-23)."""
    from app.services import matching
    from app.services.retrieval import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(matching, "search", lambda *a, **k: [candidate])
    # The page says something else.
    monkeypatch.setattr(matching, "_page_text", lambda s, c: "A completely different page")

    client = FakeClient(
        {
            "requirements": [
                {
                    "found": True,
                    "attribute": "body material",
                    "value": "SS316",
                    "unit": None,
                    "operator": "eq",
                    "model": None,
                    "quote": "Body of body shall be SS316",
                }
            ]
        }
    )

    result = match_requirement(FakeSession(), "Body shall be SS316", client=client)

    assert result.status is not RequirementStatus.FULL_MATCH
    assert result.review_required is True
    assert result.quote == "Body of body shall be SS316"


def test_an_extraction_that_fails_is_flagged_not_silently_empty(monkeypatch):
    """A failed extraction is a failure the reviewer can see."""
    from app.services import matching
    from app.services.llm import LLMError
    from app.services.retrieval import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(matching, "search", lambda *a, **k: [candidate])

    result = match_requirement(
        FakeSession(), "Body shall be SS316", client=FakeClient(LLMError("not JSON"))
    )

    assert result.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert result.review_required is True
    assert result.reason


def test_the_fact_that_answers_the_requirement_is_the_one_compared(monkeypatch):
    """One passage can state several capabilities. The comparison runs on the
    one that answers the requirement, not on whichever the model listed
    first."""
    from app.services import matching
    from app.services.retrieval import Candidate

    candidate = Candidate(
        chunk_id="c1", document_id="d1", text="Body of body shall be SS316", page=1
    )
    monkeypatch.setattr(matching, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(matching, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": False, "attribute": None, "value": None, "unit": None,
                 "operator": None, "model": None, "quote": None},
                {"found": True, "attribute": "body material", "value": "SS316",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Body of body shall be SS316"},
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=client,
    )

    assert result.fact_value == "SS316"
    assert result.status is RequirementStatus.FULL_MATCH


def test_family_level_evidence_is_capped_and_flagged_not_a_confirmed_match(monkeypatch):
    """A catalogue that covers a family is not evidence confirmed for one
    model: the score is capped and the claim goes to review (PRD 4)."""
    from app.schemas.enums import Applicability
    from app.services import matching
    from app.services.retrieval import Candidate

    candidate = Candidate(
        chunk_id="c1",
        document_id="d1",
        text="Body of body shall be SS316",
        page=1,
        is_family_level=True,
    )
    monkeypatch.setattr(matching, "search", lambda *a, **k: [candidate])
    monkeypatch.setattr(matching, "_page_text", lambda s, c: candidate.text)

    client = FakeClient(
        {
            "requirements": [
                {"found": True, "attribute": "body material", "value": "SS316",
                 "unit": None, "operator": "eq", "model": None,
                 "quote": "Body of body shall be SS316"}
            ]
        }
    )

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        client=client,
    )

    assert result.applicability is Applicability.FAMILY_LEVEL
    assert result.score <= 0.5
    assert result.review_required is True


def test_a_verified_fact_is_answered_without_retrieving_again(monkeypatch):
    """The structured fact cache is checked before retrieval (FR-21)."""
    from app.services import matching

    def no_search(*args, **kwargs):  # pragma: no cover
        raise AssertionError("retrieval ran despite a cached fact")

    monkeypatch.setattr(matching, "_cached_fact", lambda *a, **k: {
        "id": "f1",
        "value": "SS316",
        "unit": None,
        "value_type": "material",
        "document_id": "d1",
        "page": 4,
        "quote": "Body of body shall be SS316",
        "attribute": "body material",
        "is_family_level": False,
    })
    monkeypatch.setattr(matching, "search", no_search)

    result = match_requirement(
        FakeSession(),
        "Body shall be SS316",
        requirement_value="SS316",
        requirement_operator="eq",
        manufacturer_name="Acme",
        model="Series 75",
    )

    assert result.llm_calls == 0
    assert result.page == 4
    assert result.quote == "Body of body shall be SS316"
    assert result.status is RequirementStatus.FULL_MATCH
