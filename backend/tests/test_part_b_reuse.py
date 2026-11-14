"""Part B: reuse verified reads, dedup shared context, no paid re-buys (offline)."""
import json
from types import SimpleNamespace

import pytest

from app.services.match import match
from app.services.match.facts import batch_input_chars, extract_fact_batch
from app.services.match.retrieve import Candidate


class Client:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0
        self.payloads = []

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        self.payloads.append(json.loads(user))
        return self.answer


def _ctx(tag):
    return {"tables": [{"headers": ["Synthetic"], "rows": [[tag]]}], "passage": tag}


def test_identical_context_sent_once_by_id(monkeypatch):
    monkeypatch.setenv("LLM_MATCH_EXTRACTION_BATCH_SIZE", "2")
    client = Client({"passages": [{"passage_id": i, "requirements": [{"found": False}]} for i in range(2)]})
    extract_fact_batch("Synthetic body", ["A", "B"], client=client, source_contexts=[_ctx("x"), _ctx("x")])
    payload = client.payloads[0]
    assert len(payload["contexts"]) == 1
    assert [p["context_id"] for p in payload["passages"]] == [0, 0]


def test_dedup_reduces_input_chars():
    same = batch_input_chars(["A", "B"], [_ctx("x"), _ctx("x")])
    distinct = batch_input_chars(["A", "B"], [_ctx("x"), _ctx("y")])
    assert same < distinct


def test_one_verified_read_serves_several_requirements(monkeypatch):
    monkeypatch.setattr(match, "_cached_fact", lambda *a: {"attribute": "body material"})
    monkeypatch.setattr(match, "_from_cached_fact", lambda cached, text, **k: match.MatchResult(requirement_text=text))
    monkeypatch.setattr(match, "search", lambda *a, **k: pytest.fail("cache hit must not retrieve"))
    client = Client({})
    for text in ("Body shall be SS316", "Body material must be SS316"):
        result = match.match_requirement(SimpleNamespace(commit=lambda: None), text,
                                         manufacturer_name="M", model="X",
                                         requirement_attribute="body material", client=client)
        assert result.llm_calls == 0
    assert client.calls == 0


def test_cached_candidates_trigger_no_paid_batch(monkeypatch):
    monkeypatch.setenv("LLM_MATCH_EXTRACTION_BATCH_SIZE", "2")
    # Top-level lookup (no manufacturer/model) misses; per-candidate lookups hit.
    monkeypatch.setattr(match, "_cached_fact", lambda s, r, m, model: {"attribute": "body material"} if model else None)
    monkeypatch.setattr(match, "search", lambda *a, **k: [
        Candidate(chunk_id="a", document_id="d", text="Synthetic A", page=1, manufacturer="M", model="X"),
        Candidate(chunk_id="b", document_id="d", text="Synthetic B", page=1, manufacturer="M", model="X"),
    ])
    monkeypatch.setattr(match, "_match_candidate", lambda *a, **k: match.MatchResult(requirement_text="Synthetic", review_required=True))
    client = Client({})
    result = match.match_requirement(SimpleNamespace(commit=lambda: None), "Body shall be SS316", client=client)
    assert client.calls == 0 and result.llm_calls == 0


def test_wrong_column_evidence_still_fails():
    from app.services.match.verify import check_table_binding
    wrong = [[["Body Material", "Size"], ["A216 WCB", "CF8M"]]]
    right = [[["Body Material", "Size"], ["CF8M", "2"]]]
    assert check_table_binding(wrong, "body material", "CF8M") is not None
    assert check_table_binding(right, "body material", "CF8M") is None
