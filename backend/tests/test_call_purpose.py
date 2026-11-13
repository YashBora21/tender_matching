"""Synthetic HTTP responses verify purpose metadata without provider calls."""
import io,json
import pytest
from app.services.llm import LLMClient,LLMError


@pytest.mark.parametrize('purpose',['extraction','classification','matching'])
def test_shared_client_records_stage_then_repair_and_restores_default(monkeypatch,purpose):
    monkeypatch.setenv('LLM_MODEL','synthetic')
    monkeypatch.setenv('LLM_API_KEY','synthetic-key')
    events=[]
    class Ledger:
        def check(self):pass
        def reserve(self,**kwargs):
            events.append(kwargs['purpose']);return len(events)
        def settle(self,*args,**kwargs):pass
    def respond(*args,**kwargs):
        return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"synthetic":true}'}}],
            'usage':{'prompt_tokens':2,'completion_tokens':1}}).encode())
    monkeypatch.setattr('app.services.llm.urllib.request.urlopen',respond)
    client=LLMClient(purpose='extraction');client.ledger=Ledger()
    checked=0
    def validate(value):
        nonlocal checked
        checked+=1
        if checked==1:raise LLMError('invalid_source_quote')
    client.complete_json('Synthetic source','Synthetic data',validate=validate,purpose=purpose)
    assert events==[purpose,'repair'] and client.retries==1
    assert client.purpose=='extraction'
