"""`POST /api/tenders` and `GET /api/tenders/{id}/requirements` (PRD 11).

The service that freezes and reads a tender is tested in
`test_tenderupload.py`. What is tested here is the contract: what the routes
accept, what they answer, and when they refuse to answer at all. A database is
replaced by a fake repository, so the tests describe the API, not SQL.
"""

from __future__ import annotations

import json
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
    stages: dict[str, dict[str, str]] = {}

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

    def stage_statuses(self, tender_id):
        return dict(FakeRepo.stages.get(tender_id, {}))

    def count_requirements(self, tender_id):
        return len(FakeRepo.requirements.get(tender_id, []))

    def set_status(self, tender_id, status, *, error=None, reason_code=None):
        row = FakeRepo.rows[tender_id]
        row["status"] = status.value
        row["error"] = error
        row["reason_code"] = reason_code.value if reason_code else None


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
    FakeRepo.stages.clear()
    monkeypatch.setattr("app.api.tenders.TenderRepo", FakeRepo)
    monkeypatch.setattr("app.services.tender.upload.TenderRepo", FakeRepo)

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


def test_a_duplicate_whose_read_was_rate_limited_is_read_again(client, scheduled):
    """The read stopped at the provider's limit. Uploading the same file again
    is the caller asking for the rest, and the stages that stopped are the ones
    the next read resumes (D8, D-69)."""
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "rate_limited"
    FakeRepo.rows[TENDER_ID]["error"] = "the model was rate limited; retry in 24.9s"

    response = _upload(client)

    assert response.status_code == 202
    assert response.json()["status"] == "rate_limited"
    assert scheduled == [TENDER_ID, TENDER_ID]


def test_a_duplicate_that_is_still_reading_is_left_alone(client, scheduled):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "processing"

    response = _upload(client)

    assert response.status_code == 202
    assert response.json()["duplicate"] is True
    # A tender being read is not queued a second time.
    assert scheduled == [TENDER_ID]


def test_a_duplicate_left_processing_with_its_stages_missing_is_read_again(
    client, scheduled
):
    """The pipeline stores the requirements before it records its stages, so a
    process that died in between leaves a row saying `processing` with nothing
    reading it. Re-uploading it must read it, not wait forever (D-157)."""
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "processing"
    # Requirements stored, no stage row: the shape a killed process leaves.
    FakeRepo.requirements[TENDER_ID] = [{"item_id": "1", "text": "Body shall be SS316"}]

    response = _upload(client)

    assert response.status_code == 202
    assert response.json()["status"] == "incomplete"
    assert scheduled == [TENDER_ID, TENDER_ID]
    assert FakeRepo.rows[TENDER_ID]["status"] == "incomplete"


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


def test_a_tender_that_is_still_being_read_returns_what_it_has_read(client, tmp_path):
    """A partial list is not a complete one, and the answer says which it is.

    The requirement rows are written while the extraction reads (D-82), so the
    route answers with them instead of a 409. `partial` is what stops a caller
    from reading the short list as the document stating little (PRD 4).
    """
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "processing"
    FakeRepo.requirements[TENDER_ID] = [
        {
            "item_id": "1",
            "text": "The body shall be stainless steel",
            "page": 2,
            "type": "material",
            "mandatory": True,
            "review_required": False,
        }
    ]

    response = client.get(f"/api/tenders/{TENDER_ID}/requirements")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["partial"] is True
    assert body["counts"]["requirements"] == 1
    assert body["requirements"][0]["item_id"] == "1"
    # The item still travels with the requirement it belongs to.
    assert body["requirements"][0]["item_title"] is None


def test_a_tender_that_has_been_read_is_not_partial(client):
    assert _upload(client).status_code == 202
    FakeRepo.rows[TENDER_ID]["status"] = "done"

    body = client.get(f"/api/tenders/{TENDER_ID}/requirements").json()

    assert body["partial"] is False


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


# --- the live stream (D-82) ------------------------------------------------


def test_the_stream_of_an_unknown_tender_is_not_found(client):
    response = client.get(
        "/api/tenders/00000000-0000-0000-0000-000000000000/requirements/stream"
    )

    # Checked before the stream opens: an endless connection saying nothing is
    # worse than a 404.
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_the_stream_is_an_event_stream(client, monkeypatch):
    assert _upload(client).status_code == 202
    monkeypatch.setattr(
        "app.api.tenders._requirements_events",
        lambda tender_id: iter(['event: end\ndata: {"tender_id": "x"}\n\n']),
    )

    with client.stream(
        "GET", f"/api/tenders/{TENDER_ID}/requirements/stream"
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # A proxy that buffers would hold every event until the read finished.
        assert response.headers["x-accel-buffering"] == "no"
        assert "event: end" in "".join(response.iter_text())


class _ReadingTender:
    """A tender that has read one more requirement each time it is looked at."""

    def __init__(self, steps: list[tuple[str, list[str]]]) -> None:
        self.steps = steps
        self.looks = 0
        self.rows: list[dict] = []

    def get_tender(self, tender_id):
        status, texts = self.steps[min(self.looks, len(self.steps) - 1)]
        self.looks += 1
        self.rows = [
            {"id": f"r{index}", "item_id": "1", "text": text, "review_required": False}
            for index, text in enumerate(texts)
        ]
        return {
            "id": tender_id,
            "filename": "tender.pdf",
            "status": status,
            "error": None,
            "page_count": 1,
        }

    def list_items(self, tender_id):
        return [{"item_id": "1", "name": "Gate valve", "category": "valve"}]

    def list_requirements(self, tender_id):
        return self.rows


def _events(body: str) -> list[tuple[str, dict]]:
    """The events in an SSE body, in order. Comments are not events."""
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        name = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name:
            events.append((name, data))
    return events


def test_the_stream_sends_a_snapshot_then_each_change_then_the_end(monkeypatch):
    from contextlib import contextmanager

    from app.api import tenders as tenders_api

    read = ["The body shall be stainless steel"]
    also = read + ["The end connection shall be flanged"]
    repo = _ReadingTender([("processing", read), ("processing", also), ("done", also)])

    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr(tenders_api, "session_scope", lambda: scope())
    monkeypatch.setattr(tenders_api, "TenderRepo", lambda session: repo)
    monkeypatch.setenv("STREAM_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()

    events = _events("".join(tenders_api._requirements_events(TENDER_ID)))

    assert [name for name, _ in events] == ["snapshot", "requirements", "end"]
    # The snapshot is what is already read; the next event is the change.
    assert len(events[0][1]["requirements"]) == 1
    assert len(events[1][1]["requirements"]) == 2
    assert events[1][1]["partial"] is True
    assert events[1][1]["requirements"][1]["item_title"] == "Gate valve"
    # The tender stopped reading, so the stream says so and closes.
    assert events[2][1]["status"] == "done"
    assert repo.looks == 3
