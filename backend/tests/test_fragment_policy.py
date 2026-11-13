"""Synthetic prose fragments: no external model calls or application DB writes."""
from dataclasses import asdict
import json
from pathlib import Path
import pytest
from app.core.config import get_settings
from app.schemas.enums import RequirementType
from app.services.tender.source_policy import triage, fingerprint, blocking_requirement
from app.services.tender.ingest import RequirementRecord, drop_fragments
from app.services.tender.pipeline import _as_dict, _as_record, _stored_records, _extract_all, to_stored, stage_keys, STAGE_REQUIREMENTS, STAGE_ITEMS, STAGE_ATTACHMENTS
from app.services.tender.completeness import completion_gaps
from app.services.tender.upload import effective_limits


@pytest.fixture(autouse=True)
def floors(monkeypatch):
    monkeypatch.setenv('REQ_MIN_CHARS', '20')
    monkeypatch.setenv('REQ_MIN_WORDS', '4')
    get_settings.cache_clear()


def record(text, role='cover', **kwargs):
    decision = triage(text, role, **kwargs)
    return RequirementRecord(text=text, source_text=text, item_id='', page=2,
        source_role=role, context_only=decision.context_only,
        fragment_reason=decision.fragment_reason, candidate_type=decision.candidate_type,
        requirement_type=RequirementType(decision.clause_type), review_required=True,
        review_reason='Synthetic independent reviewer note')


@pytest.mark.parametrize('role', ['cover', 'atc', 'sla', 'supporting'])
def test_short_noise_is_stored_as_unknown_context_and_never_read_by_model(role):
    row = record('Loose scrap', role)
    assert row.fragment_reason and row.context_only
    assert row.requirement_type is RequirementType.UNKNOWN
    class Forbidden:
        def complete_json(self, *args, **kwargs): raise AssertionError('No model for fragments')
    result, calls, hits, limited, _ = _extract_all([row], client=Forbidden())
    assert result == [row] and calls == hits == 0 and not limited
    assert not blocking_requirement(asdict(row))
    assert completion_gaps({}, [], [], [asdict(row)]) == []
    assert completion_gaps({}, [], [{'page':1,'ledger_status':'failed'}], [asdict(row)])
    assert drop_fragments([row])[0] == [row]
    assert row.text == row.source_text == 'Loose scrap'
    assert row.review_required and row.review_reason == 'Synthetic independent reviewer note'


@pytest.mark.parametrize('text', ['8 bar', 'ISO 9001', 'Must be new'])
def test_short_signal_is_retained_for_interpretation(text):
    row = record(text)
    assert row.fragment_reason is None and not row.context_only
    assert row.candidate_type in {'technical', 'certification'}


def test_technical_parent_or_page_context_is_retained():
    for context in [dict(section='Technical specification'), dict(page_tag='technical')]:
        row = record('ZETA', **context)
        assert row.fragment_reason is None and row.candidate_type == 'technical'


@pytest.mark.parametrize('role', ['specification', 'boq'])
@pytest.mark.parametrize('table', [False, True])
def test_primary_sources_and_table_rows_are_exempt(role, table):
    row = record('ZETA', role, table=table)
    assert row.fragment_reason is None and not row.context_only


def test_restricted_source_table_is_not_fragmented():
    assert record('ZETA', table=True).fragment_reason is None


def test_floor_boundary_and_unicode_whitespace(monkeypatch):
    text = 'abcd efgh ijkl mnopq'
    assert len(text) == 20 and len(text.split()) == 4
    assert record(text).fragment_reason is None
    assert record(text[:-1]).fragment_reason
    spaced = 'abcd   efgh\nijkl mnopq'
    row = record(spaced)
    assert row.fragment_reason is None and row.source_text == spaced
    assert record('अज्ञात पाठ').fragment_reason
    monkeypatch.setenv('REQ_MIN_CHARS', '0'); monkeypatch.setenv('REQ_MIN_WORDS', '0')
    get_settings.cache_clear()
    assert record('Loose scrap').fragment_reason is None


@pytest.fixture
def aliases(tmp_path, monkeypatch):
    from app.comparison.vocabulary import vocabulary_path
    data = json.loads(Path(vocabulary_path()).read_text(encoding='utf-8'))
    data['attributes'] += [dict(canonical_term=name, value_type='numeric', approved=True)
                           for name in ['synthetic_zeta', 'synthetic_eta']]
    path = tmp_path / 'synthetic-vocabulary.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setenv('ATTRIBUTE_VOCABULARY_PATH', str(path)); get_settings.cache_clear()
    return [dict(alias='Zeta label', canonical_term='synthetic_zeta', approved=True, confidence=1)]


def test_approved_alias_retains_short_label_and_ambiguity_is_not_discarded(aliases):
    row = record('Zeta label', aliases=aliases)
    assert row.fragment_reason is None and row.candidate_type == 'technical'
    aliases[0]['approved'] = False
    assert record('Zeta label', aliases=aliases).fragment_reason is None
    aliases[0]['approved'] = True
    aliases.append(dict(alias='Zeta label', canonical_term='synthetic_eta', approved=True, confidence=1))
    row = record('Zeta label', aliases=aliases)
    assert row.fragment_reason is None and row.requirement_type is RequirementType.UNKNOWN


def test_fragment_round_trip_keeps_original_source_and_independent_review():
    row = record('Loose scrap')
    row.source_table_id = 'synthetic-source'; row.source_row_id = 'row-1'
    cached = _as_record(_as_dict(row))
    stored = to_stored(cached, 'synthetic', model='synthetic')
    assert stored.normalization['source_context']['fragment_reason'] == row.fragment_reason
    assert stored.source_text == row.source_text and stored.model is None
    class Repo:
        def list_requirements(self, tender_id):
            return [dict(vars(stored), type=stored.requirement_type, page=stored.page,
                         source_table_id=row.source_table_id, source_row_id=row.source_row_id)]
    restored = _stored_records(Repo(), 'synthetic')[0]
    assert restored.fragment_reason == row.fragment_reason and restored.context_only
    assert restored.source_table_id == row.source_table_id
    assert restored.review_required and restored.review_reason == row.review_reason


def test_floors_and_alias_snapshot_invalidate_source_checkpoints(monkeypatch):
    options=dict(prompt_version='synthetic', schema_version='synthetic', model='synthetic', vocabulary_hash='synthetic')
    first = stage_keys([], **options, alias_fingerprint='first')
    second = stage_keys([], **options, alias_fingerprint='second')
    assert first[STAGE_REQUIREMENTS] != second[STAGE_REQUIREMENTS]
    assert first[STAGE_ITEMS] == second[STAGE_ITEMS]
    old_hash = fingerprint()
    monkeypatch.setenv('REQ_MIN_CHARS', '21');get_settings.cache_clear()
    third = stage_keys([], **options, alias_fingerprint='first')
    assert fingerprint() != old_hash and third[STAGE_REQUIREMENTS] != first[STAGE_REQUIREMENTS]
    assert third[STAGE_ATTACHMENTS] == first[STAGE_ATTACHMENTS]
    assert effective_limits()['req_min_chars'] == 21
    assert effective_limits()['req_min_words'] == 4


def test_package_metadata_distinguishes_prose_from_ocr_table(monkeypatch, tmp_path):
    from app.services.tender import pipeline
    from app.services.tender.ingest import PageRead, SpecificationRead
    source = {'path': 'synthetic-sla.pdf', 'kind': 'scanned', 'sha256': 'synthetic'}
    monkeypatch.setattr(pipeline, '_read_specification', lambda *args: SpecificationRead(
        rows=[{'page':1,'description':'Loose scrap','extraction_method':'text'},
              {'page':1,'description':'ZETA','extraction_method':'ocr',
               'source_headers':['Synthetic label'],'source_cells':['ZETA'],'source_row_id':'table-row'}],
        pages=[PageRead(page=1,tag='mixed',status='ocr')]))
    result = pipeline._read_package('synthetic', tmp_path, [], source, None)
    assert result.rows[0]['fragment_reason']
    assert result.rows[1]['fragment_reason'] is None
