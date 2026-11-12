"""Synthetic proposal contracts: exact schemas, explicit silence, no aliases."""
import json

import pytest

from app.services.match import facts
from app.services.requirement import extract


class Client:
    def __init__(self, answer, schema):
        self.answer = answer
        self.schema = schema
        self.calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        assert json.dumps(self.schema.model_json_schema(), separators=(',', ':')) in system
        return self.answer


@pytest.mark.parametrize('answer', [{}, {'requirements': [{'operation': 'eq'}]}])
def test_fact_contract_rejects_missing_envelope_and_alias_without_retry(answer):
    client = Client(answer, facts.ExtractionResponse)
    result = facts.extract_fact('Synthetic size', 'Synthetic size 25 mm', client=client)
    assert result.error and not result.facts and client.calls == 1


@pytest.mark.parametrize('answer', [{}, {'requirements': [{'text': 'Synthetic', 'operation': 'eq'}]}])
def test_requirement_contract_rejects_missing_envelope_and_alias_without_retry(answer):
    client = Client(answer, extract.ExtractionResponse)
    result = extract.extract_requirements('Synthetic size 25 mm', 'synthetic-item', client=client)
    assert result.error and result.fell_back and not result.requirements and client.calls == 1


def test_explicit_empty_response_is_distinct_from_malformed_response():
    client = Client({'requirements': []}, facts.ExtractionResponse)
    result = facts.extract_fact('Synthetic size', 'Synthetic unrelated text', client=client)
    assert not result.error and not result.facts
    client = Client({'requirements': []}, extract.ExtractionResponse)
    result = extract.extract_requirements('Synthetic informational line', 'synthetic-item', client=client)
    assert not result.error and not result.requirements


def test_batch_uses_its_own_schema(monkeypatch):
    monkeypatch.setenv('MATCH_EXTRACTION_BATCH_SIZE', '2')
    client = Client({'passages': [{'passage_id': 0, 'requirements': []}]}, facts.BatchExtractionResponse)
    results = facts.extract_fact_batch('Synthetic size', ['Synthetic text'], client=client)
    assert len(results) == 1 and not results[0].error and client.calls == 1


def test_run_contract_changes_with_fact_prompt_or_schema(monkeypatch):
    from app.services.output.runs import config_snapshot

    before = facts.contract_version()
    assert config_snapshot()['fact_prompt_version'] == before
    monkeypatch.setattr(facts, 'SYSTEM_PROMPT', facts.SYSTEM_PROMPT + '\nSynthetic change')
    assert facts.contract_version() != before
    assert config_snapshot()['fact_prompt_version'] == facts.contract_version()


def test_grouping_instructions_are_part_of_run_contract(monkeypatch):
    before = facts.contract_version()
    monkeypatch.setattr(facts, 'BATCH_INSTRUCTIONS', facts.BATCH_INSTRUCTIONS + '\nSynthetic change')
    assert facts.contract_version() != before


def test_fact_schema_change_invalidates_run_contract(monkeypatch):
    before = facts.contract_version()
    monkeypatch.setattr(facts.ExtractionResponse, 'model_json_schema',
                        lambda: {'type': 'object', 'description': 'Synthetic changed schema'})
    assert facts.contract_version() != before
