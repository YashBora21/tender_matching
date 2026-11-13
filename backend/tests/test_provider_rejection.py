"""Synthetic provider errors must not masquerade as unreadable tender clauses."""
import urllib.error
import pytest
from app.services.llm import LLMClient, LLMRequestRejected
from app.services.tender.pipeline import _extract_all
from app.services.tender.ingest import RequirementRecord
from app.schemas.enums import RequirementType
from app.services.requirement.classify import classify_batch


@pytest.mark.parametrize('status', [400, 401, 402, 403, 404, 413, 422])
def test_permanent_http_rejection_is_not_retried(monkeypatch, status):
    client = LLMClient(model='synthetic', api_key='synthetic',
                       base_url='https://synthetic-llm.example.test/v1')
    def reject(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, status, 'synthetic', {}, None)
    monkeypatch.setattr('app.services.llm.urllib.request.urlopen', reject)
    with pytest.raises(LLMRequestRejected) as error:
        client.complete_json('synthetic', 'synthetic')
    assert error.value.status == status
    assert client.calls == 1 and client.retries == 0


class RejectedClient:
    def __init__(self): self.calls = 0
    def complete_json(self, *args, **kwargs):
        self.calls += 1
        raise LLMRequestRejected(400)


def test_provider_failure_stops_before_the_next_requirement():
    rows = [RequirementRecord(text='Synthetic operating pressure 8 bar', item_id=str(i),
            requirement_type=RequirementType.TECHNICAL) for i in range(2)]
    client = RejectedClient()
    with pytest.raises(LLMRequestRejected): _extract_all(rows, client=client)
    assert client.calls == 1


def test_provider_failure_does_not_become_a_batch_classification_review():
    client = RejectedClient()
    with pytest.raises(LLMRequestRejected):
        classify_batch([{'text': 'Synthetic pressure 8 bar', 'context': {}}], client=client)
    assert client.calls == 1


@pytest.mark.parametrize('batch', [False, True])
def test_provider_failure_cannot_become_missing_catalogue_evidence(batch):
    from app.services.match.facts import extract_fact, extract_fact_batch
    client = RejectedClient()
    with pytest.raises(LLMRequestRejected):
        if batch:
            extract_fact_batch('Synthetic pressure 8 bar', ['Synthetic pressure 10 bar'], client=client)
        else:
            extract_fact('Synthetic pressure 8 bar', 'Synthetic pressure 10 bar', client=client)
    assert client.calls == 1
