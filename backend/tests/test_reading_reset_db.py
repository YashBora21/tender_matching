"""Synthetic reading/cache/reset invariants in a disposable PostgreSQL database."""
import uuid
import pytest
from sqlalchemy import create_engine, insert, select, func
from sqlalchemy.orm import Session
from app.repositories import schema as db
from app.repositories.tender import TenderRepo, LLMCache
from app.services.cli.reset import SqlResetRepo, ResetPlanner, Protection, execute_reset

pytestmark = pytest.mark.integration


def test_reading_cache_sharing_and_scoped_deletion(test_database_url):
    engine = create_engine(test_database_url)
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection)
        try:
            first, other, maker, run_id = [uuid.uuid4() for _ in range(4)]
            session.execute(insert(db.manufacturer).values(id=maker, name='Synthetic reset preservation'))
            for tender_id, digest in [(first, 'a'), (other, 'b')]:
                session.execute(insert(db.tender).values(id=tender_id, filename='Synthetic-'+digest+'.pdf',
                    sha256=digest * 64, status='done', page_count=1))
            repo = TenderRepo(session)
            attempt = repo.start_reading_attempt(str(first), limits={'call_budget': 3})
            repo.finish_reading_attempt(attempt, status='done', metrics={'llm_calls': 1})
            repo.finish_reading_attempt(attempt, status='failed')
            assert repo.list_reading_attempts(str(first))[0]['status'] == 'done'
            cache_a = LLMCache(session, tender_id=str(first), reading_id=attempt)
            cache_b = LLMCache(session, tender_id=str(other))
            cache_a.put('c' * 64, 'synthetic', 'synthetic', 'synthetic', {'ok': True})
            cache_a.put('d' * 64, 'synthetic', 'synthetic', 'synthetic', {'ok': True})
            assert cache_b.get('c' * 64, 'synthetic', 'synthetic', 'synthetic') == {'ok': True}
            session.execute(insert(db.requirement).values(id=uuid.uuid4(), tender_id=first,
                source_page=1, source_text='Synthetic capacity', extraction_method='text',
                type='technical', extraction_version='synthetic', schema_version='synthetic'))
            session.execute(insert(db.run).values(id=run_id, tender_id=first, status='done'))
            session.execute(insert(db.llm_spend).values(id=uuid.uuid4(), run_id=run_id,
                model='synthetic', cost_inr=1))
            reset_repo = SqlResetRepo(session)
            plan = ResetPlanner(reset_repo, Protection()).plan([str(first)], dry_run=False)
            assert len(plan.cache_delete) == 1 and plan.cache_retained_shared == 1
            execute_reset(plan, reset_repo, confirm=True)
            assert session.execute(select(db.tender.c.id)).scalars().all() == [other]
            assert session.execute(select(db.manufacturer.c.id).where(db.manufacturer.c.id == maker)).scalar_one() == maker
            assert session.execute(select(db.llm_spend.c.run_id)).scalar_one() is None
            assert session.execute(select(func.count()).select_from(db.llm_cache)).scalar_one() == 1
        finally:
            session.close()
            transaction.rollback()
    engine.dispose()
