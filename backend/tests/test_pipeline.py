"""The pipeline: stages, invalidation and the LLM cache.

The cases that matter: a tender that works, a changed input, a cached
rerun, a malformed attachment and an ambiguous item mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services import pipeline
from app.services.pipeline import (
    STAGE_ATTACHMENTS,
    STAGE_EXTRACTION,
    STAGE_ITEMS,
    STAGE_REQUIREMENTS,
    STAGE_RESOLUTION,
    content_hash,
    run_pipeline,
    stage_keys,
)


class FakeSession:
    """The pipeline's session, counted: a write the stream cannot see does not
    count, so the progress write has to be committed (D-82)."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class Clock:
    """A clock the test moves, so the write interval is not a race."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 100.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now


class FakeRepo:
    """A repository that keeps the stage checkpoints in memory."""

    def __init__(self):
        self.session = FakeSession()
        self.stages: dict[str, str] = {}
        self.statuses: dict[str, str] = {}
        self.details: dict[str, str] = {}
        self.attachments: list[dict] = []
        self.items: list[dict] = []
        self.requirements: list = []
        # How many requirements each write held, in order.
        self.requirement_writes: list[int] = []

    def stage_keys(self, tender_id):
        return dict(self.stages)

    def stage_statuses(self, tender_id):
        return dict(self.statuses)

    def upsert_attachment(self, tender_id, record):
        self.attachments.append(record)
        return "attachment-1"

    def replace_items(self, tender_id, items):
        self.items = list(items)
        return len(self.items)

    def replace_requirements(self, tender_id, requirements):
        self.requirements = list(requirements)
        self.requirement_writes.append(len(self.requirements))
        return len(self.requirements)

    def save_stage(self, tender_id, section, **kwargs):
        self.stages[section] = kwargs.get("key", "")
        self.statuses[section] = kwargs.get("status", "")
        if kwargs.get("detail"):
            self.details[section] = kwargs["detail"]


class FakeCache:
    """A cache that counts the calls it saves."""

    def __init__(self):
        self.entries: dict[tuple, list] = {}
        self.hits = 0

    def get(self, input_hash, model, prompt_version, schema_version):
        key = (input_hash, model, prompt_version, schema_version)
        if key in self.entries:
            self.hits += 1
            return self.entries[key]
        return None

    def put(self, input_hash, model, prompt_version, schema_version, output):
        self.entries[(input_hash, model, prompt_version, schema_version)] = output


class CountingClient:
    """An LLM client that counts the calls and answers with one requirement."""

    def __init__(self, answer=None):
        self.calls = 0
        self.answer = answer

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        if self.answer is not None:
            return self.answer
        return {
            "requirements": [
                {
                    "text": f"read: {user}",
                    "span": [0, len(user)],
                    "type": "technical",
                    "attribute": "body material",
                    "operator": "eq",
                    "value": "cast carbon steel",
                    "unit": None,
                    "mandatory": True,
                }
            ]
        }


def _boq_csv(path: Path) -> None:
    path.write_text(
        "Item Number,Item Title,Item Description,Item Quantity,Unit of Measure\n"
        "1,GATE VALVE SIZE 150 MM CLASS 150,Flanged Gate valve 6 inch,13,pieces\n"
        "2,CHECK VALVE 100 MM,Wafer check valve,4,pieces\n",
        encoding="utf-8",
    )


def _stage_keys_for(attachments: list[dict]) -> dict[str, str]:
    """The stage keys, computed the way the pipeline computes them."""
    from app.services.extractor import PROMPT_VERSION
    from app.services.pipeline import _vocabulary_hash

    settings = get_settings()
    return stage_keys(
        attachments,
        prompt_version=PROMPT_VERSION,
        schema_version=settings.schema_version,
        model=settings.llm_model or "",
        vocabulary_hash=_vocabulary_hash(),
        fetch_config=settings.fetch_allowed_domains,
    )


def _spec_rows() -> list[dict]:
    """The rows of a specification: one per item in the bill of quantities."""
    return [
        {
            "row": 1,
            "page": 1,
            "number": "1",
            "title": "GATE VALVE SIZE 150 MM CLASS 150",
            "description": "Cast carbon steel Face to face distance 267mm",
        },
        {
            "row": 2,
            "page": 1,
            "number": "2",
            "title": "CHECK VALVE 100 MM",
            "description": "Wafer check valve Working pressure 8 bar",
        },
    ]


def _spec_pdf(path: Path) -> None:
    """A specification PDF with a bill-of-quantities table."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    columns = (55, 95, 235, 430, 465, 510)
    top = 60
    for index, cell in enumerate(["Sr. No.", "Item", "Descriptions", "Qty", "Unit"]):
        page.insert_text((columns[index], top), cell, fontsize=7)
    top += 18
    for number, title, description, qty in [
        ("1", "GATE VALVE SIZE 150 MM CLASS 150", "Cast carbon steel Face to face distance 267mm", "13"),
        ("2", "CHECK VALVE 100 MM", "Wafer check valve Working pressure 8 bar", "4"),
    ]:
        for index, value in enumerate([number, title, description, qty, "Nos."]):
            page.insert_text((columns[index], top), value, fontsize=7)
        top += 18
    bottom = top
    for index in range(len(columns) - 1):
        page.draw_line(
            pymupdf.Point(columns[index], 40),
            pymupdf.Point(columns[index], bottom),
            width=0.5,
        )
    page.draw_line(
        pymupdf.Point(columns[-1], 40), pymupdf.Point(columns[-1], bottom), width=0.5
    )
    for y in list(range(40, bottom, 18)):
        page.draw_line(pymupdf.Point(columns[0], y), pymupdf.Point(columns[-1], y), width=0.5)
    document.save(path)
    document.close()


def _attachments(tmp_path: Path, **overrides) -> tuple[list[dict], Path]:
    """A tender directory with a bill of quantities and a specification."""
    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    _boq_csv(raw / "boq.csv")
    _spec_pdf(raw / "spec.pdf")

    attachments = [
        {
            "url": "https://example.test/boq.csv",
            "filename": "boq.csv",
            "path": "raw/boq.csv",
            "sha256": "a" * 64,
            "kind": "boq-csv",
            "status": "new",
        },
        {
            "url": "https://example.test/spec.pdf",
            "filename": "spec.pdf",
            "path": "raw/spec.pdf",
            "sha256": "b" * 64,
            "kind": "boq-pdf",
            "status": "new",
        },
    ]
    for index, attachment in enumerate(attachments):
        attachments[index] = {**attachment, **overrides.get(attachment["kind"], {})}
    return attachments, tmp_path


def test_a_tender_is_processed_through_every_stage(tmp_path, monkeypatch):
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.failure is None
    assert len(result.items) == 2
    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_DONE
    assert result.llm_calls == 2


def test_a_unchanged_tender_is_not_processed_again(tmp_path, monkeypatch):
    """A second run over an unchanged tender makes no LLM calls."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)
    repo.statuses = {section: pipeline.STATUS_DONE for section in repo.stages}

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert client.calls == 0
    assert result.llm_calls == 0
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_SKIPPED


def test_a_changed_attachment_re_reads_the_items_and_the_requirements(tmp_path, monkeypatch):
    """The chain: an attachment's hash changes, so the items and the
    requirements are read again."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)

    # The bill of quantities changed.
    changed = [dict(record) for record in attachments]
    changed[0]["sha256"] = "c" * 64

    result = run_pipeline(
        "T1", directory, changed, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_DONE
    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_DONE
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE


def test_a_changed_reader_re_reads_the_items_and_the_requirements(tmp_path, monkeypatch):
    """A change to the code that reads a bill of quantities is a change to
    what the stage produces, so the stage runs again."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)

    # The reader changed.
    import app.services.tenderingest as reader

    monkeypatch.setattr(reader, "READER_VERSION", "changed")
    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_DONE
    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_DONE


def test_a_changed_classifier_re_reads_the_attachments(tmp_path, monkeypatch):
    """A change to the code that recognises a document is a change to what
    the attachment stage produces."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()

    repo.stages = _stage_keys_for(attachments)

    import app.services.tendercollect as collector

    monkeypatch.setattr(collector, "CLASSIFIER_VERSION", "changed")
    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert result.stages[STAGE_ATTACHMENTS].status == pipeline.STATUS_DONE
    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_DONE


def test_a_changed_prompt_re_extracts_but_does_not_re_read_the_items(tmp_path, monkeypatch):
    """The chain: a change to the prompt re-extracts, and the stages before it
    do not move."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)

    # The prompt version changed: the extraction key changes, the item key
    # does not. It is read from the extractor, which owns the prompt.
    import app.services.extractor as extractor

    monkeypatch.setattr(extractor, "PROMPT_VERSION", "2")
    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_SKIPPED
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert client.calls == 2


def test_a_changed_model_re_extracts(tmp_path, monkeypatch):
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)
    monkeypatch.setenv("LLM_MODEL", "new-model")
    get_settings.cache_clear()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE


def test_a_changed_vocabulary_re_resolves_only(tmp_path, monkeypatch):
    """A change to the vocabulary changes the resolution and nothing before
    it: no re-read, no re-extract."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)
    monkeypatch.setattr(pipeline, "_vocabulary_hash", lambda: "new-vocabulary")

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_SKIPPED
    assert result.stages[STAGE_RESOLUTION].status == pipeline.STATUS_DONE
    assert client.calls == 0


def test_a_cached_answer_is_reused_and_is_not_a_call(tmp_path, monkeypatch):
    """The same input, read again, is a cache hit and not a call."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    cache = FakeCache()
    client = CountingClient()

    first = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=cache, client=client
    )
    assert client.calls == 2

    # The cache is warm: the same requirements are read again.
    second = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=cache, client=client
    )
    assert client.calls == 2
    assert second.llm_cache_hits == 2
    assert second.llm_calls == 0
    assert first.llm_calls == 2


def test_a_tender_with_no_bill_of_quantities_fails_with_a_reason(tmp_path):
    attachments, directory = _attachments(tmp_path)
    # Only a form was fetched: there is nothing to read the items from.
    attachments = [dict(record, kind="form", status="new") for record in attachments]

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    assert result.failure is not None
    assert "bill of quantities" in result.failure
    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_FAILED


def test_a_tender_with_no_specification_fails_with_a_reason(tmp_path):
    attachments, directory = _attachments(tmp_path)
    # The bill of quantities is a CSV, and the specification is not there.
    # A CSV cannot be read as a specification: it has no pages.
    attachments = [
        dict(record, kind="boq-csv" if record["kind"] == "boq-csv" else "form")
        for record in attachments
    ]

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    assert result.failure is not None
    assert "specification" in result.failure


def test_a_malformed_attachment_does_not_stop_the_others(tmp_path, monkeypatch):
    """A file that cannot be read is a file that cannot be read."""
    attachments, directory = _attachments(tmp_path)
    (directory / "raw" / "spec.pdf").write_bytes(b"not a pdf")
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    # The bill of quantities is still read.
    assert len(result.items) == 2


def test_a_requirement_that_names_no_item_is_flagged_not_guessed(tmp_path, monkeypatch):
    """An ambiguous item mapping is persisted with review_required and a
    reason, never guessed."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: [
            {
                "row": 1,
                "page": 1,
                "number": "99",
                "title": "SOMETHING ELSE",
                "description": "A requirement that names no item",
            }
        ],
    )

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    ambiguous = [r for r in result.requirements if r.review_required]
    assert len(ambiguous) == 1
    assert ambiguous[0].item_id == ""
    assert "no item" in (ambiguous[0].review_reason or "")


def test_a_line_that_cannot_be_read_is_flagged_not_dropped(tmp_path, monkeypatch):
    """A mangled line is a gap, and a gap has to be visible."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: [
            {
                "row": 1,
                "page": 1,
                "number": "1",
                "title": "GATE VALVE SIZE 150 MM CLASS 150",
                "description": "� mangled �",
            }
        ],
    )

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    flagged = [r for r in result.requirements if r.review_required]
    assert flagged
    assert "could not be read" in (flagged[0].review_reason or "")


def test_the_stage_keys_follow_the_chain():
    """A stage's key depends on the stage before it, so a change upstream
    changes everything downstream."""
    attachments = [
        {"kind": "boq-csv", "sha256": "a" * 64, "status": "new"},
        {"kind": "boq-pdf", "sha256": "b" * 64, "status": "new"},
    ]
    keys = stage_keys(
        attachments,
        prompt_version="1",
        schema_version="1",
        model="m",
        vocabulary_hash="v",
    )
    assert set(keys) == {
        STAGE_ATTACHMENTS,
        STAGE_ITEMS,
        STAGE_REQUIREMENTS,
        STAGE_EXTRACTION,
        STAGE_RESOLUTION,
    }
    # Every key is different, and each one is built on the one before.
    assert len(set(keys.values())) == len(keys)

    changed = [dict(record) for record in attachments]
    changed[0]["sha256"] = "c" * 64
    after = stage_keys(
        changed,
        prompt_version="1",
        schema_version="1",
        model="m",
        vocabulary_hash="v",
    )
    assert after[STAGE_ITEMS] != keys[STAGE_ITEMS]
    assert after[STAGE_REQUIREMENTS] != keys[STAGE_REQUIREMENTS]
    assert after[STAGE_EXTRACTION] != keys[STAGE_EXTRACTION]
    assert after[STAGE_RESOLUTION] != keys[STAGE_RESOLUTION]


def test_a_prompt_change_does_not_move_the_stages_before_the_extraction():
    attachments = [
        {"kind": "boq-csv", "sha256": "a" * 64, "status": "new"},
        {"kind": "boq-pdf", "sha256": "b" * 64, "status": "new"},
    ]
    before = stage_keys(
        attachments,
        prompt_version="1",
        schema_version="1",
        model="m",
        vocabulary_hash="v",
    )
    after = stage_keys(
        attachments,
        prompt_version="2",
        schema_version="1",
        model="m",
        vocabulary_hash="v",
    )
    assert before[STAGE_ATTACHMENTS] == after[STAGE_ATTACHMENTS]
    assert before[STAGE_ITEMS] == after[STAGE_ITEMS]
    assert before[STAGE_REQUIREMENTS] == after[STAGE_REQUIREMENTS]
    assert before[STAGE_EXTRACTION] != after[STAGE_EXTRACTION]
    assert before[STAGE_RESOLUTION] != after[STAGE_RESOLUTION]


def test_a_vocabulary_change_does_not_move_the_stages_before_the_resolution():
    attachments = [
        {"kind": "boq-csv", "sha256": "a" * 64, "status": "new"},
        {"kind": "boq-pdf", "sha256": "b" * 64, "status": "new"},
    ]
    before = stage_keys(
        attachments,
        prompt_version="1",
        schema_version="1",
        model="m",
        vocabulary_hash="v1",
    )
    after = stage_keys(
        attachments,
        prompt_version="1",
        schema_version="1",
        model="m",
        vocabulary_hash="v2",
    )
    assert before[STAGE_ATTACHMENTS] == after[STAGE_ATTACHMENTS]
    assert before[STAGE_ITEMS] == after[STAGE_ITEMS]
    assert before[STAGE_REQUIREMENTS] == after[STAGE_REQUIREMENTS]
    assert before[STAGE_EXTRACTION] == after[STAGE_EXTRACTION]
    assert before[STAGE_RESOLUTION] != after[STAGE_RESOLUTION]


def test_a_content_hash_is_stable():
    assert content_hash("a", "b") == content_hash("a", "b")
    assert content_hash("a", "b") != content_hash("ab")
    assert content_hash("a", "b") != content_hash("a", "c")


class FlakyClient:
    """A client that fails on a chosen call.

    It carries the counters a real client carries, so the pipeline's reporting
    is exercised and not just its control flow (D3/D4).
    """

    def __init__(self, fail_on: int, error: Exception):
        self.calls = 0
        self.fail_on = fail_on
        self.error = error
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.retries = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.error
        self.prompt_tokens += 60
        self.completion_tokens += 15
        return {
            "requirements": [
                {
                    "text": f"read: {user}",
                    "span": [0, len(user)],
                    "type": "technical",
                    "attribute": "body material",
                    "operator": "eq",
                    "value": "cast carbon steel",
                    "unit": None,
                    "mandatory": True,
                }
            ]
        }


def test_a_line_the_model_could_not_read_is_flagged_not_dropped(tmp_path, monkeypatch):
    """A failed extraction is a gap in the reading, not a line the tender did
    not state (AGENTS #3, #4). It stays, and it is flagged."""
    from app.services.llm import LLMError

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    client = FlakyClient(1, LLMError("the answer was not JSON"))

    result = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert result.rate_limited is False
    # The first line was kept and flagged; the second was read normally.
    assert len(result.requirements) == 2
    flagged = [row for row in result.requirements if row.review_required]
    assert len(flagged) == 1
    assert "could not read this line" in (flagged[0].review_reason or "")
    assert result.llm_calls == 2


def test_a_rate_limit_stops_the_stage_and_keeps_the_unread_tail(tmp_path, monkeypatch):
    """The Phase 12 rule: stop at the first limit, record it, resume later.

    The line that was answered and the line that was never asked about are
    both kept. Dropping the tail would read as a shorter tender than it is.
    """
    from app.services.llm import LLMRateLimited

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    client = FlakyClient(2, LLMRateLimited("the model is rate limited", retry_after=24.9))
    repo = FakeRepo()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.rate_limited is True
    assert "rate limited" in (result.rate_limited_detail or "")
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_RATE_LIMITED
    # The stage's row carries the reason, so a resumed run can read it (D9).
    assert "rate limited" in repo.details[STAGE_EXTRACTION]
    # Both attempts were charged to the account, so both are reported.
    assert result.llm_calls == 2
    # The one answer that came back is reported with what it cost.
    assert result.prompt_tokens == 60
    assert result.completion_tokens == 15
    # The answered line and the unread tail are both there; nothing vanished.
    assert len(result.requirements) == 2


def test_a_rate_limit_before_the_first_line_keeps_every_requirement(tmp_path, monkeypatch):
    """Stopping on the first call must not empty the tender."""
    from app.services.llm import LLMRateLimited

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    client = FlakyClient(1, LLMRateLimited("the model is rate limited"))

    result = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_RATE_LIMITED
    assert len(result.requirements) == 2
    assert result.llm_calls == 1


def test_a_rate_limited_stage_is_read_again_on_the_next_run(tmp_path, monkeypatch):
    """Resuming a rate-limited tender re-reads the stage it stopped in.

    A resumed upload has the same attachments, so every stage key is
    unchanged. The extraction is still unfinished, and a key-only check would
    skip it and call the tender done (D15).
    """
    from app.services.llm import LLMRateLimited

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()

    first = run_pipeline(
        "T1",
        directory,
        attachments,
        repo=repo,
        cache=FakeCache(),
        client=FlakyClient(1, LLMRateLimited("the model is rate limited")),
    )
    assert first.stages[STAGE_EXTRACTION].status == pipeline.STATUS_RATE_LIMITED

    # The provider's limit has passed; the same files are uploaded again.
    client = CountingClient()
    second = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert second.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert second.rate_limited is False
    assert client.calls == 2
    # Only the stage that stopped short is repeated.
    assert second.stages[STAGE_ITEMS].status == pipeline.STATUS_SKIPPED
    assert repo.statuses[STAGE_EXTRACTION] == pipeline.STATUS_DONE


# --- the live requirement list (D-82) -------------------------------------


def test_the_extraction_writes_what_it_has_read_so_far(tmp_path, monkeypatch):
    """The UI shows the requirements as they are read, not after the last one.

    Each write is a complete snapshot committed while the stage runs: an
    uncommitted row is a requirement the streaming reader cannot see.
    """
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    monkeypatch.setattr(pipeline, "time", Clock(step=2.0))
    repo = FakeRepo()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert result.failure is None
    # One write per requirement read, then the final complete set.
    assert repo.requirement_writes == [1, 2, 2]
    assert repo.session.commits == 2


def test_the_progress_write_is_no_more_often_than_the_interval(tmp_path, monkeypatch):
    """A read faster than the interval does not write once per requirement."""
    monkeypatch.setenv("STREAM_INTERVAL_SECONDS", "5")
    get_settings.cache_clear()

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    monkeypatch.setattr(pipeline, "time", Clock(step=0.5))
    repo = FakeRepo()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert result.failure is None
    # The second requirement is read inside the interval, so it is only in the
    # final write: a partial snapshot exists to be shown, not to be written.
    assert repo.requirement_writes == [1, 2]
    assert repo.session.commits == 1
