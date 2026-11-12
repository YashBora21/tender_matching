"""`POST /api/tenders/{id}/read-again` — the explicit resume action (step 5).

The contract only: what the route accepts, what it schedules, when it refuses.
A database is replaced by a fake repository, exactly as in
`test_tender_upload_api.py`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app

TENDER_ID = "11111111-1111-1111-1111-111111111111"


class FakeRepo:
    rows: dict[str, dict] = {}

    def __init__(self, session) -> None:
        self.session = session

    def get_tender(self, tender_id, *, for_update=False):
        row = FakeRepo.rows.get(tender_id)
        return dict(row) if row else None

    def set_status(self, tender_id, status, *, error=None, reason_code=None):
        FakeRepo.rows[tender_id]["status"] = status.value


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


@pytest.fixture
def scheduled(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.api.tenders.ingest", lambda tender_id, **kwargs: calls.append((tender_id, kwargs)))
    return calls


@pytest.fixture
def client(monkeypatch, scheduled):
    monkeypatch.setattr("app.main.embed", lambda texts: [[0.0]])
    monkeypatch.setattr("app.main._reconcile_on_startup", lambda: True)
    get_settings.cache_clear()
    FakeRepo.rows.clear()
    FakeRepo.rows[TENDER_ID] = {"id": TENDER_ID, "status": "rate_limited", "error": None}
    monkeypatch.setattr("app.api.tenders.TenderRepo", FakeRepo)
    session = FakeSession()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_a_stopped_tender_is_read_again(client, scheduled):
    response = client.post(f"/api/tenders/{TENDER_ID}/read-again")

    assert response.status_code == 202
    body = response.json()
    assert body["tender_id"] == TENDER_ID
    assert body["status"] == "queued"
    assert scheduled == [(TENDER_ID, {'force': True})]
    assert FakeRepo.rows[TENDER_ID]["status"] == "queued"


def test_a_tender_still_being_read_is_refused_not_double_read(client, scheduled):
    FakeRepo.rows[TENDER_ID]["status"] = "processing"

    response = client.post(f"/api/tenders/{TENDER_ID}/read-again")

    assert response.status_code == 409
    assert scheduled == []


def test_an_unknown_tender_is_a_404(client, scheduled):
    other = "22222222-2222-2222-2222-222222222222"
    response = client.post(f"/api/tenders/{other}/read-again")

    assert response.status_code == 404
    assert scheduled == []


def test_a_malformed_id_never_reaches_the_repository(client, scheduled):
    response = client.post("/api/tenders/not-a-uuid/read-again")

    assert response.status_code == 404
    assert scheduled == []
