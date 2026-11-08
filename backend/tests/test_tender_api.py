"""The tender attachment routes: the listing, and the manual upload."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.embed", lambda texts: [[0.0]])
    monkeypatch.setattr("app.main._reconcile_on_startup", lambda: True)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def _write_manifest(data_dir: Path, tender_id: str, documents: list[dict]) -> None:
    directory = data_dir / "tenders" / tender_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"tender_id": tender_id, "category": "valve", "documents": documents}),
        encoding="utf-8",
    )


def test_the_attachments_of_a_tender_are_listed_with_name_url_and_hash(client, tmp_path):
    _write_manifest(
        tmp_path,
        "T1",
        [
            {
                "url": "https://mkp.gem.gov.in/boq.csv",
                "filename": "boq.csv",
                "sha256": "a" * 64,
                "kind": "boq",
                "status": "new",
            }
        ],
    )

    response = client.get("/api/tenders/T1/attachments")

    assert response.status_code == 200
    body = response.json()
    assert body["tender_id"] == "T1"
    assert body["attachments"][0]["name"] == "boq.csv"
    assert body["attachments"][0]["url"] == "https://mkp.gem.gov.in/boq.csv"
    assert body["attachments"][0]["sha256"] == "a" * 64


def test_a_failed_attachment_is_reported_as_missing(client, tmp_path):
    """A failed fetch is not a tender with no requirements. The UI needs to
    know to prompt for an upload."""
    _write_manifest(
        tmp_path,
        "T1",
        [
            {
                "url": "https://mkp.gem.gov.in/boq.csv",
                "status": "failed",
                "note": "the server returned HTTP 404",
            }
        ],
    )

    body = client.get("/api/tenders/T1/attachments").json()

    assert body["missing"] == ["https://mkp.gem.gov.in/boq.csv"]


def test_a_manual_upload_is_frozen_and_recorded(client, tmp_path):
    """The fallback: the reviewer has the file, and the pipeline is given
    it."""
    response = client.post(
        "/api/tenders/T1/attachments",
        files={"file": ("boq.csv", io.BytesIO(b"Item Number,Item Title\n1,GATE VALVE\n"), "text/csv")},
        data={"url": "https://mkp.gem.gov.in/boq.csv"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "manual"
    assert body["name"] == "boq.csv"
    assert len(body["sha256"]) == 64
    # The file is on disk, in the tender's own directory.
    frozen = tmp_path / "tenders" / "T1" / "raw" / "boq.csv"
    assert frozen.exists()


def test_an_upload_replaces_the_failed_record_it_stands_in_for(client, tmp_path):
    """One attachment, not two: the report lists what was used."""
    _write_manifest(
        tmp_path,
        "T1",
        [{"url": "https://mkp.gem.gov.in/boq.csv", "status": "failed"}],
    )

    client.post(
        "/api/tenders/T1/attachments",
        files={"file": ("boq.csv", io.BytesIO(b"Item Number\n1\n"), "text/csv")},
        data={"url": "https://mkp.gem.gov.in/boq.csv"},
    )

    body = client.get("/api/tenders/T1/attachments").json()
    assert len(body["attachments"]) == 1
    assert body["attachments"][0]["status"] == "manual"
    assert body["missing"] == []


def test_an_upload_of_the_wrong_kind_is_refused(client):
    response = client.post(
        "/api/tenders/T1/attachments",
        files={"file": ("note.exe", io.BytesIO(b"x"), "application/octet-stream")},
    )

    assert response.status_code == 400


def test_a_tender_with_no_manifest_has_no_attachments(client):
    body = client.get("/api/tenders/UNKNOWN/attachments").json()
    assert body["attachments"] == []
    assert body["missing"] == []
