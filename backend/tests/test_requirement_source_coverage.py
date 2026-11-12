"""Synthetic facets across unrelated domains; no vendor facts or provider calls."""
import pytest
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all, to_stored
from app.services.requirement.coverage import uncovered_tokens, MARKER
from app.services.tender.completeness import completion_gaps
from app.schemas.enums import RequirementType


def fact(attribute,value,unit=None,text='synthetic paraphrase'):
    return RequirementRecord(text=text,item_id='1',attribute=attribute,value=value,unit=unit)


@pytest.mark.parametrize('text,attribute,value,unit,miss',[
 ('size 25 mm surface condition ZETA','size','25','mm','zeta'),
 ('voltage 24 V insulation category OMEGA','voltage','24','V','omega'),
 ('retention 90 days encryption ALPHA','retention','90','days','alpha'),
 ('flow 12 LPM mounting arrangement BETA','flow','12','LPM','beta')])
def test_paraphrase_and_broad_span_cannot_cover_unrepresented_facets(text,attribute,value,unit,miss):
    extracted=fact(attribute,value,unit,text=text)
    extracted.source_span=(0,len(text))
    assert miss in uncovered_tokens(text,[extracted])


def test_distinct_structured_facts_cover_an_atomic_source_without_inventing_values():
    assert uncovered_tokens('pressure 12 bar and temperature 40 C',
                            [fact('pressure','12','bar'),fact('temperature','40','C')])==[]


class Client:
    def __init__(self,empty=False):self.calls=0;self.empty=empty
    def complete_json(self,system,user, **kwargs):
        self.calls+=1
        return {'requirements':[] if self.empty else [{'text':user,'source_quote':user,'type':'technical',
                'attribute':'size','operator':'eq','value':'25','unit':'mm','mandatory':True}]}
class Cache:
    def __init__(self):self.data={}
    def get(self,*key):return self.data.get(key)
    def put(self,*args):self.data[args[:-1]]=args[-1]


def test_incomplete_facets_are_not_cached_and_item_identity_is_preserved():
    cache=Cache();client=Client();text='size 25 mm surface condition ZETA'
    for item in ('1','2'):
        rows,_,_,_,_=_extract_all([RequirementRecord(text=text,item_id=item)],cache=cache,client=client)
        assert rows[0].item_id==item and rows[0].source_text==text
        assert rows[0].review_required and MARKER in rows[0].review_reason
        assert completion_gaps({},[],[],[vars(rows[0])])
    assert client.calls==2 and not cache.data


def test_empty_output_retains_the_whole_source_as_unknown():
    text='Synthetic nonnumeric condition ZETA'
    rows,_,_,_,_=_extract_all([RequirementRecord(text=text,item_id='1')],client=Client(empty=True))
    assert len(rows)==1 and rows[0].text==text and rows[0].source_text==text
    assert rows[0].requirement_type is RequirementType.UNKNOWN
    assert MARKER in rows[0].review_reason


def test_persistence_uses_original_source_not_the_paraphrase():
    row=fact('size','25','mm',text='Synthetic paraphrase')
    row.source_text='Synthetic original size 25 mm with additional condition ZETA'
    stored=to_stored(row,'synthetic-tender',model='synthetic',prompt_version='synthetic',input_hash='synthetic')
    assert stored.source_text==row.source_text
    assert stored.text=='Synthetic paraphrase'


def test_unapproved_attribute_cannot_launder_an_unassessed_source_facet():
    row=fact('synthetic surface condition size','25','mm',text='size 25 mm surface condition')
    assert 'surface' in uncovered_tokens('size 25 mm surface condition',[row])


def test_numeric_value_does_not_cover_an_unassessed_connection_in_attribute_name():
    row=fact('synthetic end arrangement size','25','mm')
    assert 'arrangement' in uncovered_tokens('size 25 mm end arrangement ZETA',[row])


def test_refresh_reads_one_original_source_not_each_generated_paraphrase():
    class CompoundClient(Client):
        def complete_json(self,system,user, **kwargs):
            self.calls+=1
            assert user=='pressure 12 bar and temperature 40 C'
            return {'requirements':[{'text':'Synthetic pressure paraphrase','source_quote':user,
                'type':'technical','attribute':'pressure','operator':'eq','value':'12','unit':'bar','mandatory':True},
                {'text':'Synthetic temperature paraphrase','source_quote':user,
                'type':'technical','attribute':'temperature','operator':'eq','value':'40','unit':'C','mandatory':True}]}
    original=RequirementRecord(text='pressure 12 bar and temperature 40 C',item_id='1',
                               source_table_id='synthetic-source',source_row_id='synthetic-row')
    client=CompoundClient();cache=Cache()
    first,_,_,_,_=_extract_all([original],client=client,cache=cache)
    second,_,_,_,_=_extract_all(first,client=client,cache=cache)
    assert len(first)==len(second)==2 and client.calls==1
    assert all(r.source_text==original.text for r in second)


@pytest.mark.parametrize('text',['Synthetic unreadable source \ufffd','अस्पष्ट पाठ'])
def test_unreadable_clause_remains_a_gap_even_beside_valid_facts(text):
    class NeverCall:
        def complete_json(self,*a,**kw):raise AssertionError('unreadable text must not be sent')
    rows,_,_,_,_=_extract_all([RequirementRecord(text=text,item_id='1')],client=NeverCall())
    assert len(rows)==1 and rows[0].source_text==text
    assert MARKER in rows[0].review_reason
    assert completion_gaps({},[],[],[vars(fact('pressure','12','bar')),vars(rows[0])])


def test_legacy_incomplete_cached_answer_is_not_reused():
    class LegacyCache(Cache):
        def get(self, *key):
            return [{'text':'size 25 mm','item_id':'1','type':'technical',
                     'attribute':'size','operator':'eq','value':'25','unit':'mm','mandatory':True}]
    client=Client()
    _,calls,hits,_,_=_extract_all([RequirementRecord(
        text='size 25 mm surface condition ZETA',item_id='1')],cache=LegacyCache(),client=client)
    assert calls==1 and hits==0 and client.calls==1


def test_quoted_open_vocabulary_value_is_read_without_approving_the_alias():
    from app.services.requirement.structured_values import normalize_fields
    text='Synthetic identity ABC-X-42'
    record=fact('Synthetic identity','ABC-X-42')
    record.source_span=(0,len(text))
    assert '42' not in uncovered_tokens(text,[record])
    assert normalize_fields(record.attribute,record.value,None)['review_required']


def test_table_context_survives_extraction_cache_and_storage():
    """Synthetic source: headers affect the answer/cache, not just the display."""
    import json
    from app.services.tender.pipeline import _as_record
    context = {'headers': ['Synthetic identifier', 'Qty'], 'cells': ['SYN-X-42', '7']}
    text = 'SYN-X-42 7'

    class TableClient:
        calls = 0

        def complete_json(self, system, user, **kwargs):
            self.calls += 1
            envelope = json.loads(user)
            assert envelope == {'source_text': text, 'table': context}
            return {'requirements': [
                {'text': value, 'source_quote': value, 'type': kind,
                 'attribute': header, 'operator': 'eq', 'value': value,
                 'unit': None, 'mandatory': True}
                for header, value, kind in zip(context['headers'], context['cells'],
                                               ('informational', 'commercial'))]}

    client, cache = TableClient(), Cache()
    contexts = (context, context,
                dict(context, headers=['Synthetic alternate identifier', 'Qty']))
    for context, expected_calls in zip(contexts, (1, 1, 2)):
        source = RequirementRecord(text=text, item_id='synthetic', table_context=context)
        records, _, _, limited, _ = _extract_all([source], client=client, cache=cache)
        assert not limited and client.calls == expected_calls
        assert all(row.table_context == context for row in records)
        assert all(_as_record(value).table_context == context
                   for value in list(cache.data.values())[-1])
        stored = to_stored(records[0], 'synthetic-tender', model='synthetic',
                           prompt_version='synthetic', input_hash='synthetic')
        assert stored.normalization['source_context']['table'] == context


def test_nearby_source_context_survives_storage_and_cache():
    from app.services.tender.pipeline import _as_dict, _as_record
    row=fact('Synthetic field','Synthetic value')
    row.source_neighbors=['Synthetic preceding sentence.','Synthetic following sentence.']
    assert _as_record(_as_dict(row)).source_neighbors==row.source_neighbors
    stored=to_stored(row,'synthetic',model='synthetic',prompt_version='synthetic',input_hash='synthetic')
    assert stored.normalization['source_context']['nearby_source_clauses']==row.source_neighbors


@pytest.mark.parametrize('value,accepted',[('7',True),('42',False)])
def test_full_row_quote_cannot_borrow_value_from_another_column(value, accepted):
    from app.services.requirement.extract import extract_requirements
    class TableClient:
        def complete_json(self,system,user, **kwargs):
            return {'requirements':[{'text':'Synthetic quantity','source_quote':'42 7',
                'attribute':'Qty','value':value,'unit':None,'type':'commercial',
                'operator':'eq','mandatory':True}]}
    outcome=extract_requirements('42 7','synthetic',client=TableClient(),
        table_context={'headers':['Synthetic part','Qty'],'cells':['42','7']})
    assert bool(outcome.requirements)==accepted
    assert bool(outcome.rejected)!=accepted
