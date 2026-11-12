"""Synthetic source-role policy; no provider calls or production database writes."""
from dataclasses import asdict

import pytest

from app.schemas.enums import RequirementType
from app.services.tender.source_policy import source_role, triage
from app.services.tender.ingest import RequirementRecord, assign_to_items, TenderItem, drop_fragments
from app.services.tender.pipeline import _extract_all, _as_dict, _as_record, to_stored
from app.services.tender.completeness import completion_gaps, page_coverage
from app.services.output.runs import assessable, needs_model


def record(text, role, **kwargs):
    decision = triage(text, role, **kwargs)
    return RequirementRecord(text=text, source_text=text, item_id='',
        requirement_type=RequirementType(decision.clause_type), source_role=role,
        context_only=decision.context_only, candidate_type=decision.candidate_type)


@pytest.mark.parametrize('role', ['cover', 'atc', 'sla', 'supporting'])
def test_restricted_administrative_text_stays_visible_without_any_model_call(role):
    row = record('Synthetic invoice payment is due under the contract.', role)
    class Forbidden:
        def complete_json(self, *args, **kwargs):
            raise AssertionError('restricted context must never reach a model')
    rows, calls, hits, limited, _ = _extract_all([row], client=Forbidden())
    assert len(rows) == 1 and rows[0].text == row.text
    assert rows[0].context_only and rows[0].requirement_type is RequirementType.COMMERCIAL
    assert calls == hits == 0 and not limited
    assert not completion_gaps({}, [], [], [asdict(rows[0])])
    stored = to_stored(row, 'synthetic', model='synthetic', input_hash='synthetic')
    assert stored.normalization['source_context']['source_role'] == role
    assert stored.model is None  # deterministic context was not read by the configured model
    assert not assessable([vars(stored)]) and not needs_model([vars(stored)])


def test_company_iso_clause_is_retained_as_a_candidate_even_on_a_commercial_page():
    row = record('Synthetic manufacturer must have ISO 9001 certification.', 'cover',
                 page_tag='commercial_legal')
    assert not row.context_only and row.candidate_type == 'certification'
    assert row.requirement_type is RequirementType.CERTIFICATION
    assert completion_gaps({}, [], [], [asdict(row)])  # no guessed item or attributes


@pytest.mark.parametrize('role', ['specification', 'boq'])
def test_unresolved_primary_criteria_still_block(role):
    row = record('Synthetic proprietary criterion ALPHA', role)
    row.review_reason = 'source_coverage_gap: synthetic unread criterion'
    assert not row.context_only
    assert completion_gaps({}, [], [], [asdict(row)])


def test_technical_condition_inside_buyer_terms_is_not_lost():
    row = record('Working pressure 8 bar', 'atc', section='Additional terms and conditions')
    assert not row.context_only and row.candidate_type == 'technical'
    qualification = record('Bidder supplied valves with pressure rating of 16 bar minimum',
                           'atc', section='Additional terms and conditions')
    assert qualification.context_only
    assert qualification.requirement_type is RequirementType.ELIGIBILITY
    unfamiliar = record('Synthetic product shall withstand ZETA exposure.', 'atc')
    assert unfamiliar.candidate_type == 'technical' and not unfamiliar.context_only


def test_review_flag_is_not_tender_incomplete_and_context_is_not_item_assigned():
    row = record('Synthetic heading', 'cover')
    row.review_required = True
    row.review_reason = 'Synthetic context reviewer note'
    assert not completion_gaps({}, [], [], [asdict(row)])
    assigned, unassigned = assign_to_items([{'description': row.text, 'context_only': True}],
                                         [TenderItem(item_id='1', title='Synthetic item')])
    assert not assigned and len(unassigned) == 1
    kept, _ = drop_fragments([record('1.', 'cover')])
    assert len(kept) == 1


def test_role_metadata_round_trips_through_response_cache_and_storage():
    row = record('Synthetic manufacturer requires ISO 9001.', 'atc')
    cached = _as_record(_as_dict(row))
    assert (cached.source_role, cached.context_only, cached.candidate_type) == ('atc', False, 'certification')
    context = to_stored(row, 'synthetic', model='synthetic', input_hash='synthetic').normalization['source_context']
    assert context['candidate_type'] == 'certification' and not context['context_only']


def test_document_roles_are_explicit_and_restricted_names_outrank_coarse_kind():
    assert source_role({'source_key': 'wrapper', 'kind': 'spec'}, specification=True) == 'cover'
    assert source_role({'kind': 'spec', 'path': 'synthetic_ATC.pdf'}, specification=True) == 'atc'
    assert source_role({'kind': 'spec', 'path': 'synthetic-SLA.pdf'}) == 'sla'
    assert source_role({'kind': 'boq-pdf'}) == 'boq'
    assert source_role({'kind': 'scanned'}, specification=True) == 'specification'
    assert source_role({'kind': 'scanned'}) == 'supporting'


def test_wrapper_and_attachment_pages_remain_separate_in_the_ledger():
    ledger = page_coverage({'sha256': 'synthetic-wrapper', 'page_count': 1},
        [{'id': 'synthetic-attachment', 'sha256': 'synthetic-spec', 'pages': 1}],
        [{'source_key': 'wrapper', 'page': 1, 'ledger_status': 'parsed'},
         {'source_key': 'synthetic-attachment', 'page': 1, 'ledger_status': 'parsed'}])
    assert len(ledger['sources']) == 2 and ledger['accounted'] == 2
    assert not ledger['incomplete_ingestion']


def test_a_nontechnical_model_label_cannot_hide_an_approved_technical_attribute():
    row = record('pressure 8 bar', 'cover')
    class IncorrectReader:
        def complete_json(self, *args, **kwargs):
            return {'requirements': [{'text':row.text, 'source_quote':row.text,
                'type':'informational','attribute':'operating_pressure','value':'8',
                'unit':'bar','operator':'eq','mandatory':True}]}
    rows, _, _, _, _ = _extract_all([row], client=IncorrectReader())
    assert rows[0].requirement_type is RequirementType.UNKNOWN
    assert not rows[0].context_only and rows[0].review_required
    assert completion_gaps({}, [], [], [asdict(rows[0])])


def test_package_reader_attaches_roles_before_model_processing(monkeypatch, tmp_path):
    from app.services.tender import pipeline
    from app.services.tender.ingest import PageRead, SpecificationRead
    spec = {'kind':'spec','path':'synthetic-spec.pdf','sha256':'synthetic-spec'}
    sla = {'kind':'scanned','path':'synthetic-SLA.pdf','sha256':'synthetic-sla'}
    def read(source, directory):
        texts = (['pressure 8 bar'] if source is spec else
                 ['Synthetic invoice payment under the contract.', 'Synthetic manufacturer must have ISO 9001.'])
        return SpecificationRead(rows=[{'description':text,'row_text':text,'page':1}
                                      for text in texts],
                                 pages=[PageRead(page=1,tag='mixed',status='parsed')])
    monkeypatch.setattr(pipeline, '_read_specification', read)
    package = pipeline._read_package('synthetic', tmp_path, [spec,sla], spec, None)
    assert [row['source_role'] for row in package.rows] == ['specification','sla','sla']
    assert [row['context_only'] for row in package.rows] == [False,True,False]
    assert package.rows[-1]['candidate_type'] == 'certification'
    assert [page.source_role for page in package.pages] == ['specification','sla']
