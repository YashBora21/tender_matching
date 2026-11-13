"""Synthetic repeat discovery and explicit review; no provider/database calls."""
import pytest

from app.services.tender import templates
from app.services.tender.source_policy import triage, fingerprint


def row(tender, text, index='1'):
    return {'id': 'synthetic-' + tender + '-' + index, 'tender_id': tender,
            'source_text': text, 'source_page': 1, 'filename': 'synthetic.pdf'}


def test_revisions_or_repetition_within_one_tender_cannot_create_a_template():
    rows = [row('one', 'Synthetic clause', str(i)) for i in range(8)]
    assert templates.candidates(rows) == []
    assert templates.candidates(rows + [row('two', 'Synthetic clause')]) == []


def test_three_distinct_tenders_nominate_data_without_approving_exclusion():
    found = templates.candidates([row('one', 'Synthetic clause'),
        row('two', 'SYNTHETIC  clause'), row('three', 'Synthetic\nclause')])
    assert len(found) == 1 and found[0]['distinct_tenders'] == 3
    assert len(found[0]['examples']) == 3
    assert found[0]['approved'] is False and found[0]['type'] is None
    assert found[0]['roles'] == [] and not found[0]['context_independent']


def test_normalization_preserves_values_and_multilingual_text():
    assert templates.normalized('Synthetic pressure 8 bar') != templates.normalized('Synthetic pressure 16 bar')
    assert templates.normalized('दबाव  8 bar') == 'दबाव 8 bar'


def template(text, **changes):
    return dict({'template_id': 'synthetic-template', 'normalized_text': templates.normalized(text),
        'approved': True, 'context_independent': True, 'roles': ['cover'],
        'type': 'informational'}, **changes)


def test_template_requires_explicit_review_role_and_context_independence(monkeypatch):
    text = 'Synthetic document label'
    for changes in ({'approved': False}, {'roles': ['sla']}, {'context_independent': False}):
        monkeypatch.setattr(templates, 'reviewed_data', lambda: {'templates': [template(text, **changes)]})
        assert templates.reviewed_template(text, 'cover') is None
    monkeypatch.setattr(templates, 'reviewed_data', lambda: {'templates': [template(text)]})
    assert templates.reviewed_template(text, 'cover')['type'] == 'informational'
    assert triage(text, 'cover').clause_type == 'informational'
    assert triage(text, 'specification').candidate_type == 'technical'


def test_conflicting_reviewed_types_do_not_approve_a_mapping(monkeypatch):
    text = 'Synthetic label'
    monkeypatch.setattr(templates, 'reviewed_data', lambda: {'templates': [
        template(text), template(text, type='commercial')]})
    assert templates.reviewed_template(text, 'cover') is None


@pytest.mark.parametrize('text,candidate', [('pressure 8 bar', 'technical'),
    ('Synthetic manufacturer must have ISO 9001.', 'certification')])
@pytest.mark.parametrize('role',['specification','cover'])
def test_reviewed_template_keeps_signals_within_the_current_source_scope(monkeypatch, text, candidate,role):
    monkeypatch.setattr(templates, 'reviewed_data', lambda: {'templates': [template(text,roles=['specification','cover'])]})
    result = triage(text, role)
    assert result.clause_type == candidate
    assert result.context_only == (role=='cover')
    assert result.candidate_type == (None if role=='cover' else candidate)


def test_reviewed_policy_changes_invalidate_source_policy_fingerprint(monkeypatch):
    before = fingerprint()
    monkeypatch.setattr(templates, 'reviewed_data', lambda: {'templates': [template('Synthetic label')]})
    assert fingerprint() != before


def test_discovery_cannot_overwrite_reviewed_policy(monkeypatch, tmp_path):
    policy = tmp_path / 'synthetic-reviewed.json'
    monkeypatch.setattr(templates, 'templates_path', lambda: policy)
    with pytest.raises(SystemExit) as exc:
        templates.main(['--output', str(policy)])
    assert exc.value.code == 2 and not policy.exists()
