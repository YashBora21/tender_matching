"""Synthetic corpus-gap abstention occurs before retrieval/model work."""
from types import SimpleNamespace

from app.repositories.part_coverage import indexed_part_manufacturers
from app.services.output.runs import _assess
from app.schemas.enums import ReasonCode, RequirementStatus


def test_indexed_part_query_is_reused_and_exact_spelling_is_checked():
    class Session:
        info = {}
        calls = 0
        def execute(self, statement, params):
            self.calls += 1
            assert params['part'] == 'SYNTHETIC-X-001'
            return SimpleNamespace(all=lambda: [('Synthetic A', 'SYNTHETIC-X-001'),
                                               ('Synthetic B', 'SYNTHETIC-X-002')])
    session = Session()
    assert indexed_part_manufacturers(session, 'SYNTHETIC-X-001') == ['Synthetic A']
    assert indexed_part_manufacturers(session, 'synthetic-x-001') == ['Synthetic A']
    assert session.calls == 1


def test_corpus_gap_never_calls_matcher_or_declares_missing_capability(monkeypatch):
    monkeypatch.setattr('app.repositories.part_coverage.source_part', lambda *a: 'SYNTHETIC-X-001')
    monkeypatch.setattr('app.repositories.part_coverage.indexed_part_manufacturers', lambda *a: [])
    def forbidden(*a, **k):
        raise AssertionError('expensive matching must not run')
    outcome, _ = _assess(None, {'text': 'Synthetic part requirement', 'type': 'technical', 'item_id': '1'},
                         {'1': {'category': 'valves'}}, manufacturer_name='Synthetic A',
                         matcher=forbidden, client=None)
    assert outcome.status is RequirementStatus.NO_EVIDENCE_FOUND
    assert outcome.reason_code is ReasonCode.CORPUS_GAP
    assert outcome.review_required and outcome.candidates == 0 and outcome.llm_calls == 0
