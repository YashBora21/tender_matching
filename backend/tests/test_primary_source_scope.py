"""Synthetic primary-source scope; no provider calls or production database writes."""
from dataclasses import asdict
import pytest
from app.services.tender import source_policy
from app.services.tender.ingest import RequirementRecord
from app.services.tender.pipeline import _extract_all, to_stored, _stored_records
from app.services.tender.completeness import completion_gaps
from app.schemas.enums import RequirementType
from app.services.output.runs import assessable


@pytest.mark.parametrize('role',['cover','atc','sla','supporting','unknown'])
@pytest.mark.parametrize('text',['Synthetic pressure 8 bar','Synthetic manufacturer ISO 9001',
                                'Synthetic payment terms','Synthetic unrecognised property ZETA'])
def test_nonprimary_sources_are_typed_visible_context_with_zero_calls(role,text):
    decision=source_policy.triage(text,role)
    assert decision.context_only and decision.candidate_type is None
    # Simulate an older stored candidate: the scheduler must apply current scope.
    row=RequirementRecord(text=text,source_text=text,item_id='',source_role=role,
        context_only=False,candidate_type='technical',requirement_type=RequirementType.UNKNOWN)
    class Forbidden:
        def complete_json(self,*args,**kwargs):raise AssertionError('Context must not call a provider')
    output,calls,_,limited,_=_extract_all([row],client=Forbidden())
    assert not calls and not limited and len(output)==1
    assert row.source_text==text and row.context_only
    assert row.requirement_type.value==decision.clause_type
    assert not completion_gaps({},[],[],[asdict(row)])
    assert not assessable([asdict(to_stored(row,'synthetic'))])
    stored=to_stored(row,'synthetic')
    class Repo:
        def list_requirements(self,_):return [dict(vars(stored),type=row.requirement_type.value)]
    restored=_stored_records(Repo(),'synthetic')[0]
    assert restored.source_text==text and restored.context_only


@pytest.mark.parametrize('role',['specification','boq'])
def test_primary_signals_still_require_reading_and_failed_pages_still_block(role):
    decision=source_policy.triage('Working pressure 8 bar',role)
    assert not decision.context_only
    row=RequirementRecord(text='Working pressure 8 bar',item_id='',source_role=role,
        candidate_type=decision.candidate_type,requirement_type=RequirementType(decision.clause_type))
    assert completion_gaps({},[],[],[asdict(row)])
    assert completion_gaps({},[],[dict(page=1,ledger_status='failed')],[])


def test_source_selector_changes_source_checkpoint_but_not_valid_clause_cache(monkeypatch):
    full=source_policy.fingerprint();classifier=source_policy.fingerprint(for_classification=True)
    data=dict(source_policy.policy(),requirement_source_roles=[])
    monkeypatch.setattr(source_policy,'policy',lambda:data)
    assert source_policy.fingerprint()!=full
    assert source_policy.fingerprint(for_classification=True)==classifier
