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
