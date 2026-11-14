"""Offline Part B benchmark: request + input-char counts, cache/dedup on vs off.

Run:  ../venv/Scripts/python.exe eval/part_b_benchmark.py   (from repo root)
No database and no paid calls: retrieval, context and the candidate verdict are
synthetic stand-ins, so only the batching/cache mechanics are measured.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("LLM_MATCH_EXTRACTION_BATCH_SIZE", "2")

from app.services.match import match
from app.services.match.facts import batch_input_chars
from app.services.match.retrieve import Candidate
from app.schemas.enums import RequirementStatus

CTX = {"tables": [{"headers": ["Body Material"], "rows": [["CF8M"]]}], "passage": "Body CF8M"}
ROWS = [Candidate(chunk_id=str(i), document_id="d", text="Synthetic passage %d" % i,
                  page=1, manufacturer="M", model="X") for i in range(2)]
REQS = ["Body shall be CF8M", "Body material must be CF8M", "Body is CF8M"]


class Client:
    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, **kw):
        self.calls += 1
        p = json.loads(user)
        return {"passages": [{"passage_id": x["passage_id"], "requirements": [{"found": False}]}
                             for x in p["passages"]]}


def verified(*a, **k):
    return match.MatchResult(requirement_text="Synthetic", status=RequirementStatus.FULL_MATCH)


def run(cache_on):
    client = Client()

    def cached(s, r, m, model):
        if not model:
            return None
        # once this run has paid for a read, later requirements reuse it
        if cache_on and client.calls > 0:
            return {"attribute": "body material"}
        return None

    with mock.patch.object(match, "_cached_fact", cached), \
         mock.patch.object(match, "_from_cached_fact", lambda c, t, **k: verified()), \
         mock.patch.object(match, "search", lambda *a, **k: ROWS), \
         mock.patch.object(match, "_evidence_context", lambda *a: CTX), \
         mock.patch.object(match, "_match_candidate", verified):
        statuses = [match.match_requirement(SimpleNamespace(commit=lambda: None), t,
                                            manufacturer_name="M", model="X",
                                            requirement_attribute="body material",
                                            client=client).status for t in REQS]
    return client.calls, statuses


def main():
    passages = [r.text for r in ROWS]
    naive = sum(map(len, passages)) + len(passages) * len(json.dumps(CTX))
    dedup = batch_input_chars(passages, [CTX] * len(passages))

    before_calls, before_status = run(cache_on=False)
    after_calls, after_status = run(cache_on=True)

    assert before_status == after_status, "verified outcomes must not change"
    assert after_calls < before_calls and dedup < naive
    print("input chars per shared-context batch: before=%d after=%d" % (naive, dedup))
    print("requests for %d requirements: no-cache=%d cache=%d" % (len(REQS), before_calls, after_calls))
    print("verified outcomes unchanged:", before_status == after_status)


if __name__ == "__main__":
    main()
