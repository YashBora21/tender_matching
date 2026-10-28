"""The run and manufacturer routes (PRD 11).

The database is replaced by a dependency override and the services by fakes, so
these test the HTTP contract: what a caller sends, what it gets back, and which
requests are refused. The work behind the routes is tested in test_runs.py and
test_report.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.core.errors import NotFoundError
from app.main import create_app
from app.services.report import STATUS_MEANINGS
from app.services.runs import RunProgress

RUN_ID = "11111111-1111-4111-8111-111111111111"
TENDER_ID = "22222222-2222-4222-8222-222222222222"


class FakeSession:
    """The route commits the run before scheduling it; nothing else is used."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def client():
    app = create_app()
    # No connection is opened: the routes are exercised without a database.
    app.dependency_overrides[get_session] = lambda: FakeSession()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _progress(status="queued", results=0):
    return RunProgress(
        run_id=RUN_ID,
        status=status,
        tender_id=TENDER_ID,
        manufacturers=["Acme"],
        requirements=2,
        expected=2,
        results=results,
    )


REPORT = {
    "run": {
        "id": RUN_ID,
        "tender_id": TENDER_ID,
        "status": "done",
        "started_at": "2026-10-25T10:00:00+00:00",
        "error": None,
        "manufacturers": ["Acme"],
        "corpus_hash": "c" * 64,
    },
    "tender": {
        "id": TENDER_ID,
        "filename": "tender.pdf",
        "sha256": "a" * 64,
        "page_count": 1,
        "pages": {
            "declared": 1,
            "accounted": 1,
            "unaccounted": 0,
            "counts": {"parsed": 1},
            "unresolved": [],
            "skipped": [],
            "incomplete_ingestion": False,
        },
    },
    "config": {"llm_model": "m"},
    "results": {"stored": 1, "expected": 1, "complete": True},
    "manufacturers": [
        {
            "manufacturer_id": "mfr-a",
            "manufacturer": "Acme",
            "rank": 1,
            "compatibility_percent": 100.0,
            "coverage_percent": 100.0,
            "status_counts": {"full_match": 1},
            "fails_mandatory": False,
            "review_count": 0,
        }
    ],
    "requirements": [
        {
            "requirement_id": "r1",
            "item_id": "1",
            "item_title": "GATE VALVE",
            "category": "valves",
            "text": "Body shall be SS316",
            "source_text": "Body shall be SS316",
            "type": "technical",
            "mandatory": True,
            "page": 1,
            "value": None,
            "unit": None,
            "results": [
                {
                    "manufacturer_id": "mfr-a",
                    "manufacturer": "Acme",
                    "status": "full_match",
                    "score": 1.0,
                    "verification_outcome": "passed",
                    "applicability": "model_level",
                    "capability_text": "Body material SS316",
                    "confidence": "high",
                    "review_required": False,
                    "review_reason": None,
                    "evidence": {"page": 2, "quote": "Body: SS316", "document": "d.pdf"},
                }
            ],
        }
    ],
    "unsupported_requirements": [],
    "review_queue": [],
    "status_meanings": STATUS_MEANINGS,
}


# --- starting a run -------------------------------------------------------


def test_starting_a_run_answers_at_once_and_works_in_the_background(client, monkeypatch):
    scheduled: list[str] = []
    created: list[dict] = []

    def fake_create(session, tender_id, manufacturers=None):
        created.append({"tender_id": tender_id, "manufacturers": manufacturers})
        return RUN_ID

    monkeypatch.setattr("app.api.runs.create_run", fake_create)
    monkeypatch.setattr("app.api.runs.execute_run", scheduled.append)
    monkeypatch.setattr("app.api.runs.run_progress", lambda session, run_id: _progress())

    response = client.post("/api/runs", json={"tender_id": TENDER_ID})

    assert response.status_code == 202
    assert response.json()["run_id"] == RUN_ID
    assert response.json()["status"] == "queued"
    assert created == [{"tender_id": TENDER_ID, "manufacturers": None}]
    # The request that starts a run is not the request that waits for it.
    assert scheduled == [RUN_ID]


def test_a_run_can_name_its_manufacturers(client, monkeypatch):
    created: list[dict] = []

    def fake_create(session, tender_id, manufacturers=None):
        created.append({"tender_id": tender_id, "manufacturers": manufacturers})
        return RUN_ID

    monkeypatch.setattr("app.api.runs.create_run", fake_create)
    monkeypatch.setattr("app.api.runs.execute_run", lambda run_id: None)
    monkeypatch.setattr("app.api.runs.run_progress", lambda session, run_id: _progress())

    client.post("/api/runs", json={"tender_id": TENDER_ID, "manufacturers": ["Acme"]})

    assert created[0]["manufacturers"] == ["Acme"]


def test_an_empty_manufacturer_list_is_refused(client, monkeypatch):
    """An empty list is not "all of them": the caller asked for none."""
    called: list[object] = []
    monkeypatch.setattr("app.api.runs.create_run", lambda *a, **k: called.append(a))

    response = client.post(
        "/api/runs", json={"tender_id": TENDER_ID, "manufacturers": []}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert called == []


def test_a_malformed_tender_id_is_not_a_server_error(client, monkeypatch):
    called: list[object] = []
    monkeypatch.setattr("app.api.runs.create_run", lambda *a, **k: called.append(a))

    response = client.post("/api/runs", json={"tender_id": "not-a-tender"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert called == []


def test_a_tender_the_database_does_not_have_is_a_not_found(client, monkeypatch):
    def missing(*args, **kwargs):
        raise NotFoundError(f"no tender with id {TENDER_ID}")

    monkeypatch.setattr("app.api.runs.create_run", missing)

    response = client.post("/api/runs", json={"tender_id": TENDER_ID})

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": f"no tender with id {TENDER_ID}",
    }


# --- polling --------------------------------------------------------------


def test_a_run_reports_its_progress(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.runs.run_progress",
        lambda session, run_id: _progress(status="retrieving", results=1),
    )

    body = client.get(f"/api/runs/{RUN_ID}").json()

    assert body["status"] == "retrieving"
    assert body["results"] == 1
    assert body["expected_results"] == 2
    assert body["progress_percent"] == 50.0


def test_a_malformed_run_id_is_a_not_found_without_touching_the_database(
    client, monkeypatch
):
    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a malformed id must not reach the service")

    monkeypatch.setattr("app.api.runs.run_progress", explode)

    response = client.get("/api/runs/not-a-run")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_an_unknown_run_is_a_not_found(client, monkeypatch):
    def missing(session, run_id):
        raise NotFoundError(f"no run with id {run_id}")

    monkeypatch.setattr("app.api.runs.run_progress", missing)

    response = client.get(f"/api/runs/{RUN_ID}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# --- the report -----------------------------------------------------------


def test_a_report_of_a_run_still_working_is_a_conflict(client, monkeypatch):
    """Half a run reads as a manufacturer that answered nothing."""
    monkeypatch.setattr(
        "app.api.runs.run_progress",
        lambda session, run_id: _progress(status="retrieving", results=1),
    )
    monkeypatch.setattr(
        "app.api.runs.build_report",
        lambda *a, **k: pytest.fail("a report must not be built for a live run"),
    )

    response = client.get(f"/api/runs/{RUN_ID}/report")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["detail"]["progress"]["status"] == "retrieving"


def test_a_report_of_a_rate_limited_run_is_served(client, monkeypatch):
    """A rate-limited run has stopped, so its partial report is its answer.
    Refusing it would hide the rows that were found (D5)."""
    monkeypatch.setattr(
        "app.api.runs.run_progress",
        lambda session, run_id: _progress(status="rate_limited", results=1),
    )
    monkeypatch.setattr("app.api.runs.build_report", lambda *a, **k: REPORT)

    response = client.get(f"/api/runs/{RUN_ID}/report")

    assert response.status_code == 200


@pytest.fixture
def finished(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.runs.run_progress",
        lambda session, run_id: _progress(status="done", results=2),
    )
    monkeypatch.setattr("app.api.runs.build_report", lambda *a, **k: REPORT)
    return client


def test_the_json_report_is_the_canonical_answer(finished):
    response = finished.get(f"/api/runs/{RUN_ID}/report")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "done"
    assert body["requirements"][0]["results"][0]["evidence"]["quote"] == "Body: SS316"
    assert body["config"]["llm_model"] == "m"


def test_the_html_report_is_readable(finished):
    response = finished.get(f"/api/runs/{RUN_ID}/report.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Tender matching report" in response.text
    assert "Body shall be SS316" in response.text


def test_the_csv_export_is_a_download(finished):
    response = finished.get(f"/api/runs/{RUN_ID}/export.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.text.splitlines()[0].startswith("requirement_id")
    assert "Acme" in response.text


# --- the corpus -----------------------------------------------------------


def test_the_manufacturers_route_reports_what_the_corpus_covers(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.manufacturers.list_manufacturers",
        lambda session: [
            {
                "id": "mfr-a",
                "name": "Acme",
                "official_domain": "acme.example",
                "documents": 2,
                "products": 3,
                "chunks": 40,
                "categories": ["valves"],
            },
            {
                "id": "mfr-b",
                "name": "Beta",
                "official_domain": None,
                "documents": 1,
                "products": 1,
                "chunks": 10,
                "categories": ["pumps", "valves"],
            },
        ],
    )
    monkeypatch.setattr("app.api.manufacturers.corpus_hash", lambda session: "hash")

    body = client.get("/api/manufacturers").json()

    assert body["count"] == 2
    assert body["totals"] == {"documents": 3, "products": 4, "chunks": 50}
    assert body["categories"] == ["pumps", "valves"]
    assert body["corpus_hash"] == "hash"


def test_an_empty_corpus_is_an_empty_list_not_an_error(client, monkeypatch):
    monkeypatch.setattr("app.api.manufacturers.list_manufacturers", lambda session: [])
    monkeypatch.setattr("app.api.manufacturers.corpus_hash", lambda session: "")

    response = client.get("/api/manufacturers")

    assert response.status_code == 200
    assert response.json()["count"] == 0
