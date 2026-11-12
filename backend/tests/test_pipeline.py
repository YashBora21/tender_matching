"""The pipeline: stages, invalidation and the LLM cache.

The cases that matter: a tender that works, a changed input, a cached
rerun, a malformed attachment and an ambiguous item mapping.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.schemas.enums import RequirementType
from app.services.tender import pipeline
from app.services.tender.pipeline import (
    STAGE_ATTACHMENTS,
    STAGE_EXTRACTION,
    STAGE_ITEMS,
    STAGE_REQUIREMENTS,
    STAGE_RESOLUTION,
    content_hash,
    run_pipeline,
    stage_keys,
)
from app.services.tender.ingest import PageRead, SpecificationRead


class FakeSession:
    """The pipeline's session, counted: a write the stream cannot see does not
    count, so the progress write has to be committed (D-82)."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1

    def execute(self, statement):
        class EmptyAliases:
            def all(self):
                return []
        return EmptyAliases()


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
        self.pages: list[dict] = []
        # What is in the requirement table, in the shape the repository reads
        # it back in. Ids are minted per write, like the real uuid4 (D-93).
        self.stored: list[dict] = []
        self._ids = 0
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
        self.stored = []
        for item in self.requirements:
            self._ids += 1
            self.stored.append(
                {
                    "id": f"r{self._ids}",
                    "item_id": item.item_id,
                    "text": item.text,
                    "source_text": item.source_text,
                    "source_span": item.source_span,
                    "page": item.page,
                    "source_section": item.section,
                    "extraction_method": item.extraction_method,
                    "type": item.requirement_type,
                    "attribute": item.attribute,
                    "review_required": item.review_required,
                    "review_reason": item.review_reason,
                    "value": item.value,
                    "unit": item.unit,
                    "operator": item.operator,
                    "confidence": item.confidence,
                    "mandatory": item.mandatory,
                }
            )
        return len(self.requirements)

    def list_requirements(self, tender_id):
        return [dict(row) for row in self.stored]

    def replace_pages(self, tender_id, pages):
        self.pages = list(pages)
        return len(self.pages)

    def store_ocr_table(self, *args):
        pass

    def list_pages(self, tender_id):
        return [dict(row) for row in self.pages]

    def list_attachments(self, tender_id):
        return [
            {"id": "attachment-1", "url": record.get("url"), "path": record.get("path")}
            for record in self.attachments
        ]

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
        if user.startswith('{'):
            envelope = json.loads(user)
            user = envelope.get('source_text', user)
        self.calls += 1
        if self.answer is not None:
            return self.answer
        return {
            "requirements": [
                {
                    "text": f"read: {user}",
                    "source_quote": user,
                    "type": "technical",
                    "attribute": "body material",
                    "operator": "eq",
                    "value": user,
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
    from app.services.requirement.extract import PROMPT_VERSION
    from app.services.tender.pipeline import _vocabulary_hash

    settings = get_settings()
    return stage_keys(
        attachments,
        prompt_version=PROMPT_VERSION,
        schema_version=settings.schema_version,
        model=settings.llm_model or "",
        vocabulary_hash=_vocabulary_hash(),
        fetch_config=settings.fetch_allowed_domains,
    )


def _spec_rows() -> SpecificationRead:
    """The rows of a specification: one per item in the bill of quantities."""
    return SpecificationRead(
        rows=[
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
    )


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
            "sha256": hashlib.sha256((raw / "boq.csv").read_bytes()).hexdigest(),
            "kind": "boq-csv",
            "status": "new",
        },
        {
            "url": "https://example.test/spec.pdf",
            "filename": "spec.pdf",
            "path": "raw/spec.pdf",
            "sha256": hashlib.sha256((raw / "spec.pdf").read_bytes()).hexdigest(),
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


def test_a_second_run_keeps_the_requirements_the_first_one_stored(tmp_path, monkeypatch):
    """A run with nothing to do does not re-read the clauses.

    The stored rows are the reader's and the model's output. Rebuilding them
    from the file would replace every type the model read with `unknown` and
    give every requirement a new id, so a second run would silently undo the
    first (D-93).
    """
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()

    first = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )
    assert first.llm_calls == 2
    types = [row["type"] for row in repo.stored]
    ids = [row["id"] for row in repo.stored]
    writes = list(repo.requirement_writes)
    # The model read them, so they are not raw clauses.
    assert types and all(kind == "technical" for kind in types)

    second = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert second.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_SKIPPED
    assert second.llm_calls == 0
    # Neither re-read nor rewritten: the types and the ids are the first
    # run's, and no new write was made.
    assert [row["type"] for row in repo.stored] == types
    assert [row["id"] for row in repo.stored] == ids
    assert repo.requirement_writes == writes
    assert len(second.requirements) == len(first.requirements)


def test_a_changed_attachment_re_reads_the_items_and_the_requirements(tmp_path, monkeypatch):
    """The chain: an attachment's hash changes, so the items and the
    requirements are read again."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    repo.stages = _stage_keys_for(attachments)

    # The synthetic bill of quantities changed, with its manifest updated consistently.
    changed = [dict(record) for record in attachments]
    path = directory / changed[0]['path']
    path.write_text(path.read_text() + "\n", encoding='utf-8')
    changed[0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

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
    import app.services.tender.ingest as reader

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

    import app.services.tender.collect as collector

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
    # The tender was read once, under the old prompt: its requirements are
    # stored, and a re-extraction reads those, not the file (D-93).
    run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )
    client = CountingClient()

    # The prompt version changed: the extraction key changes, the item key
    # does not. It is read from the extractor, which owns the prompt.
    import app.services.requirement.extract as extractor

    monkeypatch.setattr(extractor, "PROMPT_VERSION", "synthetic-changed-prompt")
    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_ITEMS].status == pipeline.STATUS_SKIPPED
    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_SKIPPED
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert client.calls == 2


def test_a_changed_model_re_extracts(tmp_path, monkeypatch):
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )
    client = CountingClient()

    monkeypatch.setenv("LLM_MODEL", "new-model")
    get_settings.cache_clear()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    # The stored requirements are read again, and the file is not.
    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_SKIPPED
    assert client.calls == 2


def test_a_changed_vocabulary_rechecks_coverage_and_resolves(tmp_path, monkeypatch):
    """Coverage depends on approved vocabulary as well as normalization."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    repo = FakeRepo()
    client = CountingClient()

    cache = FakeCache()
    run_pipeline("T1", directory, attachments, repo=repo, cache=cache, client=client)
    client.calls = 0
    monkeypatch.setattr(pipeline, "_vocabulary_hash", lambda: "new-vocabulary")

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=cache, client=client
    )

    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_SKIPPED
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert result.stages[STAGE_RESOLUTION].status == pipeline.STATUS_DONE
    assert client.calls == 0
    assert result.llm_cache_hits == 2


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
    attachments[1]['sha256'] = hashlib.sha256(b'not a pdf').hexdigest()
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo(), client=CountingClient())

    # The bill of quantities is still read.
    assert len(result.items) == 2


def test_a_requirement_that_names_no_item_is_flagged_not_guessed(tmp_path, monkeypatch):
    """An ambiguous item mapping is persisted with review_required and a
    reason, never guessed."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: SpecificationRead(
            rows=[
                {
                    "row": 1,
                    "page": 1,
                    "number": "99",
                    "title": "SOMETHING ELSE",
                    "description": "The valve shall be supplied with a test certificate",
                }
            ]
        ),
    )

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo(), client=CountingClient())

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
        lambda a, d: SpecificationRead(
            rows=[
                {
                    "row": 1,
                    "page": 1,
                    "number": "1",
                    "title": "GATE VALVE SIZE 150 MM CLASS 150",
                    "description": "� mangled �",
                }
            ]
        ),
    )

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo(), client=CountingClient())

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


def test_a_stored_row_is_stamped_with_the_versions_the_run_uses():
    """The cache key and the row have to say the same thing.

    `to_stored` used to default both versions to a literal written beside its
    signature while the stage keys were built from `settings.schema_version`,
    so a deployment that changed the setting kept reading rows it had already
    invalidated.
    """
    from app.schemas.models import EXTRACTION_VERSION
    from app.services.tender.pipeline import to_stored
    from app.services.tender.ingest import RequirementRecord

    stored = to_stored(RequirementRecord(text="Head >= 20 m", item_id="1"), "tender-1")

    assert stored.schema_version == get_settings().schema_version
    assert stored.extraction_version == EXTRACTION_VERSION


def test_a_vocabulary_change_invalidates_coverage_but_not_source_reading():
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
    assert before[STAGE_EXTRACTION] != after[STAGE_EXTRACTION]
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
        if user.startswith('{'):
            envelope = json.loads(user)
            user = envelope.get('source_text', user)
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.error
        self.prompt_tokens += 60
        self.completion_tokens += 15
        return {
            "requirements": [
                {
                    "text": f"read: {user}",
                    "source_quote": user,
                    "type": "technical",
                    "attribute": "body material",
                    "operator": "eq",
                    "value": user,
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

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_FAILED
    assert result.rate_limited is False
    # The first line was kept and flagged; the second was read normally.
    assert len(result.requirements) == 2
    flagged = [row for row in result.requirements if 'could not read this line' in (row.review_reason or '')]
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


def test_extraction_commits_cache_but_publishes_one_requirement_revision(tmp_path, monkeypatch):
    """Synthetic rows publish once, while cache transactions remain resumable."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _spec_rows())
    monkeypatch.setattr(pipeline, "time", Clock(step=2.0))
    repo = FakeRepo()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert result.failure is None
    # No growing full-list snapshots or superseded progress revisions.
    assert repo.requirement_writes == [2]
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
    # Cache commits are throttled; final requirements are written once.
    assert repo.requirement_writes == [2]
    assert repo.session.commits == 1


def test_a_clause_the_model_called_unknown_is_re_typed_from_its_own_value(
    tmp_path, monkeypatch
):
    """The model's unknown is a proposal, not a verdict: a clause that states a
    value and a direction is technical, and the re-typing fills the fields."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: SpecificationRead(
            rows=[
                {
                    "row": 1,
                    "page": 1,
                    "number": "1",
                    "title": "GATE VALVE SIZE 150 MM CLASS 150",
                    "description": "minimum efficiency 85%",
                    "page_tag": "technical",
                }
            ]
        ),
    )
    client = CountingClient(
        answer={
            "requirements": [
                {
                    "text": "minimum efficiency 85%",
                    "source_quote": "minimum efficiency 85%",
                    "type": "unknown",
                    "attribute": None,
                    "operator": None,
                    "value": None,
                    "unit": None,
                    "mandatory": True,
                }
            ]
        }
    )

    result = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=FakeCache(), client=client
    )

    assert len(result.requirements) == 1
    read = result.requirements[0]
    assert read.requirement_type is RequirementType.TECHNICAL
    assert read.attribute == "efficiency"
    assert read.operator == "gte"
    assert read.value == "85"
    assert read.unit == "%"


def test_every_page_read_ends_in_the_ledger(tmp_path, monkeypatch):
    """The ledger accounts for every page, including the one that failed: a
    failed page is not a page that was processed (FR-T2)."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: SpecificationRead(
            rows=[
                {
                    "row": 1,
                    "page": 1,
                    "number": "1",
                    "title": "GATE VALVE SIZE 150 MM CLASS 150",
                    "description": "The body shall be cast carbon steel.",
                }
            ],
            pages=[
                PageRead(
                    page=1,
                    tag="technical",
                    status="parsed",
                    chars=1100,
                    method="table",
                ),
                PageRead(
                    page=2,
                    tag="boilerplate",
                    status="failed",
                    error="the OCR read nothing on this page",
                ),
            ],
        ),
    )
    repo = FakeRepo()

    result = run_pipeline(
        "T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient()
    )

    assert result.failure == 'source pages remain unreadable'
    assert [row["page"] for row in repo.pages] == [1, 2]
    assert [row["ledger_status"] for row in repo.pages] == ["parsed", "failed"]
    assert repo.pages[0]["extraction_method"] == "table"
    assert repo.pages[0]["tags"] == ["technical", "source_role:boq"]
    assert repo.pages[0]["attachment_id"] == "attachment-1"
    assert repo.pages[1]["skip_reason"] == "the OCR read nothing on this page"


def test_a_tender_read_before_the_ledger_existed_has_its_pages_recorded(tmp_path, monkeypatch):
    """A cached requirements stage is not an excuse for no page states.

    The stage key has not changed, so the requirements are not read again -
    but the ledger is empty, so the pages are: the cost is a local read, not a
    model call (FR-T2, D-91).
    """
    attachments, directory = _attachments(tmp_path)
    pages = [PageRead(page=1, tag="technical", status="parsed", chars=1100, method="table")]
    reads = []

    def fake_read(attachment, tender_dir):
        reads.append(attachment)
        return SpecificationRead(
            rows=[
                {
                    "row": 1,
                    "page": 1,
                    "number": "1",
                    "title": "GATE VALVE SIZE 150 MM CLASS 150",
                    "description": "The body shall be cast carbon steel.",
                }
            ],
            pages=list(pages),
        )

    monkeypatch.setattr(pipeline, "_read_specification", fake_read)
    repo = FakeRepo()

    run_pipeline("T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient())
    assert len(reads) == 1
    assert [row["page"] for row in repo.pages] == [1]

    # The ledger is gone but the requirements are stored: the state of a
    # tender read by an earlier build of this code.
    repo.pages = []
    run_pipeline("T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient())

    assert [row["page"] for row in repo.pages] == [1], "the empty ledger was filled in"
    assert len(reads) == 2

    # A third pass has nothing to backfill: the pages are not read again.
    run_pipeline("T1", directory, attachments, repo=repo, cache=FakeCache(), client=CountingClient())
    assert len(reads) == 2


def test_a_specification_that_cannot_be_opened_is_a_failure(tmp_path, monkeypatch):
    """A document that could not be opened is not a document with no
    requirements (PRD 4)."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(
        pipeline,
        "_read_specification",
        lambda a, d: SpecificationRead(error="FileNotFoundError: spec.pdf"),
    )

    result = run_pipeline("T1", directory, attachments, repo=FakeRepo())

    assert result.failure is not None
    assert "could not be read" in result.failure
    assert result.stages[STAGE_REQUIREMENTS].status == pipeline.STATUS_FAILED
    assert result.requirements == []


# --- the atomizer in production (D-105) -----------------------------------


def _compound_prose_spec() -> SpecificationRead:
    """One prose clause that states three facts."""
    return SpecificationRead(
        rows=[
            {
                "row": 1,
                "page": 1,
                "number": "1",
                "title": "GATE VALVE SIZE 150 MM CLASS 150",
                "description": "Body shall be SS316, protection IP68, speed 1500 RPM",
                "extraction_method": "text",
            }
        ]
    )


def test_a_compound_prose_clause_is_atomized_before_the_model_reads_it(
    tmp_path, monkeypatch
):
    """Each atom is its own requirement and its own model call (FR-13).

    Atomizing after extraction would leave one attribute and one value on
    three facts; the split happens first so each atom is read on its own.
    """
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _compound_prose_spec())
    client = CountingClient()

    result = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=FakeCache(), client=client
    )

    assert len(result.requirements) == 3
    assert client.calls == 3
    assert result.stages[STAGE_REQUIREMENTS].rows == 3
    assert result.stages[STAGE_EXTRACTION].rows == 3
    assert all(
        requirement.extraction_method == "prose"
        for requirement in result.requirements
    )
    assert all(requirement.item_id == "1" for requirement in result.requirements)


def test_a_rate_limit_mid_atoms_keeps_the_unread_atoms(tmp_path, monkeypatch):
    """Stopping on the provider's limit never drops an atom (D-105).

    The atoms were split before the model was asked, so the ones that were
    never read are still requirements; the stage says `rate_limited` and the
    next run resumes it.
    """
    from app.services.llm import LLMRateLimited

    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, "_read_specification", lambda a, d: _compound_prose_spec())
    client = FlakyClient(2, LLMRateLimited("the model is rate limited", retry_after=24.9))

    result = run_pipeline(
        "T1", directory, attachments, repo=FakeRepo(), cache=FakeCache(), client=client
    )

    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_RATE_LIMITED
    assert result.rate_limited is True
    assert len(result.requirements) == 3
    assert result.llm_calls == 2


# --- a scan nobody can read is not a document that states nothing (D-156) ---


def test_a_blank_scan_is_not_a_specification_that_states_nothing(tmp_path):
    """One page, read through OCR, before any model call: a specification
    whose pages hold no text is a different answer from one that states no
    requirement (PRD 4)."""
    import pymupdf

    blank = tmp_path / "scan.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(blank)
    document.close()

    assert pipeline._reads_nothing(blank) is True


def test_a_page_of_prose_is_readable(tmp_path):
    import pymupdf

    written = tmp_path / "spec.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (72, 72),
        "The valve body shall be cast steel, and the flange shall be drilled "
        "to ASME B16.5 class 150.",
    )
    document.save(written)
    document.close()

    assert pipeline._reads_nothing(written) is False


def test_wrapper_change_invalidates_reading_but_not_boq_items():
    """Synthetic wrapper hashes must invalidate source reading and downstream caches."""
    kwargs = dict(prompt_version="synthetic", schema_version="synthetic",
                  model="synthetic", vocabulary_hash="synthetic")
    before = stage_keys([], wrapper_fingerprint="synthetic-before", **kwargs)
    after = stage_keys([], wrapper_fingerprint="synthetic-after", **kwargs)
    assert before[STAGE_ITEMS] == after[STAGE_ITEMS]
    assert before[STAGE_REQUIREMENTS] != after[STAGE_REQUIREMENTS]
    assert before[STAGE_EXTRACTION] != after[STAGE_EXTRACTION]


def test_source_extraction_failure_is_retried_instead_of_cached_as_done(tmp_path, monkeypatch):
    """Synthetic malformed response must not strand an unchanged tender forever."""
    attachments, directory = _attachments(tmp_path)
    monkeypatch.setattr(pipeline, '_read_specification', lambda a, d: _spec_rows())
    repo = FakeRepo()
    failed = run_pipeline('T1', directory, attachments, repo=repo,
                          cache=FakeCache(), client=CountingClient(answer={'invalid': []}))
    assert failed.stages[STAGE_EXTRACTION].status == pipeline.STATUS_FAILED
    valid_client = CountingClient()
    retried = run_pipeline('T1', directory, attachments, repo=repo,
                           cache=FakeCache(), client=valid_client)
    assert retried.stages[STAGE_EXTRACTION].status != pipeline.STATUS_SKIPPED
    assert valid_client.calls > 0
    assert retried.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert not any('source_coverage_gap' in (r.review_reason or '') for r in retried.requirements)


def test_output_budget_change_rechecks_extraction_stage(monkeypatch):
    args = dict(prompt_version='synthetic', schema_version='synthetic',
                model='synthetic', vocabulary_hash='synthetic')
    monkeypatch.setenv('LLM_MAX_TOKENS', '512')
    get_settings.cache_clear()
    before = stage_keys([], **args)
    monkeypatch.setenv('LLM_MAX_TOKENS', '4096')
    get_settings.cache_clear()
    after = stage_keys([], **args)
    assert before[STAGE_REQUIREMENTS] == after[STAGE_REQUIREMENTS]
    assert before[STAGE_EXTRACTION] != after[STAGE_EXTRACTION]


def test_catalogue_absence_does_not_prevent_tender_extraction(tmp_path, monkeypatch):
    """Synthetic source extraction is independent of manufacturer inventory."""
    monkeypatch.setenv('OCR_BACKEND', 'paddle')
    get_settings.cache_clear()
    monkeypatch.setattr(pipeline, '_reader_version', lambda: 'synthetic-paddle-reader')
    monkeypatch.setattr(pipeline, '_ocr_signature', lambda: 'synthetic-paddle-signature')
    monkeypatch.setattr(pipeline, '_read_specification', lambda a, d: _spec_rows())
    def forbidden(*args, **kwargs):
        raise AssertionError('vendor catalogue queries belong to matching')
    monkeypatch.setattr('app.repositories.part_coverage.source_part', forbidden)
    monkeypatch.setattr('app.repositories.part_coverage.indexed_part_manufacturers', forbidden)
    attachments, directory = _attachments(tmp_path)
    client = CountingClient()
    result = run_pipeline('T1', directory, attachments, repo=FakeRepo(),
                          cache=FakeCache(), client=client)
    assert client.calls == 2
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE


@pytest.mark.parametrize('count', [2, 100, 332])
def test_progress_never_rewrites_the_growing_requirement_list(tmp_path, monkeypatch, count):
    """Synthetic large tender: constant full-list writes, independent of clause count."""
    attachments, directory = _attachments(tmp_path)
    boq = directory / 'raw' / 'boq.csv'
    boq.write_text('Item Number,Item Title,Item Description,Item Quantity,Unit of Measure\n' +
        ''.join(f'{i+1},Synthetic valve {i},Synthetic body material,1,pieces\n' for i in range(count)), encoding='utf-8')
    attachments[0]['sha256'] = hashlib.sha256(boq.read_bytes()).hexdigest()
    rows = [{'row':i+1,'page':1,'number':str(i+1),'title':f'Synthetic valve {i}',
             'description':f'body material SYNTHETIC-GRADE-{i}', 'page_tag':'technical'}
            for i in range(count)]
    monkeypatch.setattr(pipeline, '_read_specification', lambda a,d: SpecificationRead(rows=rows))
    monkeypatch.setattr(pipeline, 'time', Clock(step=2.0))
    repo=FakeRepo()
    result=run_pipeline('T1',directory,attachments,repo=repo,cache=FakeCache(),client=CountingClient())
    assert result.stages[STAGE_EXTRACTION].status == pipeline.STATUS_DONE
    assert repo.requirement_writes == [count]
    assert repo.session.commits == count


def test_no_specification_uses_cover_only_for_deterministic_candidates(tmp_path):
    """Synthetic cover certification remains readable without a spec attachment."""
    import pymupdf
    attachments, directory = _attachments(tmp_path)
    attachments = [row for row in attachments if row['kind']=='boq-csv']
    wrapper = directory/'synthetic-cover.pdf'
    document=pymupdf.open();page=document.new_page()
    page.insert_text((50,60),'Synthetic manufacturer must have ISO 9001 certification.')
    document.save(wrapper);document.close()
    (directory/'manifest.json').write_text(json.dumps({'filename':wrapper.name,
        'wrapper_path':str(wrapper),'wrapper_sha256':hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        'page_count':1,'documents':[]}),encoding='utf-8')
    class Reader:
        calls=0
        def complete_json(self,system,user,**kwargs):
            self.calls+=1
            return {'requirements':[{'text':user,'source_quote':user,'type':'eligibility',
                'attribute':None,'value':user,'unit':None,'operator':'eq','mandatory':True}]}
    client=Reader()
    result=run_pipeline('synthetic-tender',directory,attachments,client=client)
    assert result.failure is None and client.calls==1
    assert result.requirements and all(row.source_role=='cover' for row in result.requirements)
    assert all(row.context_only for row in result.requirements)
    assert [page.source_key for page in result.pages]==['wrapper']
