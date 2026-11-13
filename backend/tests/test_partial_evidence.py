"""Synthetic five-field and multi-clause replies; no provider calls."""
import json
import pytest
from app.services.llm import LLMClient,LLMResult,LLMBudgetExceeded
from app.services.requirement.extract import extract_requirements
from app.services.requirement.classify import classify_batch
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all

SOURCE='Synthetic row 1: size 10 mm; pressure 8 bar; material Steel; class 150; quantity 1'

def five_fields():
    rows=[]
    for attribute,value,unit,quote in [('nominal_size','10','mm','size 10 mm'),
            ('operating_pressure','8','bar','pressure 8 bar'),('material_grade','Steel',None,'material Steel'),
            ('pressure_class','150',None,'class 150'),('Synthetic sequence','1',None,'1')]:
        rows.append(dict(text=quote,source_quote=quote,attribute=attribute,value=value,
                         unit=unit,type='technical' if len(rows)<4 else 'informational',operator='eq'))
    return {'requirements':rows}

def client(monkeypatch,answer,*,stop_repair=False):
    monkeypatch.setenv('LLM_MODEL','synthetic')
    monkeypatch.setenv('LLM_API_KEY','synthetic-key')
    value=LLMClient()
    def chat(*args,**kwargs):
        if stop_repair and value.calls:
            raise LLMBudgetExceeded('Synthetic call limit')
        value.calls+=1
        return LLMResult(content=json.dumps(answer),prompt_tokens=2,completion_tokens=1)
    monkeypatch.setattr(value,'chat',chat)
    return value

def test_five_fields_keep_four_verified_fields_after_one_failed_repair(monkeypatch):
    reader=client(monkeypatch,five_fields())
    result=extract_requirements(SOURCE,'synthetic',client=reader)
    assert len(result.requirements)==4 and len(result.rejected)==1
    assert reader.calls==2 and reader.retries==1
    assert all(row.source_span for row in result.requirements)
    assert result.rejected_fields[0]['verification']=='rejected'

def test_pipeline_does_not_discard_the_four_good_fields(monkeypatch):
    reader=client(monkeypatch,five_fields())
    rows,_,_,limited,_=_extract_all([RequirementRecord(text=SOURCE,item_id='synthetic')],client=reader)
    assert not limited and len(rows)==4
    assert all(row.item_id=='synthetic' and row.source_span for row in rows)

def test_budget_before_repair_keeps_good_fields_and_unread_tail(monkeypatch):
    reader=client(monkeypatch,five_fields(),stop_repair=True)
    tail=RequirementRecord(text='Synthetic unread next row',item_id='next')
    rows,_,_,limited,reason=_extract_all([RequirementRecord(text=SOURCE,item_id='synthetic'),tail],client=reader)
    assert limited and 'Synthetic call limit' in reason
    assert len([r for r in rows if r.item_id=='synthetic'])==4
    assert rows[-1] is tail and reader.calls==1

class Cache:
    def __init__(self):self.rows={}
    def get(self,*key):return self.rows.get(key)
    def put(self,*args):self.rows[args[:-1]]=args[-1]

def test_bad_clause_keeps_other_seven_and_only_valid_answers_are_cached(monkeypatch):
    inputs=[dict(text=f'Synthetic property {i}',context={}) for i in range(8)]
    answer={'clauses':[dict(clause_id=str(i),type='technical',quote=row['text']) for i,row in enumerate(inputs)]}
    answer['clauses'][-1]['quote']='Synthetic absent quote'
    reader=client(monkeypatch,answer);cache=Cache()
    result=classify_batch(inputs,client=reader,cache=cache)
    assert all(row[0].value=='technical' for row in result[:-1])
    assert result[-1][0] is None and result[-1][3]=='invalid_source_quote'
    assert len(cache.rows)==7 and reader.calls==2 and reader.retries==1
