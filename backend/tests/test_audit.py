"""The audit's record of a rate-limited tender (D8, D9)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.schemas.enums import TenderStatus
from app.services.corpus import audit


class FakeRepo:
    def __init__(self):
        self.status = None

    def resolve_tender(self, manifest, tender_dir):
        return "T1"

    def set_status(self, tender_id, status, *, error=None):
        self.status = (status.value, error)


class FakeSession:
    """A session that remembers being rolled back."""

    def __init__(self):
        self.rolled_back = 0

    def rollback(self):
        self.rolled_back += 1


def _tender_dir(tmp_path) -> "object":
    directory = tmp_path / "T1"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "tender_id": "T1",
                "filename": "T1.pdf",
                "wrapper_sha256": "a" * 64,
                "category": "valve",
                "documents": [],
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_a_rate_limited_tender_is_not_stored_as_done(tmp_path, monkeypatch):
    """The extraction stopped at the provider's limit. The tender keeps what
    was read, reports the limit, and is neither done nor failed (D8)."""
    directory = _tender_dir(tmp_path)
    detail = "the model was rate limited; retry in 24.9s"
    result = SimpleNamespace(
        items=[],
        requirements=[],
        pages=[],
        failure=None,
        failure_code=None,
        llm_calls=5,
        llm_cache_hits=0,
        prompt_tokens=100,
        completion_tokens=200,
        llm_retries=0,
        rate_limited=True,
        rate_limited_detail=detail,
        stages={"extraction": SimpleNamespace(status="rate_limited")},
    )
    monkeypatch.setattr(audit, "run_pipeline", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        audit,
        "resolve_roles",
        lambda attachments: SimpleNamespace(specification=1, boq=1),
    )
    repo = FakeRepo()

    tender = audit.audit_tender(directory, repo=repo)

    assert tender.rate_limited is True
    assert tender.rate_limited_detail == detail
    assert repo.status == (TenderStatus.RATE_LIMITED.value, detail)


def test_a_failed_pipeline_leaves_the_session_able_to_record_the_failure(
    tmp_path, monkeypatch
):
    """The bug this guards: a rejected delete poisoned the transaction, the
    caller's `set_status` failed too, and the tender stayed `processing` with
    nothing left to move it (D-128)."""

    def boom(*args, **kwargs):
        raise RuntimeError("ForeignKeyViolation: result_requirement_id_fkey")

    directory = _tender_dir(tmp_path)
    monkeypatch.setattr(audit, "run_pipeline", boom)
    repo = FakeRepo()
    repo.session = FakeSession()

    tender = audit.audit_tender(directory, repo=repo)

    assert "RuntimeError" in (tender.failure or "")
    assert repo.session.rolled_back == 1
