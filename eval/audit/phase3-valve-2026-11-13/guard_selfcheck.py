"""Synthetic money/ledger self-check; no model, network or database."""
import ast, json, os, time, uuid
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
source = ast.parse(Path('.audit_tmp/phase3_read.py').read_text(encoding='utf-8'))
nodes = [n for n in source.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in
         {'append','spent','MeasuredClient'}]
class Budget(Exception): pass
class FakeClient:
    calls = 0
    model = 'synthetic'
    fail = False
    def _chat_once(self,*args,**kwargs):
        self.calls += 1
        if self.fail: raise RuntimeError('synthetic request failure')
        return SimpleNamespace(prompt_tokens=2,completion_tokens=1)
class Insert:
    def values(self,**kwargs): return kwargs
folder=Path('.audit_tmp')/('phase3-driver-selfcheck-'+uuid.uuid4().hex)
folder.mkdir()
scope=dict(json=json,os=os,time=time,uuid=uuid,Decimal=Decimal,
           journal=folder/'calls.jsonl',LLMClient=FakeClient,LLMBudgetExceeded=Budget,
           settings=SimpleNamespace(llm_max_tokens=3),price_in=Decimal('.01'),price_out=Decimal('.02'),
           spend_cap=Decimal('1'),mode='synthetic',llm_spend=SimpleNamespace(insert=Insert),
           session_scope=lambda:nullcontext(SimpleNamespace(execute=lambda query:None)))
exec(compile(ast.Module(body=nodes,type_ignores=[]),'<synthetic-money-check>','exec'),scope)
client=scope['MeasuredClient']()
try: client._chat_once('s','u')
except Budget: pass
else: raise AssertionError('A call exceeding the cap was allowed')
assert client.calls == 0 and not scope['journal'].exists()
scope['spend_cap']=Decimal('100')
client._chat_once('s','u')
assert scope['spent']() == Decimal('.04')
scope['append']({'id':'pending','reserved_inr':'100'})
try: client._chat_once('s','u')
except Budget: pass
else: raise AssertionError('An unclosed reservation was ignored')
assert client.calls == 1
scope['journal']=folder/'uncertain.jsonl'
client.fail=True
try: client._chat_once('s','u')
except RuntimeError: pass
else: raise AssertionError('The synthetic failure was swallowed')
assert scope['spent']() == Decimal('5.2')
print('Four synthetic driver checks passed: pre-call refusal, actual cost, pending reservation, uncertain-call reservation')
