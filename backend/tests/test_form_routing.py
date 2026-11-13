"""Synthetic forms retain provenance and never incur provider calls."""
from dataclasses import asdict
import pytest
from app.services.tender.source_policy import triage, form_heading, fingerprint
from app.services.tender.ingest import RequirementRecord, SpecificationRead, PageRead
from app.schemas.enums import RequirementType
from app.services.tender import pipeline


@pytest.mark.parametrize('tag',['commercial_legal','boilerplate'])
def test_form_only_page_triggers_no_client_calls(tag):
    texts=['Synthetic signature and name', 'Material breach of this contract is prohibited.',
           'The Principal shall provide a signed declaration.', 'Synthetic registered address']
    rows=[]
    for text in texts:
        decision=triage(text,'specification',page_tag=tag)
        rows.append(RequirementRecord(text=text,source_text=text,item_id='',page=1,
            source_role=decision.source_role,context_only=decision.context_only,
            candidate_type=decision.candidate_type,requirement_type=RequirementType(decision.clause_type)))
    class Forbidden:
        def complete_json(self,*args,**kwargs):raise AssertionError('Form-only content must not be billed')
    output,calls,_,limited,_=pipeline._extract_all(rows,client=Forbidden())
    assert calls==0 and not limited and len(output)==len(rows)
    assert all(row.context_only for row in output)
    assert [row.source_text for row in output]==texts


@pytest.mark.parametrize('text',['Working pressure 8 bar','Body material carbon steel',
    'The body shall withstand ZETA exposure.','Synthetic manufacturer must have ISO 9001.'])
def test_form_context_does_not_hide_real_product_or_certification_candidates(text):
    row=triage(text,'supporting',page_tag='mixed',form_context=True)
    assert not row.context_only and row.candidate_type in {'technical','certification'}


def test_document_heading_carries_form_context_to_mixed_following_pages(monkeypatch,tmp_path):
    source={'kind':'spec','path':'synthetic.pdf','sha256':'synthetic'}
    texts=['(Format of Integrity Pact)','Synthetic name and address','Material breach']
    monkeypatch.setattr(pipeline,'_read_specification',lambda *args:SpecificationRead(
        rows=[{'page':i+1,'description':text,'row_text':text,'page_tag':'mixed'} for i,text in enumerate(texts)],
        pages=[PageRead(page=i+1,tag='mixed',status='parsed') for i in range(len(texts))]))
    result=pipeline._read_package('synthetic',tmp_path,[],source,None)
    assert all(row['context_only'] for row in result.rows)
    assert all(row['source_role']=='supporting' for row in result.rows)
    assert len(result.pages)==3 and all(page.status=='parsed' for page in result.pages)


def test_selector_changes_do_not_invalidate_unchanged_classifier_inputs(monkeypatch):
    from app.core.config import get_settings
    before=fingerprint(for_classification=True)
    source_before=fingerprint()
    monkeypatch.setenv('SOURCE_FORM_HEADING_CHARS','900');get_settings.cache_clear()
    assert fingerprint(for_classification=True)==before
    assert fingerprint()!=source_before


def test_technical_annexure_is_not_recognized_as_a_form_heading():
    assert form_heading([{'page':1,'description':'Synthetic technical annexure: pressure 8 bar'}]) is None


def test_stored_form_candidate_is_retriaged_before_the_cohort_builder():
    row=RequirementRecord(text='Synthetic name and address',source_text='Synthetic name and address',
        item_id='',source_role='specification',page_tag='commercial_legal',candidate_type='technical',
        requirement_type=RequirementType.UNKNOWN,source_table_id='synthetic')
    class Forbidden:
        def complete_json(self,*args,**kwargs):raise AssertionError('Stale form candidate must not be billed')
    output,calls,_,_,_=pipeline._extract_all([row],client=Forbidden())
    assert not calls and output[0].context_only
