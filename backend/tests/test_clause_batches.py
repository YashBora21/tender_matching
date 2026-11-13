"""Synthetic independent clauses; never invokes an external provider."""
import json
import pytest
from app.services.llm import LLMError, LLMBudgetExceeded
from app.services.requirement.classify import classify_batch, classify_clause, parse_batch
from app.schemas.enums import RequirementType


class Cache:
    def __init__(self): self.rows = {}
    def get(self, *key): return self.rows.get(key)
    def put(self, *args): self.rows[args[:-1]] = args[-1]


class Client:
    model = 'synthetic'
    def __init__(self): self.calls = []
    def complete_json(self, system, user, **kwargs):
        payload = json.loads(user)
        self.calls.append(payload)
        answer = {'clauses': [dict(clause_id=row['clause_id'], type='technical', quote=row['text'])
                              for row in payload['clauses']]}
        kwargs['validate'](answer)
        return answer


def clauses():
    return [dict(text='Synthetic capacity 12 units', context={'source_role': 'specification'}),
            dict(text='Synthetic strength 24 units', context={'source_role': 'specification'})]


def test_two_clauses_share_one_call_but_remain_individually_cached():
    cache, client = Cache(), Client()
    result = classify_batch(clauses(), cache=cache, client=client)
    assert [row[0] for row in result] == [RequirementType.TECHNICAL] * 2
    assert len(client.calls) == 1 and len(cache.rows) == 2
    assert sum(row[1] for row in result) == 1
    assert classify_clause(clauses()[0]['text'], context=clauses()[0]['context'],
                           cache=cache, client=client)[2]
    assert all(row[2] for row in classify_batch(clauses(), cache=cache, client=client))
    assert len(client.calls) == 1


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'unknown'])
def test_batch_requires_exact_ids_and_each_own_quote(problem):
    entries = [dict(clause_id=str(i), **row) for i, row in enumerate(clauses())]
    rows = [dict(clause_id=row['clause_id'], type='technical', quote=row['text']) for row in entries]
    if problem == 'missing': rows.pop()
    if problem == 'duplicate': rows[1]['clause_id'] = '0'
    if problem == 'unknown': rows[1]['clause_id'] = '9'
    with pytest.raises(LLMError): parse_batch({'clauses': rows}, entries)


def test_a_wrong_quote_rejects_only_its_own_clause():
    entries=[dict(clause_id=str(i),**row) for i,row in enumerate(clauses())]
    answer={'clauses':[dict(clause_id=row['clause_id'],type='technical',quote=row['text']) for row in entries]}
    answer['clauses'][1]['quote']=answer['clauses'][0]['quote']
    found,failed=parse_batch(answer,entries)
    assert list(found)==['0'] and failed=={'1':'invalid_source_quote'}


def test_duplicate_clauses_keep_separate_results_and_cannot_get_conflicting_types():
    entry = dict(text='Synthetic capacity 12 units', context={})
    entries = [dict(clause_id=str(i), **entry) for i in range(2)]
    rows = [dict(clause_id=str(i), type=label, quote=entry['text'])
            for i, label in enumerate(['technical', 'certification'])]
    with pytest.raises(LLMError, match='conflicting_clause_classification'):
        parse_batch({'clauses': rows}, entries)


def test_requests_split_at_configured_bound(monkeypatch):
    from app.core.config import get_settings
    rows = [dict(text='Synthetic ' + str(i) + ' ' + 'x' * 100, context={}) for i in range(6)]
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_MAX_CHARS', '500')
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_BATCH_CHARS', '500')
    get_settings.cache_clear()
    client = Client()
    result = classify_batch(rows, client=client)
    assert len(result) == 6 and len(client.calls) > 1
    assert all(len(json.dumps(payload, ensure_ascii=False)) <= 500 for payload in client.calls)
    get_settings.cache_clear()


def test_eight_independent_contexts_share_a_request_without_changing_any_input(monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_BATCH_SIZE', '8')
    monkeypatch.setenv('REQUIREMENT_CLASSIFICATION_BATCH_CHARS', '24000')
    get_settings.cache_clear()
    rows = [dict(text=f'Synthetic property {i}', context={'section':f'Synthetic section {i}',
            'source_role':'specification','nearby_source_clauses':['Synthetic context '*30]}) for i in range(24)]
    client = Client()
    result = classify_batch(rows, client=client)
    assert len(client.calls) == 3 and len(result) == 24
    sent = [entry for payload in client.calls for entry in payload['clauses']]
    assert [(r['text'],r['context']) for r in sent] == [(r['text'],r['context']) for r in rows]
    assert all(r[0] is RequirementType.TECHNICAL for r in result)
    get_settings.cache_clear()


def test_budget_stop_propagates_without_empty_success():
    class Limited(Client):
        def complete_json(self, *args, **kwargs): raise LLMBudgetExceeded('synthetic')
    with pytest.raises(LLMBudgetExceeded): classify_batch(clauses(), client=Limited())
