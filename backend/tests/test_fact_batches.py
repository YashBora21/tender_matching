"""Synthetic passages keep batch evidence isolated."""
import pytest
from app.services.match.facts import extract_fact_batch


class Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete_json(self, *args):
        self.calls += 1
        return self.answer


def test_batch_preserves_passage_identity(monkeypatch):
    monkeypatch.setenv("MATCH_EXTRACTION_BATCH_SIZE", "2")
    client = Client({"passages": [
        {"passage_id": 1, "requirements": [{"found": False}]},
        {"passage_id": 0, "requirements": [{"found": True, "value": "SS316", "quote": "Body SS316"}]},
    ]})
    results = extract_fact_batch("Synthetic body", ["Body SS316", "Other text"], client=client)
    assert results[0].found and not results[1].found
    assert client.calls == 1


@pytest.mark.parametrize("ids", [[0], [0, 0], [0, 2]])
def test_bad_ids_fail_every_passage(monkeypatch, ids):
    monkeypatch.setenv("MATCH_EXTRACTION_BATCH_SIZE", "2")
    client = Client({"passages": [{"passage_id": i, "requirements": []} for i in ids]})
    assert all(r.error for r in extract_fact_batch("Synthetic", ["A", "B"], client=client))


def test_quote_cannot_cross_passages(monkeypatch):
    monkeypatch.setenv("MATCH_EXTRACTION_BATCH_SIZE", "2")
    client = Client({"passages": [
        {"passage_id": 0, "requirements": [{"found": True, "quote": "Body SS316"}]},
        {"passage_id": 1, "requirements": []},
    ]})
    assert extract_fact_batch("Synthetic", ["Unrelated", "Body SS316"], client=client)[0].error


def test_oversize_is_refused_before_call(monkeypatch):
    monkeypatch.setenv("MATCH_EXTRACTION_BATCH_CHARS", "2")
    client = Client({})
    with pytest.raises(ValueError):
        extract_fact_batch("Synthetic", ["Too long"], client=client)
    assert client.calls == 0


def test_matcher_groups_two_misses_into_one_call(monkeypatch):
    from app.services.match import match
    from app.services.match.retrieve import Candidate
    from types import SimpleNamespace

    monkeypatch.setenv("MATCH_EXTRACTION_BATCH_SIZE", "2")
    monkeypatch.setattr(match, "_cached_fact", lambda *a: None)
    monkeypatch.setattr(match, "search", lambda *a, **k: [
        Candidate(chunk_id="a", document_id="d", text="Synthetic unrelated A", page=1),
        Candidate(chunk_id="b", document_id="d", text="Synthetic unrelated B", page=2),
    ])
    monkeypatch.setattr(match, "_page_text", lambda s, c: c.text)
    monkeypatch.setattr(match, "_evidence_context", lambda s, c: {"tables": [], "passage": c.text})
    client = Client({"passages": [
        {"passage_id": i, "requirements": [{"found": False}]} for i in range(2)
    ]})
    result = match.match_requirement(SimpleNamespace(commit=lambda: None), "Body shall be SS316", client=client)
    assert client.calls == result.llm_calls == 1
    assert result.review_required


def test_budget_exhaustion_propagates_instead_of_becoming_silence(monkeypatch):
    from app.services.llm import LLMBudgetExceeded
    from app.services.match.facts import extract_fact
    class Exhausted:
        def complete_json(self,*args):
            raise LLMBudgetExceeded('Synthetic budget exhausted')
    monkeypatch.setenv('MATCH_EXTRACTION_BATCH_SIZE','2')
    with pytest.raises(LLMBudgetExceeded):
        extract_fact('Synthetic','Synthetic passage',client=Exhausted())
    with pytest.raises(LLMBudgetExceeded):
        extract_fact_batch('Synthetic',['Synthetic A','Synthetic B'],client=Exhausted())
