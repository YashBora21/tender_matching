"""`POST /api/tenders` and `GET /api/tenders/{id}/requirements` (PRD 11).

The service that freezes and reads a tender is tested in
`test_tenderupload.py`. What is tested here is the contract: what the routes
accept, what they answer, and when they refuse to answer at all. A database is
replaced by a fake repository, so the tests describe the API, not SQL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app

PDF = b"%PDF-1.7\n% synthetic tender\n"
# The database gives a tender a UUID; the route refuses anything else.
TENDER_ID = "11111111-1111-1111-1111-111111111111"


class FakeRepo:
    """The tender table, and the rows of a tender, without a database."""

    rows: dict[str, dict] = {}
    upserts: list[dict] = []
    items: dict[str, list[dict]] = {}
    requirements: dict[str, list[dict]] = {}

    def __init__(self, session) -> None:
        self.session = session

    def upsert_tender(self, *, status=None, **values):
        FakeRepo.upserts.append({**values, "status": status})
        if TENDER_ID not in FakeRepo.rows:
            FakeRepo.rows[TENDER_ID] = {
                "id": TENDER_ID,
                "filename": values.get("filename"),
                "sha256": values.get("sha256"),
                "page_count": values.get("page_count"),
                "status": getattr(status, "value", status) or "queued",
                "error": None,
            }
        return TENDER_ID

    def get_tender(self, tender_id):
        row = FakeRepo.rows.get(tender_id)
        return dict(row) if row else None

    def list_items(self, tender_id):
        return [dict(item) for item in FakeRepo.items.get(tender_id, [])]

    def list_requirements(self, tender_id):
        return [dict(row) for row in FakeRepo.requirements.get(tender_id, [])]


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


@pytest.fixture
def scheduled(monkeypatch):
    """The background read, recorded rather than run."""
    calls: list[str] = []
    monkeypatch.setattr("app.api.tenders.ingest", lambda tender_id: calls.append(tender_id))
    return calls


@pytest.fixture
def client(tmp_path, monkeypatch, scheduled):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    FakeRepo.rows.clear()
    FakeRepo.upserts.clear()
    FakeRepo.items.clear()
    FakeRepo.requirements.clear()
    monkeypatch.setattr("app.api.tenders.TenderRepo", FakeRepo)
    monkeypatch.setattr("app.services.tenderupload.TenderRepo", FakeRepo)

    session = FakeSession()
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _upload(client, data: bytes = PDF, name: str = "tender.pdf"):
    return client.post(
        "/api/tenders",
        files={"file": (name, data, "application/pdf")},
    )


# --- accepting a tender ----------------------------------------------------


def test_an_uploaded_tender_is_accepted_and_read_in_the_background(
    client, tmp_path, scheduled
):
    response = _upload(client)

    assert response.status_code == 202
    body = response.json()
    assert body["tender_id"] == TENDER_ID
    assert body["status"] == "queued"
    assert body["duplicate"] is False

    # The answer does not carry requirements: they do not exist yet.
    assert "requirements" not in body
    # The read was scheduled, and the bytes were frozen before it.
    assert scheduled == [TENDER_ID]
    frozen = tmp_path / "tenders" / TENDER_ID / "tender.pdf"
    assert frozen.read_bytes() == PDF


def test_the_name_of_the_upload_is_not_trusted_as_a_path(client, tmp_path):
    response = _upload(client, name="..\\..\\escape.pdf")

    assert response.status_code == 202
    directory = tmp_path / "tenders" / TENDER_ID
    assert (directory / "escape.pdf").exists()
    assert not (tmp_path.parent / "escape.pdf").exists()


def test_a_file_that_is_not_a_pdf_is_refused_by_its_content(client, scheduled):
    response = _upload(client, data=b"<html>this is not a tender</html>")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    # Nothing was scheduled and no tender was created.
    assert scheduled == []
    assert FakeRepo.upserts == []


def test_a_file_whose_name_is_not_a_pdf_is_refused(client):
    response = _upload(client, name="tender.docx")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_an_empty_upload_is_refused(client):
    response = _upload(client, data=b"")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_an_oversized_upload_is_refused_before_it_is_read(
    client, monkeypatch, scheduled
):
    monkeypatch.setenv("FETCH_MAX_BYTES", "10")
    get_settings.cache_clear()

    response = _upload(client, data=PDF)

    assert response.status_code == 413
    assert "limit" in response.json()["detail"]
    assert scheduled == []


# --- the same file twice ---------------------------------------------------


def test_the_same_file_twice_is_one_tender_that_is_not_read_twice(client, scheduled):
    assert _upload(client).status_code == 202
    first = client.post(
        "/api/tenders", files={"file": ("renamed.pdf", PDF, "application/pdf")}
    )

    assert first.status_code == 202
    assert first.json()["tender_id"] == TENDER_ID
    assert first.json()["duplicate"] is True
    # The reading it already has is not thrown away and started again.
    assert scheduled == [TENDER_ID]
    assert len(FakeRepo.upserts) == 2


def test_a_duplicate_whose_earlier_read_failed_is_read_again(client, scheduled):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "failed"
    FakeRepo.rows[TENDER_ID]["error"] = "no bill of quantities was found"

    response = _upload(client)

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert scheduled == [TENDER_ID, TENDER_ID]


def test_a_duplicate_that_is_still_reading_is_left_alone(client, scheduled):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "processing"

    response = _upload(client)

    assert response.status_code == 202
    assert response.json()["duplicate"] is True
    # A tender being read is not queued a second time.
    assert scheduled == [TENDER_ID]


# --- reading the requirements ---------------------------------------------


def test_the_requirements_of_a_read_tender_are_returned_with_their_item(client):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID].update(
        {"status": "done", "page_count": 3, "filename": "tender.pdf"}
    )
    FakeRepo.items[TENDER_ID] = [
        {"item_id": "1", "name": "Gate valve", "category": "valve", "boq_ref": "1"}
    ]
    FakeRepo.requirements[TENDER_ID] = [
        {
            "item_id": "1",
            "text": "The body shall be stainless steel",
            "page": 2,
            "type": "material",
            "mandatory": True,
            "review_required": False,
        },
        {
            "item_id": "1",
            "text": "The end connection shall be flanged",
            "page": 2,
            "type": "connection",
            "mandatory": True,
            "review_required": True,
        },
    ]

    response = client.get(f"/api/tenders/{TENDER_ID}/requirements")

    assert response.status_code == 200
    body = response.json()
    assert body["tender_id"] == TENDER_ID
    assert body["status"] == "done"
    assert body["error"] is None
    assert body["page_count"] == 3
    assert body["counts"] == {"items": 1, "requirements": 2, "flagged": 1}
    # `item_id` is preserved end to end, and the item's name travels with it.
    assert body["requirements"][0]["item_id"] == "1"
    assert body["requirements"][0]["item_title"] == "Gate valve"
    assert body["requirements"][0]["category"] == "valve"


def test_a_tender_that_has_not_been_read_yet_is_a_conflict(client):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "processing"

    response = client.get(f"/api/tenders/{TENDER_ID}/requirements")

    # An empty list would say the tender states no requirement, which is a
    # different fact from "nobody has finished reading it" (PRD 4).
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["detail"]["status"] == "processing"


def test_a_tender_that_could_not_be_read_says_why(client):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID].update(
        {"status": "failed", "error": "no bill of quantities was found"}
    )

    response = client.get(f"/api/tenders/{TENDER_ID}/requirements")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "no bill of quantities was found"
    assert body["requirements"] == []


def test_an_unknown_tender_is_not_found(client):
    response = client.get("/api/tenders/00000000-0000-0000-0000-000000000000/requirements")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_malformed_id_is_not_found_rather_than_a_server_error(client):
    response = client.get("/api/tenders/not-a-uuid/requirements")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
