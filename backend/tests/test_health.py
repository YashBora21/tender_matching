"""Health endpoint tests.

The database is mocked so these run with no Postgres and no Docker.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.enums import HealthState


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_is_ok_when_database_answers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.ping", lambda *a, **k: None)
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == HealthState.OK
    assert body["dependencies"][0]["name"] == "database"
    assert body["dependencies"][0]["state"] == HealthState.OK


def test_health_degrades_when_database_is_down(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("no database")

    monkeypatch.setattr("app.api.routes.ping", boom)
    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == HealthState.DEGRADED
    assert body["dependencies"][0]["state"] == HealthState.DOWN


def test_health_does_not_leak_secrets(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setattr("app.api.routes.ping", lambda *a, **k: None)
    body = client.get("/api/health").json()
    assert "super-secret-value" not in str(body)


def test_health_reports_the_configured_environment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which deployment this is comes from the environment.

    The route used to print "local" from a string of its own, so a container
    answered the question wrongly and the answer looked like a fact.
    """
    from app.core.config import get_settings

    monkeypatch.setattr("app.api.routes.ping", lambda *a, **k: None)
    monkeypatch.setattr(get_settings(), "app_environment", "staging")
    body = client.get("/api/health").json()
    assert body["environment"] == "staging"


def test_health_reports_effective_numeric_limits_without_provider_values(client,monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setenv('LLM_CALL_BUDGET','600')
    monkeypatch.setenv('LLM_MAX_RETRIES','3')
    get_settings.cache_clear()
    monkeypatch.setattr('app.api.routes.ping',lambda:None)
    body=client.get('/api/health').json()
    assert body['config']['processing_limits']['call_budget']==600
    assert body['config']['processing_limits']['max_retries']==3
    assert 'llm_model' not in body['config'] and 'llm_api_key' not in body['config']
