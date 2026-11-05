"""The startup repair, and asking again when the database was not up (D-163).

The repair itself is tested against a database in `test_tenderrecovery.py`;
what is tested here is the decision to ask again, which needs no database.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import app.main as main
from app.core.config import get_settings


def test_a_database_that_is_down_is_reported_not_raised(monkeypatch):
    """A database that is down must not stop the API - it is what `/health`
    reports - but the caller has to know the repair did not run."""
    from app.repositories import db

    def down():
        raise ConnectionError("the database is down")

    monkeypatch.setattr(db, "session_scope", down)

    assert main._reconcile_on_startup() is False


def test_a_repair_that_ran_is_reported_as_such(monkeypatch):
    from app.repositories import db
    from app.services.cli import recover

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(db, "session_scope", lambda: Session())
    monkeypatch.setattr(recover, "reconcile", lambda session: [])

    assert main._reconcile_on_startup() is True


def test_the_repair_is_asked_again_until_the_database_answers():
    """A row left mid-read stays `processing` until the repair runs, so one
    failed attempt is not the end of the attempt."""
    calls: list[int] = []

    def attempt() -> bool:
        calls.append(1)
        return len(calls) >= 3

    asyncio.run(main._reconcile_until_it_works(0, attempt=attempt))

    assert len(calls) == 3


def test_the_lifespan_asks_again_only_when_the_first_attempt_failed(monkeypatch):
    """The wiring, not the loop: a startup with a live database must not leave
    a task running behind it."""
    asked: list[float] = []

    async def fake(retry_seconds: float, attempt=None) -> None:
        asked.append(retry_seconds)

    monkeypatch.setattr(main, "_reconcile_on_startup", lambda: False)
    monkeypatch.setattr(main, "_reconcile_until_it_works", fake)

    with TestClient(main.create_app()):
        pass

    assert asked == [get_settings().reconcile_retry_seconds]

    asked.clear()
    monkeypatch.setattr(main, "_reconcile_on_startup", lambda: True)

    with TestClient(main.create_app()):
        pass

    assert asked == []
