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


def test_a_rate_limited_tender_is_not_stored_as_done(tmp_path, monkeypatch):
    """The extraction stopped at the provider's limit. The tender keeps what
    was read, reports the limit, and is neither done nor failed (D8)."""
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
    detail = "the model was rate limited; retry in 24.9s"
    result = SimpleNamespace(
        items=[],
        requirements=[],
        pages=[],
        failure=None,
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
