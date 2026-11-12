"""Synthetic clause labels; no real provider requests or capability claims."""
import pytest
from app.schemas.enums import RequirementType
from app.services.llm import LLMBudgetExceeded, LLMRateLimited
from app.services.requirement.classify import classify_clause
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all, stage_keys, STAGE_EXTRACTION, STAGE_REQUIREMENTS

class Client:
    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.calls = 0
    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer

class Cache:
    def __init__(self): self.rows = {}
    def get(self, *key): return self.rows.get(key)
    def put(self, *args): self.rows[args[:-1]] = args[-1]


def source(text="Synthetic bidder must provide registration"):
    return RequirementRecord(text=text, item_id="", requirement_type=RequirementType.UNKNOWN,
                             review_required=True, review_reason="synthetic unresolved item", page=2,
                             source_table_id="synthetic-table", source_row_id="synthetic-row")


def test_valid_type_is_cached_without_extracting_values():
    text = "Synthetic bidder must provide registration"
    client = Client({"type": "eligibility", "quote": text})
    cache = Cache()
    assert classify_clause(text, client=client, cache=cache)[:3] == (RequirementType.ELIGIBILITY, True, False)
    assert classify_clause(text, client=client, cache=cache)[:3] == (RequirementType.ELIGIBILITY, False, True)
    assert client.calls == 1
    classify_clause(text + " separately", client=client, cache=cache)
    assert client.calls == 2


@pytest.mark.parametrize("answer", [{"type":"technical","quote":"invented synthetic quote"},
    {"type":"commercial","quote":""}, {"type":"full_match","quote":"Synthetic"},
    {"type":"technical","quote":"Synthetic","value":"123"}, {"requirements":[]}])
def test_invalid_classification_is_not_cached_or_accepted(answer):
    cache = Cache()
    result = classify_clause("Synthetic source clause", client=Client(answer), cache=cache)
    assert result[0] is None and result[3] and not cache.rows


@pytest.mark.parametrize("label", ["technical", "certification", "commercial", "eligibility", "informational", "unknown"])
def test_classification_preserves_original_text_and_unresolved_scope(monkeypatch, label):
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    from app.core.config import get_settings
    get_settings.cache_clear()
    row = source()
    client = Client({"type":label,"quote":row.text})
    result, calls, _, limited, _ = _extract_all([row], client=client)
    assert len(result) == 1 and calls == client.calls and not limited
    assert result[0].text == row.text and result[0].item_id == ""
    expected = RequirementType(label) if label in {'commercial', 'eligibility', 'informational'} else RequirementType.UNKNOWN
    assert result[0].requirement_type is expected and result[0].attribute is None
    assert ('Source-wide nontechnical' if label in {'commercial', 'eligibility', 'informational'} else f'Proposed clause type: {label}') in result[0].review_reason
    assert result[0].review_required
    assert result[0].source_table_id == 'synthetic-table' and result[0].source_row_id == 'synthetic-row'


@pytest.mark.parametrize('error', [LLMBudgetExceeded('synthetic limit'), LLMRateLimited('synthetic rate')])
def test_limits_stop_before_next_clause_and_preserve_unread_tail(monkeypatch,error):
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    from app.core.config import get_settings
    get_settings.cache_clear()
    rows = [source(),source('Synthetic next clause')]
    client = Client(error=error)
    result, _, _, limited, detail = _extract_all(rows,client=client)
    assert limited and detail and result == rows and client.calls == 1


def test_default_does_not_call_unvalidated_classifier():
    client = Client()
    rows = [source()]
    assert _extract_all(rows,client=client)[0] == rows
    assert client.calls == 0


def test_enabling_classifier_invalidates_only_extraction_and_downstream(monkeypatch):
    from app.core.config import get_settings
    kwargs = dict(prompt_version='synthetic',schema_version='synthetic',model='synthetic',vocabulary_hash='synthetic')
    before = stage_keys([],**kwargs)
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    get_settings.cache_clear()
    after = stage_keys([],**kwargs)
    assert before[STAGE_REQUIREMENTS] == after[STAGE_REQUIREMENTS]
    assert before[STAGE_EXTRACTION] != after[STAGE_EXTRACTION]


def test_oversize_clause_is_retained_without_a_provider_call(monkeypatch):
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_MAX_CHARS','10')
    from app.core.config import get_settings
    get_settings.cache_clear()
    row = source()
    client = Client()
    result, calls, _, _, _ = _extract_all([row],client=client)
    assert result == [row] and calls == client.calls == 0
    assert 'too_long' in result[0].review_reason and result[0].review_required


def test_source_context_is_in_payload_and_separates_cache_entries():
    import json
    class Capture(Client):
        def complete_json(self,system,user, **kwargs):
            self.payload=json.loads(user)
            return super().complete_json(system,user)
    text='Synthetic certificate requested in annexure'
    client=Capture({'type':'unknown','quote':text})
    cache=Cache()
    classify_clause(text,context={'section':'Synthetic bidder documents'},cache=cache,client=client)
    assert client.payload['clause']==text
    assert client.payload['context']['section']=='Synthetic bidder documents'
    classify_clause(text,context={'section':'Synthetic product certification'},cache=cache,client=client)
    assert client.calls==2
    assert len(cache.rows)==2


def test_quote_must_support_clause_not_only_context():
    result=classify_clause('Synthetic clause',context={'section':'Synthetic bidder documents'},
                          client=Client({'type':'eligibility','quote':'Synthetic bidder documents'}))
    assert result[0] is None and result[3]=='invalid_source_quote'


def test_wrong_informational_proposal_cannot_exclude_a_technical_clause(monkeypatch):
    from app.services.tender.completeness import completion_gaps
    from app.services.output.runs import _assess
    from app.core.config import get_settings
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    get_settings.cache_clear()
    row=source('Synthetic payment terms; the pump shall withstand pressure 12 bar.')
    result,_,_,_,_=_extract_all([row],client=Client({'type':'informational','quote':'Synthetic payment terms'}))
    pending={'text':result[0].text,'type':result[0].requirement_type.value,'item_id':None,
             'review_reason':result[0].review_reason,'review_required':True}
    assert completion_gaps({},[],[],[pending])
    def forbidden(*args,**kwargs):
        raise AssertionError('unresolved source must not reach matching')
    outcome,_=_assess(None,pending,{},manufacturer_name='Synthetic manufacturer',matcher=forbidden,client=None)
    assert outcome.review_required
    assert 'classification_requires_complete_source' in outcome.review_reason
    assert 'pressure 12 bar' in outcome.requirement_text


@pytest.mark.parametrize('text', [
    'Synthetic equipment withstands 100 kg.',
    'Synthetic equipment withstands pressure 12 bar.'])
def test_full_quote_nontechnical_proposal_cannot_hide_product_cues(monkeypatch, text):
    from app.core.config import get_settings
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    get_settings.cache_clear()
    row=source(text)
    result,_,_,_,_=_extract_all([row],client=Client({'type':'informational','quote':text}))
    assert result[0].requirement_type is RequirementType.UNKNOWN
    assert result[0].review_required and result[0].item_id==''


@pytest.mark.parametrize('text,label,expected',[
 ('Synthetic technical clarifications allowed in 2 Days.','commercial','commercial'),
 ('Synthetic bidder past performance 80 %.','eligibility','eligibility'),
 ('Synthetic efficiency minimum 80 %.','informational','unknown')])
def test_subject_and_units_separate_administration_from_product_values(monkeypatch,text,label,expected):
    from app.core.config import get_settings
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true');get_settings.cache_clear()
    result,_,_,_,_=_extract_all([source(text)],client=Client({'type':label,'quote':text}))
    assert result[0].requirement_type.value==expected


def test_nearby_context_is_bounded_without_truncating_source(monkeypatch):
    import json
    from app.core.config import get_settings
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true')
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_MAX_CHARS','400');get_settings.cache_clear()
    row=source('Synthetic standalone form field')
    row.source_neighbors=['Synthetic long context '*100,'Synthetic short context']
    class Capture(Client):
        def complete_json(self,system,user,**kwargs):
            assert len(user)<=400
            envelope=json.loads(user)
            assert envelope['clause']==row.text
            assert envelope['context']['nearby_source_clauses']==['Synthetic short context']
            return {'type':'informational','quote':row.text}
    assert _extract_all([row],client=Capture())[0][0].requirement_type is RequirementType.INFORMATIONAL


def test_mixed_nontechnical_prose_can_be_read_without_assigning_a_product(monkeypatch):
    import json
    from app.core.config import get_settings
    from app.services.tender.completeness import completion_gaps
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_ENABLED','true');get_settings.cache_clear()
    clauses=['Synthetic delivery required within 10 days.','Synthetic bidder must provide registration.']
    text=' '.join(clauses)
    class MixedClient:
        calls=0
        def complete_json(self,system,user,**kwargs):
            self.calls+=1
            if user.startswith('{') and 'clause' in json.loads(user):
                return {'type':'unknown','quote':text}
            return {'requirements':[{'text':clause,'source_quote':clause,'type':kind,
                'attribute':None,'value':clause,'unit':None,'operator':'eq','mandatory':True}
                for clause,kind in zip(clauses,['commercial','eligibility'])]}
    client=MixedClient();result,_,_,_,_=_extract_all([source(text)],client=client)
    assert len(result)==2 and client.calls==2
    assert [r.requirement_type.value for r in result]==['commercial','eligibility']
    assert all(r.item_id=='' and r.review_required for r in result)
    assert not completion_gaps({},[],[],[vars(r) for r in result])
