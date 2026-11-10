"""Synthetic quote contracts: exact provenance, ambiguity and persistence."""
import pytest
from app.services.requirement.evidence import quote_span
from app.services.requirement.extract import extract_requirements


class Client:
    calls = 0
    def __init__(self, answer): self.answer = answer
    def complete_json(self, *args):
        self.calls += 1
        return self.answer


def answer(quote, **changes):
    row = dict(text='Synthetic display paraphrase', source_quote=quote, type='technical',
               attribute='size', value='25', unit='mm', operator='eq')
    row.update(changes)
    return {'requirements': [row]}


@pytest.mark.parametrize('text,quote,expected', [
    ('Synthetic size 25 mm', 'size 25 mm', (10, 20)),
    ('Synthetic Ω size 25 mm', 'Ω size 25 mm', (10, 22)),
    ('Synthetic 😀 size 25 mm', 'size 25 mm', (12, 22)),
    ('Synthetic size 25 mm', 'SIZE 25 mm', None),
    ('Synthetic size 25 mm', 'size  25 mm', None),
    ('Synthetic size 25 mm; size 25 mm', 'size 25 mm', None),
    ('aaaa', 'aaa', None),
    ('Synthetic source', '', None),
    ('Synthetic source', '  ', None),
    ('Synthetic source', None, None),
    ('Synthetic source', 'not in source', None),
])
def test_quote_location_is_exact_unique_and_unicode_safe(text, quote, expected):
    assert quote_span(text, quote) == expected


def test_valid_quote_derives_offsets_and_retains_item_page_and_source_text():
    text = 'Synthetic context; size 25 mm and pressure 80 bar'
    quote = 'size 25 mm'
    client = Client(answer(quote))
    result = extract_requirements(text, 'synthetic-item', page=3, section='synthetic-section',
                                  extraction_method='ocr', client=client)
    assert not result.error and not result.rejected and len(result.requirements) == 1
    row = result.requirements[0]
    assert row.source_span == (text.index(quote), text.index(quote) + len(quote))
    assert row.source_text[row.source_span[0]:row.source_span[1]] == quote
    assert (row.item_id, row.page, row.source_section, row.extraction_method) == (
        'synthetic-item', 3, 'synthetic-section', 'ocr')
    assert client.calls == 1


@pytest.mark.parametrize('text,quote,value', [
    ('Synthetic size 25 mm', 'size 26 mm', '26'),
    ('Synthetic size 25 mm; size 25 mm', 'size 25 mm', '25'),
    ('Synthetic size 25 mm and pressure 80 bar', 'pressure 80 bar', '25'),
    ('Synthetic size 12–18 mm', '12', '12'),
    ('Synthetic material steels', 'material steels', 'steel'),
])
def test_missing_ambiguous_or_unsupported_quote_is_rejected_without_retry(text, quote, value):
    client = Client(answer(quote, value=value))
    result = extract_requirements(text, 'synthetic-item', client=client)
    assert result.rejected and not result.requirements and client.calls == 1


def test_legacy_offsets_are_not_reinterpreted_as_an_authoritative_quote():
    old = answer('Synthetic size 25 mm')['requirements'][0]
    del old['source_quote']
    old['span'] = [0, 20]
    result = extract_requirements('Synthetic size 25 mm', 'synthetic-item', client=Client({'requirements': [old]}))
    assert result.fell_back and result.error and not result.requirements


def test_mixed_legacy_offsets_and_quote_are_rejected_instead_of_ignored():
    result = extract_requirements('Synthetic size 25 mm', 'synthetic-item',
        client=Client(answer('Synthetic size 25 mm', span=[0, 1])))
    assert result.fell_back and result.error and not result.requirements


def test_rejected_quote_preserves_original_clause_and_does_not_cache_partial_output():
    from app.services.tender.pipeline import _extract_all
    from app.services.tender.ingest import RequirementRecord
    text = 'size 25 mm and pressure 80 bar'
    payload = answer('size 25 mm')
    payload['requirements'] += answer('pressure 80 bar')['requirements']
    class Cache:
        def get(self, *args): return None
        def put(self, *args): raise AssertionError('partial invalid quote response must not be cached')
    rows, *_ = _extract_all([RequirementRecord(text=text, item_id='synthetic-item')],
                            client=Client(payload), cache=Cache())
    assert len(rows) == 1 and rows[0].text == rows[0].source_text == text
    assert rows[0].review_required and rows[0].item_id == 'synthetic-item'


def test_unresolved_unit_is_preserved_with_review_instead_of_guessed():
    text = 'Synthetic size 25 fictionalunits'
    result = extract_requirements(text, 'synthetic-item',
        client=Client(answer(text, unit='fictionalunits')))
    row = result.requirements[0]
    assert row.unit == 'fictionalunits' and row.review_required
    assert 'unit' in row.review_reason
    from app.services.tender.pipeline import _as_record, _as_dict
    cached = _as_record(_as_dict(row))
    assert cached.review_required and cached.review_reason == row.review_reason


def test_full_range_is_kept_and_code_derives_its_source_span():
    text = 'Synthetic size 12–18 mm'
    result = extract_requirements(text, 'synthetic-item', client=Client(answer(text, value='12–18')))
    assert result.requirements[0].value == '12–18'
    assert result.requirements[0].source_span == (0, len(text))


def test_derived_offsets_survive_structured_cache_and_storage_conversion():
    from app.services.tender.pipeline import _extract_all, to_stored, _stored_records
    from app.services.tender.ingest import RequirementRecord
    text = 'size 25 mm'
    class Cache:
        entries = {}
        def get(self, *key): return self.entries.get(key)
        def put(self, *args): self.entries[args[:-1]] = args[-1]
    client, cache = Client(answer(text)), Cache()
    first, *_ = _extract_all([RequirementRecord(text=text, item_id='synthetic-item')], client=client, cache=cache)
    second, *_ = _extract_all([RequirementRecord(text=text, item_id='other-item')], client=client, cache=cache)
    assert client.calls == 1 and first[0].source_span == second[0].source_span == (0, len(text))
    assert second[0].item_id == 'other-item'
    stored = to_stored(second[0], 'synthetic-tender')
    assert stored.source_text[stored.source_span[0]:stored.source_span[1]] == text
    class Repo:
        def list_requirements(self, *args):
            return [{'text': stored.text, 'source_text': stored.source_text,
                'source_span': list(stored.source_span), 'item_id': stored.item_id, 'page': 1}]
    assert _stored_records(Repo(), 'synthetic-tender')[0].source_span == stored.source_span


def test_legacy_prompt_cache_is_not_reused_for_the_quote_contract():
    from app.services.tender.pipeline import _extract_all
    from app.services.tender.ingest import RequirementRecord
    text = 'size 25 mm'
    class Cache:
        entries = {}
        def get(self, *key):
            assert key[2] != '4', 'legacy offset prompt must not be reused'
            return self.entries.get(key)
        def put(self, *args): self.entries[args[:-1]] = args[-1]
    client, cache = Client(answer(text)), Cache()
    for _ in range(2):
        rows, *_ = _extract_all([RequirementRecord(text=text, item_id='synthetic-item')],
                                client=client, cache=cache)
        assert rows[0].value == '25' and rows[0].source_span == (0, len(text))
    assert client.calls == 1


def test_budget_stop_preserves_unread_source_as_resumable_work():
    from app.services.llm import LLMBudgetExceeded
    from app.services.tender.pipeline import _extract_all
    from app.services.tender.ingest import RequirementRecord
    text = 'Synthetic size 25 mm'
    class Budget:
        def complete_json(self, *args): raise LLMBudgetExceeded('synthetic call budget stop')
    rows, _, _, limited, detail = _extract_all(
        [RequirementRecord(text=text, item_id='synthetic-item')], client=Budget())
    assert limited and 'synthetic call budget stop' in detail
    assert len(rows) == 1 and rows[0].text == text and rows[0].item_id == 'synthetic-item'


def test_repository_writes_and_returns_existing_source_span_column_without_live_db():
    from app.repositories.tender import TenderRepo
    from app.services.tender.pipeline import to_stored
    from app.services.tender.ingest import RequirementRecord
    source = 'Synthetic size 25 mm'
    record = to_stored(RequirementRecord(text='Synthetic paraphrase', source_text=source,
        source_span=(10, len(source)), item_id='synthetic-item'), 'synthetic-tender')
    class Session:
        written = None
        def execute(self, statement, params=None):
            if params is not None: self.written = params
            class Rows:
                def all(self):
                    values = dict(id='synthetic-id', item_id='synthetic-item', text=record.text,
                        source_text=source, source_page=1, source_section=None, extraction_method='table',
                        type='technical', attribute=None, review_required=False, review_reason=None,
                        value=None, unit=None, operator=None, extraction_confidence=None, mandatory=True,
                        source_table_id=None, source_row_id=None, prompt_version='synthetic-version',
                        source_span=[10, len(source)], normalization=None)
                    return [tuple(values[column.name] for column in statement.selected_columns)]
            return Rows()
    session = Session()
    repo = TenderRepo(session)
    repo._supersede_requirements = lambda *args: None
    repo.replace_requirements('synthetic-tender', [record])
    assert session.written[0]['source_span'] == [10, len(source)]
    assert repo.list_requirements('synthetic-tender')[0]['source_span'] == record.source_span
