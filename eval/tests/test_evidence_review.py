"""Synthetic source checks never populate human gold labels."""
from eval import evidence_review
import hashlib


def test_missing_source_remains_unlabelled(monkeypatch):
    monkeypatch.setattr(evidence_review, '_corpus_file', lambda path: None)
    result = evidence_review.inspect_fact({'path':'synthetic.pdf'}, {})
    assert result['machine_issue'] == 'source_missing'
    assert result['human_attribute_correct'] == ''


def test_wrong_column_is_reported_separately_from_citation(monkeypatch):
    from types import SimpleNamespace
    path = SimpleNamespace(exists=lambda:True,read_bytes=lambda:b'synthetic')
    monkeypatch.setattr(evidence_review, '_corpus_file', lambda name:path)
    row = dict(path='synthetic.pdf',sha256=hashlib.sha256(b'synthetic').hexdigest(),
               document_id='d',page=1,quote='Integral flange',attribute='end connection',value='integral flange')
    pages = {('d',1):('Integral flange',[[['Body Design','End Connections'],['Integral flange','ASME B16.5']]])}
    result = evidence_review.inspect_fact(row,pages)
    assert result['citation_passed']
    assert result['machine_issue'] == 'wrong_column'
    assert result['human_value_correct'] == ''



def test_legacy_writer_requires_explicit_opt_in_before_reading_or_writing(monkeypatch):
    import pytest
    from eval import apply_evidence_review
    monkeypatch.setattr(apply_evidence_review,'get_engine',lambda:pytest.fail('Database must not open without opt-in'))
    with pytest.raises(SystemExit,match='No database changes made'):
        apply_evidence_review.main([])


def test_current_quarantine_preserves_claim_and_never_promotes():
    from copy import deepcopy
    from eval.apply_evidence_review import quarantine_plan
    from app.services.cli.fact_cache import digest
    rows=[dict(fact_id='synthetic-fact',verification_status='verified',value='Synthetic value',quote='Synthetic quote')]
    original=deepcopy(rows)
    proposals=[dict(fact_id='synthetic-fact',original_fact_sha256=digest(rows[0]),
        proposed_status='needs_review',blockers=['Synthetic missing scope'],cache_eligible_after_reviewed_apply=False)]
    assert len(quarantine_plan(rows,proposals))==1
    proposals[0]['proposed_status']='verified'
    assert quarantine_plan(rows,proposals)==[] and rows==original


def test_current_quarantine_rejects_stale_or_duplicate_snapshots():
    import pytest
    from eval.apply_evidence_review import quarantine_plan
    row=dict(fact_id='synthetic-fact',verification_status='verified')
    proposal=dict(fact_id='synthetic-fact',original_fact_sha256='synthetic-stale')
    with pytest.raises(ValueError,match='Stale'):quarantine_plan([row],[proposal])
    with pytest.raises(ValueError,match='identities'):quarantine_plan([row],[proposal,proposal])
