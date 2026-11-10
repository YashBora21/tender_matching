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
    def complete_json(self,system,user):
        self.calls+=1
        return {'requirements':[] if self.empty else [{'text':user,'source_quote':user,'type':'technical',
                'attribute':'size','operator':'eq','value':'25','unit':'mm','mandatory':True}]}
class Cache:
    def __init__(self):self.data={}
    def get(self,*key):return self.data.get(key)
    def put(self,*args):self.data[args[:-1]]=args[-1]


def test_cache_hits_keep_source_facet_gaps_and_separate_item_identity():
    cache=Cache();client=Client();text='size 25 mm surface condition ZETA'
    for item in ('1','2'):
        rows,_,_,_,_=_extract_all([RequirementRecord(text=text,item_id=item)],cache=cache,client=client)
        assert rows[0].item_id==item and rows[0].source_text==text
        assert rows[0].review_required and MARKER in rows[0].review_reason
        assert completion_gaps({},[],[],[vars(rows[0])])
    assert client.calls==1


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
        def complete_json(self,system,user):
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
