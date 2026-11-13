"""Synthetic concurrent spend reservations in a private test schema; no model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from app.repositories.schema import llm_spend
from app.services.spend import SpendLedger,SpendCapReached

pytestmark=pytest.mark.integration

@pytest.fixture
def ledger(test_database_url,monkeypatch):
    schema='spend_test_'+uuid4().hex
    engine=create_engine(test_database_url,connect_args={'options':f'-csearch_path={schema},public'})
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'CREATE TABLE "{schema}".llm_spend (LIKE public.llm_spend INCLUDING ALL)'))
    for key,value in {'LLM_SPEND_CAPS_ENABLED':'true','LLM_DAILY_CAP_INR':'1',
                      'LLM_MONTHLY_CAP_INR':'1000','LLM_COST_INPUT_PER_MTOK_INR':'100',
                      'LLM_COST_OUTPUT_PER_MTOK_INR':'200'}.items():monkeypatch.setenv(key,value)
    @contextmanager
    def sessions():
        with Session(engine) as session:
            try:yield session;session.commit()
            except Exception:session.rollback();raise
    try:yield SpendLedger(session_factory=sessions),sessions
    finally:
        assert schema.startswith('spend_test_') and len(schema)==43
        with engine.begin() as connection:connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()

def test_concurrent_reservations_cannot_both_cross_the_cap(ledger):
    gate,sessions=ledger;barrier=Barrier(2)
    def reserve(_):
        barrier.wait()
        try:return gate.reserve(model='synthetic',purpose='classification',input_bound=6000,output_bound=0)
        except SpendCapReached:return None
    with ThreadPoolExecutor(max_workers=2) as pool:outcomes=list(pool.map(reserve,range(2)))
    assert sum(value is not None for value in outcomes)==1
    with sessions() as session:
        rows=session.execute(select(llm_spend)).mappings().all()
    assert len(rows)==1 and float(rows[0]['cost_inr'])==pytest.approx(.6)

def test_settlement_releases_only_unused_estimate_and_keeps_purpose(ledger):
    gate,sessions=ledger
    identifier=gate.reserve(model='synthetic',purpose='extraction',input_bound=6000,output_bound=0)
    gate.settle(identifier,prompt_tokens=1000,completion_tokens=0)
    gate.reserve(model='synthetic',purpose='repair',input_bound=6000,output_bound=0)
    with sessions() as session:
        rows=session.execute(select(llm_spend).order_by(llm_spend.c.occurred_at)).mappings().all()
    assert [r['purpose'] for r in rows]==['extraction','repair']
    assert float(sum(r['cost_inr'] for r in rows))==pytest.approx(.7)
    # A new ledger instance still sees the unsettled reservation.
    with pytest.raises(SpendCapReached):
        SpendLedger(session_factory=sessions).reserve(model='synthetic',purpose='matching',input_bound=6000,output_bound=0)
