"""Reading attempts: each read recorded with its limits, closed with its outcome.

Synthetic fixtures only: the tender is a fake row, the pipeline a stub, and
nothing here talks to a provider or a database.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.repositories.tender import LLMCache
from app.schemas.enums import TenderStatus
from app.services.tender import upload


class FakeRepo:
    """The tender row and the attempt ledger, without a database."""

    row: dict = {}
    attempts: list[dict] = []
    finished: list[dict] = []
    fail_start = False
    fail_finish = False

    def __init__(self, session) -> None:
        self.session = session

    def get_tender(self, tender_id):
        return dict(FakeRepo.row) if tender_id == "T1" else None

    def set_status(self, tender_id, status, *, error=None, reason_code=None):
        FakeRepo.row["status"] = status.value
        FakeRepo.row["error"] = error

    def start_reading_attempt(self, tender_id, *, limits, sha256=None, filename=None):
        if FakeRepo.fail_start:
            raise RuntimeError("the attempt table does not exist")
        attempt_id = f"A{len(FakeRepo.attempts) + 1}"
        FakeRepo.attempts.append(
            {
                "id": attempt_id,
                "tender_id": tender_id,
                "status": "running",
                "limits": dict(limits),
                "sha256": sha256,
                "filename": filename,
            }
        )
        return attempt_id

    def finish_reading_attempt(
        self, attempt_id, *, status, stop_reason=None, reason_code=None, metrics=None
    ):
        if FakeRepo.fail_finish:
            raise RuntimeError("the attempt row is gone")
        FakeRepo.finished.append(
            {
                "id": attempt_id,
                "status": status,
                "stop_reason": stop_reason,
                "metrics": dict(metrics or {}),
            }
        )


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    @contextmanager
    def begin_nested(self):
        yield


def _audit(**values):
    """A pipeline result with everything a read needs to call itself done."""
    defaults = dict(
        failure=None,
        failure_code=None,
        rate_limited=False,
        rate_limited_detail=None,
        completion_gaps=[],
        pages_read=2,
        items=1,
        requirements=3,
        attachments=1,
        attachments_failed=0,
        llm_calls=4,
        llm_cache_hits=1,
        prompt_tokens=100,
        completion_tokens=50,
        llm_retries=0,
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    FakeRepo.row.clear()
    FakeRepo.row.update(
        {"id": "T1", "status": "queued", "sha256": "a" * 64, "filename": "t.pdf"}
    )
    FakeRepo.attempts.clear()
    FakeRepo.finished.clear()
    FakeRepo.fail_start = False
    FakeRepo.fail_finish = False
    monkeypatch.setattr(upload, "TenderRepo", FakeRepo)
    directory = tmp_path / "tenders" / "T1"
    directory.mkdir(parents=True)
    (directory / "t.pdf").write_bytes(b"%PDF-1.7\n% synthetic tender\n")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "tender_id": "T1",
                "filename": "t.pdf",
                "category": "valve",
                "wrapper_path": str(directory / "t.pdf"),
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def _pipeline(monkeypatch):
    """The collection and the audit, recorded instead of run."""
    monkeypatch.setattr(upload, "write_manifest", lambda manifest, directory: None)
    monkeypatch.setattr(
        upload, "collect_tender", lambda *args, **kwargs: SimpleNamespace(documents=[])
    )
    # `_manual_documents` reads the manifest; the stub collects nothing.
    monkeypatch.setattr(upload, "_manual_documents", lambda previous, collected: [])
    monkeypatch.setattr(upload, "_wrapper_document", lambda wrapper, directory: None)
    monkeypatch.setattr(upload, "_wrapper", lambda directory, manifest: directory / "t.pdf")
    recorded = {}
    monkeypatch.setattr(upload, "LLMCache", lambda session, **kwargs: recorded.update(kwargs) or "cache")
    monkeypatch.setattr(upload, "audit_tender", lambda *args, **kwargs: _audit())
    return recorded


def test_an_attempt_is_opened_with_its_limits_and_closed_with_its_metrics(_pipeline):
    status = upload.ingest("T1")

    assert status == TenderStatus.DONE.value
    assert len(FakeRepo.attempts) == 1
    opened = FakeRepo.attempts[0]
    assert opened["status"] == "running"
    assert opened["sha256"] == "a" * 64
    assert opened["filename"] == "t.pdf"
    limits = opened["limits"]
    settings = get_settings()
    assert limits["call_budget"] == settings.llm_call_budget
    assert limits["token_budget"] == settings.llm_token_budget
    assert limits["schema_version"] == settings.schema_version
    # No model identity and no endpoint in the recorded limits.
    assert not any("model" in key or "url" in key for key in limits)

    closed = FakeRepo.finished[0]
    assert closed["id"] == opened["id"]
    assert closed["status"] == "done"
    assert closed["metrics"]["llm_calls"] == 4
    assert closed["metrics"]["requirements"] == 3
    # The cache writes carry the tender and the attempt as provenance.
    assert _pipeline["tender_id"] == "T1"
    assert _pipeline["reading_id"] == opened["id"]


def test_a_rate_limited_read_closes_its_attempt_as_rate_limited(monkeypatch, _pipeline):
    monkeypatch.setattr(
        upload, "audit_tender", lambda *a, **k: _audit(rate_limited=True,
                                                        rate_limited_detail="the provider said no")
    )

    status = upload.ingest("T1")

    assert status == TenderStatus.RATE_LIMITED.value
    assert FakeRepo.finished[0]["status"] == "rate_limited"
    assert FakeRepo.finished[0]["stop_reason"] == "the provider said no"


def test_a_failed_read_closes_its_attempt_as_failed(monkeypatch, _pipeline):
    monkeypatch.setattr(
        upload, "audit_tender", lambda *a, **k: _audit(failure="the spec was unreadable")
    )

    status = upload.ingest("T1")

    assert status == TenderStatus.FAILED.value
    assert FakeRepo.finished[0]["status"] == "failed"
    assert FakeRepo.finished[0]["stop_reason"] == "the spec was unreadable"


def test_an_attempt_that_could_not_be_opened_does_not_stop_the_read(_pipeline):
    FakeRepo.fail_start = True

    status = upload.ingest("T1")

    # The read is the answer; the ledger is bookkeeping.
    assert status == TenderStatus.DONE.value
    assert FakeRepo.finished == []


def test_an_attempt_that_could_not_be_closed_does_not_lose_the_state(_pipeline):
    FakeRepo.fail_finish = True

    status = upload.ingest("T1")

    assert status == TenderStatus.DONE.value
    assert FakeRepo.row["status"] == TenderStatus.DONE.value


def test_the_cache_without_provenance_keeps_its_old_shape():
    cache = LLMCache(None)
    assert cache.provenance is None

    cache = LLMCache(None, tender_id="T1", reading_id="A1")
    assert cache.provenance == {"tender_id": "T1", "reading_id": "A1"}


def test_read_again_forces_local_pipeline_but_keeps_the_shared_answer_cache(monkeypatch, _pipeline):
    calls = []
    monkeypatch.setattr(upload, 'audit_tender', lambda *args, **kwargs: calls.append(kwargs) or _audit())
    assert upload.ingest('T1', force=True) == 'done'
    assert calls[0]['force'] is True and calls[0]['cache'] == 'cache'
