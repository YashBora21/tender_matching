"""Synthetic independent clauses; never invokes an external provider."""
import json

import pytest

from app.core.config import get_settings
from app.schemas.enums import RequirementType
from app.services.llm import LLMError, LLMBudgetExceeded
from app.services.requirement.extract import extract_batch, _parse_batch_schema


def clause(text, item='synthetic-item'):
    return {'text': text, 'item_id': item, 'page': 1, 'section': 'Synthetic section',
            'extraction_method': 'prose', 'table_context': None}


def answer_for(payload):
    rows = []
    for row in payload['clauses']:
        src = row['source_text']
        rows.append({'clause_id': row['clause_id'], 'requirements': [{
            'text': f'read: {src}', 'source_quote': src, 'type': 'technical',
            'attribute': 'body material', 'operator': 'eq', 'value': src,
            'unit': None, 'mandatory': True}]})
    return {'clauses': rows}


class Client:
    model = 'synthetic'

    def __init__(self):
        self.calls = []

    def complete_json(self, system, user, **kwargs):
        payload = json.loads(user)
        self.calls.append(payload)
        answer = answer_for(payload)
        kwargs['validate'](answer)
        return answer


class Cache:
    def __init__(self):
        self.rows = {}

    def get(self, *key):
        return self.rows.get(key)

    def put(self, *args):
        self.rows[args[:-1]] = args[-1]


def batch_on(monkeypatch, size='8'):
    monkeypatch.setenv('REQUIREMENT_EXTRACTION_BATCH_SIZE', size)
    get_settings.cache_clear()


def test_two_clauses_share_one_call_and_keep_their_identity(monkeypatch):
    batch_on(monkeypatch)
    client = Client()
    sources = ['Synthetic capacity 12 units', 'Synthetic strength 24 units']
    results = extract_batch([clause(text) for text in sources], client=client)
    assert len(client.calls) == 1 and len(results) == 2
    for result, src in zip(results, sources):
        assert len(result.requirements) == 1
        record = result.requirements[0]
        assert record.item_id == 'synthetic-item'
        assert record.value == src and record.source_text == src
        assert record.requirement_type is RequirementType.TECHNICAL
    get_settings.cache_clear()


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'unknown'])
def test_batch_requires_exact_ids(problem):
    entries = [{'clause_id': str(i), 'source_text': f'Synthetic clause {i}'} for i in range(2)]
    answer = answer_for({'clauses': entries})
    if problem == 'missing':
        answer['clauses'].pop()
    if problem == 'duplicate':
        answer['clauses'][1]['clause_id'] = '0'
    if problem == 'unknown':
        answer['clauses'][1]['clause_id'] = '9'
    with pytest.raises(LLMError):
        _parse_batch_schema(answer, {entry['clause_id']: entry for entry in entries})


def test_a_wrong_quote_rejects_only_its_own_clause(monkeypatch):
    batch_on(monkeypatch)

    class WrongQuote(Client):
        def complete_json(self, system, user, **kwargs):
            payload = json.loads(user)
            self.calls.append(payload)
            answer = answer_for(payload)
            answer['clauses'][1]['requirements'][0]['source_quote'] = payload['clauses'][0]['source_text']
            kwargs['validate'](answer)
            return answer

    results = extract_batch([clause('Synthetic capacity 12 units'),
                             clause('Synthetic strength 24 units')], client=WrongQuote())
    assert len(results[0].requirements) == 1
    assert results[1].requirements == [] and results[1].error
    get_settings.cache_clear()


def test_a_wrong_quote_gets_one_bounded_repair(monkeypatch):
    batch_on(monkeypatch)

    class Repair(Client):
        def complete_json(self, system, user, **kwargs):
            payload = json.loads(user)
            for attempt in range(2):
                self.calls.append(payload)
                answer = answer_for(payload)
                if attempt == 0:
                    answer['clauses'][1]['requirements'][0]['source_quote'] = 'not in the source'
                try:
                    kwargs['validate'](answer)
                    return answer
                except LLMError:
                    if attempt == 1:
                        raise

    client = Repair()
    results = extract_batch([clause('Synthetic capacity 12 units'),
                             clause('Synthetic strength 24 units')], client=client)
    assert len(client.calls) == 2
    assert all(len(result.requirements) == 1 and not result.rejected for result in results)
    get_settings.cache_clear()


def test_size_one_reads_one_clause_per_call(monkeypatch):
    batch_on(monkeypatch, '1')
    client = Client()
    results = extract_batch([clause('Synthetic capacity 12 units'),
                             clause('Synthetic strength 24 units')], client=client)
    assert len(client.calls) == 2
    assert all(len(result.requirements) == 1 for result in results)
    get_settings.cache_clear()


def test_requests_split_at_the_char_bound(monkeypatch):
    batch_on(monkeypatch)
    monkeypatch.setenv('REQUIREMENT_EXTRACTION_BATCH_CHARS', '400')
    get_settings.cache_clear()
    client = Client()
    results = extract_batch([clause('Synthetic ' + str(i) + ' ' + 'x' * 100) for i in range(6)],
                            client=client)
    assert len(results) == 6 and len(client.calls) > 1
    assert all(len(json.dumps(payload, ensure_ascii=False)) <= 400 for payload in client.calls)
    get_settings.cache_clear()


def test_budget_stop_propagates_without_empty_success(monkeypatch):
    batch_on(monkeypatch)

    class Limited(Client):
        def complete_json(self, *args, **kwargs):
            raise LLMBudgetExceeded('synthetic')

    with pytest.raises(LLMBudgetExceeded):
        extract_batch([clause('Synthetic capacity 12 units'),
                       clause('Synthetic strength 24 units')], client=Limited())
    get_settings.cache_clear()


class PipelineClient:
    model = 'synthetic'

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        envelope = json.loads(user) if user.startswith('{') else {'source_text': user}
        if 'clauses' in envelope:
            answer = answer_for(envelope)
        else:
            src = envelope.get('source_text', user)
            answer = {'requirements': [{'text': f'read: {src}', 'source_quote': src,
                'type': 'technical', 'attribute': 'body material', 'operator': 'eq',
                'value': src, 'unit': None, 'mandatory': True}]}
        if kwargs.get('validate') is not None:
            kwargs['validate'](answer)
        return answer


def spec_rows(count):
    from app.services.tender.ingest import RequirementRecord
    return [RequirementRecord(text=f'Synthetic property {i} value {i}', item_id='synthetic-item',
                              source_role='specification', candidate_type='technical')
            for i in range(count)]


def test_pipeline_batches_spec_rows_into_one_call(monkeypatch):
    from app.services.tender.pipeline import _extract_all
    batch_on(monkeypatch)
    client = PipelineClient()
    output, calls, _, limited, _ = _extract_all(spec_rows(6), client=client)
    assert not limited and calls == 1
    assert len(output) == 6
    assert all(row.requirement_type is RequirementType.TECHNICAL for row in output)
    get_settings.cache_clear()


def test_cached_rows_are_not_read_again(monkeypatch):
    from app.services.tender.pipeline import _extract_all
    batch_on(monkeypatch)
    cache = Cache()
    rows = spec_rows(4)
    _extract_all(rows, client=PipelineClient(), cache=cache)
    client = PipelineClient()
    output, calls, hits, _, _ = _extract_all(rows, client=client, cache=cache)
    assert client.calls == 0 and calls == 0 and hits == 4
    assert len(output) == 4
    get_settings.cache_clear()


def test_context_rows_stay_out_of_the_batch(monkeypatch):
    from app.services.tender.pipeline import _extract_all
    from app.services.tender.ingest import RequirementRecord
    batch_on(monkeypatch)
    context = RequirementRecord(text='Synthetic cover note', item_id='',
                                source_role='cover', candidate_type=None)
    client = PipelineClient()
    output, calls, _, limited, _ = _extract_all(spec_rows(3) + [context], client=client)
    assert not limited and calls == 1
    assert len(output) == 4
    get_settings.cache_clear()