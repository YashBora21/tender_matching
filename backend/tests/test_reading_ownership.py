"""Synthetic workers: no provider calls or application DB writes."""
from contextlib import contextmanager
import pytest
from app.schemas.enums import TenderStatus
from app.services.cli.recover import settle
from app.repositories import db


class Repo:
    def __init__(self, *, owned=True, available=True, requirements=0):
        self.attempt={'id':'synthetic-attempt','limits':{'reading_ownership':'pg-session-lock-1'} if owned else {},'metrics':{'llm_calls':2}}
        self.available=available;self.requirements=requirements;self.closed=[];self.statuses=[]
    def latest_running_reading(self,tid):return self.attempt
    def claim_reading_recovery(self,tid):return self.available
    def count_requirements(self,tid):return self.requirements
    def get_tender(self,tid,**kwargs):return {'status':'processing'}
    def finish_reading_attempt(self,identity,**kwargs):self.closed.append((identity,kwargs))
    def set_status(self,tid,status,**kwargs):self.statuses.append(status)


def test_early_crash_without_stage_rows_is_closed_and_metrics_preserved():
    repo=Repo()
    assert settle(None,'synthetic',repo=repo)==TenderStatus.FAILED
    assert repo.closed[0][1]['metrics']=={'llm_calls':2}
    assert repo.statuses==[TenderStatus.FAILED]


def test_live_worker_is_untouched_even_without_stage_rows():
    repo=Repo(available=False)
    assert settle(None,'synthetic',repo=repo) is None
    assert not repo.closed and not repo.statuses


def test_legacy_worker_without_ownership_evidence_is_not_guessed_dead():
    repo=Repo(owned=False)
    assert settle(None,'synthetic',repo=repo) is None
    assert not repo.closed


def test_dead_worker_with_saved_requirements_is_incomplete_not_done():
    repo=Repo(requirements=3)
    assert settle(None,'synthetic',repo=repo)==TenderStatus.INCOMPLETE


def test_worker_finishing_between_lookup_and_lock_is_not_downgraded():
    repo=Repo()
    def claim(tid):
        repo.latest_running_reading=lambda tid:None
        return True
    repo.claim_reading_recovery=claim
    assert settle(None,'synthetic',repo=repo) is None
    assert not repo.closed and not repo.statuses


@pytest.mark.parametrize('crash',[False,True])
def test_lock_connection_is_pinned_across_commits_and_released_on_every_exit(monkeypatch,crash):
    class Connection:
        def __init__(self):self.events=[]
        def execute(self,q,p):
            self.events.append(str(q))
            return type('Result',(),{'scalar_one':lambda self:True})()
        def commit(self):self.events.append('commit')
        def rollback(self):self.events.append('rollback')
        def invalidate(self):self.events.append('invalidate')
        def __enter__(self):return self
        def __exit__(self,*args):self.events.append('close_connection')
    connection=Connection()
    class Engine:
        def connect(self):return connection
    class Session:
        def __init__(self,bind,**kwargs):assert bind is connection
        def commit(self):connection.events.append('session_commit')
        def rollback(self):connection.events.append('session_rollback')
        def close(self):connection.events.append('close_session')
    monkeypatch.setattr(db,'get_engine',lambda:Engine());monkeypatch.setattr(db,'Session',Session)
    try:
        with db.session_scope(reading_tender='synthetic') as session:
            session.commit()
            assert not any('pg_advisory_unlock' in e for e in connection.events)
            if crash:raise RuntimeError('Synthetic crash')
    except RuntimeError:
        assert crash
    assert connection.events[-1]=='close_connection'
    assert any('pg_advisory_unlock' in e for e in connection.events)


def test_ingest_does_not_start_a_second_read_when_lock_is_held(monkeypatch):
    from app.services.tender import upload
    @contextmanager
    def scope(**kwargs):yield None
    monkeypatch.setattr(db,'session_scope',scope)
    monkeypatch.setattr(upload,'_read',lambda *a,**k:pytest.fail('Duplicate read must not run'))
    assert upload.ingest('synthetic')=='processing'


def test_failed_unlock_discards_connection_instead_of_returning_a_held_lock_to_pool():
    class Connection:
        discarded=False
        def rollback(self):pass
        def execute(self,*args):raise RuntimeError('Synthetic disconnect')
        def invalidate(self):self.discarded=True
    connection=Connection()
    db._release_reading_lock(connection,db.reading_lock_key('synthetic'))
    assert connection.discarded
