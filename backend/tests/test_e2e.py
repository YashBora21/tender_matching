"""The one-command path: what it checks, what it does, and where it stops.

No database and no model are here. The steps are replaced by recorded stubs,
because the behaviour under test is the order of them and the decision to stop
before the expensive one - not the work inside each step, which has its own
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.cli import e2e
from app.services.cli.e2e import E2EResult, Facts, Step, assess, main, run_end_to_end


def test_an_emptier_corpus_blocks_and_names_the_two_ways_to_fix_it():
    blockers, next_steps = assess(Facts(documents=0, files_on_disk=0))

    assert blockers == [
        "the corpus is empty: the database holds no document and neither does the data directory"
    ]
    assert any("corpus-restore" in hint for hint in next_steps)
    assert any("make collect" in hint for hint in next_steps)


def test_a_corpus_on_disk_but_not_ingested_is_work_to_do_not_a_blocker():
    blockers, next_steps = assess(Facts(documents=0, files_on_disk=81))

    assert blockers == []
    assert any("ingests the 81 document(s) on disk" in hint for hint in next_steps)


def test_a_missing_model_blocks_a_tender_whose_clauses_need_reading():
    blockers, next_steps = assess(
        Facts(
            documents=10,
            chunks=200,
            model_reason="LLM_API_KEY is not set",
            requirements=12,
            needs_model=True,
        )
    )

    assert blockers == ["no model is configured: LLM_API_KEY is not set"]
    assert any("LLM_API_KEY" in hint for hint in next_steps)


def test_a_missing_model_does_not_block_a_tender_that_is_all_commercial():
    """An unsupported clause is answered from its type, with no call (D-108)."""
    blockers, next_steps = assess(
        Facts(documents=10, chunks=200, model_reason="LLM_API_KEY is not set", requirements=4)
    )

    assert blockers == []
    assert any("non-technical" in hint for hint in next_steps)


def test_a_tender_that_has_not_been_read_yet_is_stated_as_the_work():
    blockers, next_steps = assess(Facts(documents=10, chunks=200, tender_status="queued"))

    assert blockers == []
    assert any("reads the tender now" in hint for hint in next_steps)


def test_an_exit_code_of_two_means_nothing_was_attempted():
    result = E2EResult(blockers=["no model is configured"])

    assert result.exit_code == 2


def test_an_exit_code_of_one_means_a_step_was_attempted_and_failed():
    result = E2EResult(steps=[Step(e2e.STEP_CORPUS, "failed", "nothing was read")])

    assert result.exit_code == 1


def test_a_report_written_is_a_zero():
    result = E2EResult(ok=True)

    assert result.exit_code == 0


def test_a_check_that_finds_nothing_missing_is_a_zero():
    """A script asks "would a run go here?" and 0 is the answer."""
    assert E2EResult(check_only=True).exit_code == 0


def test_a_check_that_finds_a_blocker_says_so_without_a_failed_step():
    result = E2EResult(check_only=True, blockers=["no model is configured"])

    assert result.exit_code == 2


@pytest.mark.integration
def test_the_preflight_route_answers_about_the_database_it_is_on(test_database_url: str):
    """The route is the same measurement the command makes, as JSON."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.api.deps import get_session
    from app.main import create_app

    engine = create_engine(test_database_url)
    session = Session(engine, future=True)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    try:
        body = TestClient(app).get("/api/preflight").json()
    finally:
        session.close()
        engine.dispose()

    assert {"corpus", "model", "tender", "can_run", "why_not", "next_steps"} <= set(body)
    assert {"documents", "chunks", "manufacturers", "files_on_disk"} <= set(body["corpus"])
    assert isinstance(body["can_run"], bool)
    assert body["tender"]["stored"] is False


def test_the_text_shows_every_step_then_the_reason_and_the_fix():
    result = E2EResult(
        steps=[Step(e2e.STEP_CORPUS, "skipped", "81 documents are already in")],
        blockers=["no model is configured: LLM_API_KEY is not set"],
        next_steps=["set LLM_API_KEY in .env"],
        run_id="run-1",
    )

    text = result.as_text()

    assert "corpus: 81 documents are already in" in text
    assert "no model is configured: LLM_API_KEY is not set" in text
    assert "next" in text and "set LLM_API_KEY in .env" in text
    assert "run  run-1" in text


class _Calls:
    """The steps, recorded. Lets a test say which steps were reached."""

    def __init__(self) -> None:
        self.order: list[str] = []

    def install(self, monkeypatch, *, facts: Facts, tender_id: str = "tender-1") -> None:
        monkeypatch.setattr(e2e, "_stored_id", lambda session, reference: tender_id)
        monkeypatch.setattr(e2e, "measure", lambda session, tender_id=None: facts)

        def step(name, state="done", detail=""):
            self.order.append(name)
            return [Step(name, state, detail)]

        monkeypatch.setattr(e2e, "_corpus", lambda session, facts: step(e2e.STEP_CORPUS))
        monkeypatch.setattr(
            e2e,
            "_embedding",
            lambda session, facts: step(e2e.STEP_EMBEDDING)[0],
        )
        monkeypatch.setattr(
            e2e,
            "_tender",
            lambda session, reference: (step(e2e.STEP_TENDER)[0], tender_id),
        )
        monkeypatch.setattr(e2e, "_needs_a_model", lambda session, tid: None)
        monkeypatch.setattr(
            e2e,
            "_matching",
            lambda session, tid, manufacturers=None: (
                step(e2e.STEP_MATCHING)[0],
                "run-1",
            ),
        )

        def fake_report(session, run_id, out_dir):
            self.order.append(e2e.STEP_REPORT)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"report-{run_id}.json"
            path.write_text("{}", encoding="utf-8")
            return Step(e2e.STEP_REPORT, "done", str(path)), {"json": str(path)}

        monkeypatch.setattr(e2e, "_report", fake_report)


@pytest.fixture
def calls(monkeypatch):
    recorder = _Calls()
    return recorder


def _ready_facts(**changes) -> Facts:
    facts = Facts(documents=81, chunks=1400, manufacturers=9, files_on_disk=81, tender_status="done")
    for key, value in changes.items():
        setattr(facts, key, value)
    return facts


def test_a_healthy_machine_walks_the_chain_and_writes_the_report(monkeypatch, calls, tmp_path):
    calls.install(monkeypatch, facts=_ready_facts())

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path / "out")

    assert calls.order == [
        e2e.STEP_CORPUS,
        e2e.STEP_EMBEDDING,
        e2e.STEP_TENDER,
        e2e.STEP_MATCHING,
        e2e.STEP_REPORT,
    ]
    assert result.ok
    assert result.run_id == "run-1"
    assert Path(result.files["json"]).read_text(encoding="utf-8") == "{}"


def test_a_check_says_the_answer_and_touches_no_step(monkeypatch, calls, tmp_path):
    calls.install(monkeypatch, facts=_ready_facts())

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path, check_only=True)

    assert calls.order == []
    assert result.steps == []
    assert result.blockers == []


def test_an_unconfigured_model_stops_before_any_step_is_taken(monkeypatch, calls, tmp_path):
    """The run is not created, so nothing sits at `queued` waiting for a call
    that cannot happen (D-108)."""
    calls.install(
        monkeypatch, facts=_ready_facts(model_reason="LLM_API_KEY is not set", needs_model=True)
    )

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path)

    assert calls.order == []
    assert result.run_id is None
    assert result.exit_code == 2


def test_a_corpus_that_cannot_be_read_stops_before_the_tender(monkeypatch, calls, tmp_path):
    calls.install(monkeypatch, facts=_ready_facts())
    monkeypatch.setattr(
        e2e, "_corpus", lambda session, facts: [Step(e2e.STEP_CORPUS, "failed", "nothing was read")]
    )

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path)

    assert e2e.STEP_TENDER not in calls.order
    assert result.steps == [Step(e2e.STEP_CORPUS, "failed", "nothing was read")]
    assert result.exit_code == 1


def test_a_tender_that_fails_to_read_stops_before_the_matching(monkeypatch, calls, tmp_path):
    calls.install(monkeypatch, facts=_ready_facts())
    monkeypatch.setattr(
        e2e,
        "_tender",
        lambda session, reference: (Step(e2e.STEP_TENDER, "failed", "status failed"), "tender-1"),
    )

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path)

    assert calls.order == [e2e.STEP_CORPUS, e2e.STEP_EMBEDDING]
    assert e2e.STEP_MATCHING not in calls.order
    assert result.tender_id == "tender-1"


def test_clauses_found_after_the_read_that_need_a_model_stop_the_matching(
    monkeypatch, calls, tmp_path
):
    """The tender read fine; it is the clauses in it that cannot be answered."""
    calls.install(monkeypatch, facts=_ready_facts())
    monkeypatch.setattr(
        e2e, "_needs_a_model", lambda session, tid: "LLM_MODEL is not set"
    )

    result = run_end_to_end("tender-1", session=object(), out_dir=tmp_path)

    assert e2e.STEP_MATCHING not in calls.order
    assert result.blockers[-1] == (
        "this tender's clauses are technical and no model is configured: LLM_MODEL is not set"
    )


def test_a_reference_that_is_not_a_tender_at_all_is_refused(monkeypatch, tmp_path):
    """A path that is not a PDF and an id that is not stored: nothing to read."""
    monkeypatch.setattr(e2e, "_stored_id", lambda session, reference: None)

    with pytest.raises(ValueError, match="neither a stored tender"):
        e2e._tender(object(), str(tmp_path / "notes.txt"))


def test_the_command_reports_through_the_standard_output(monkeypatch, calls, tmp_path, capsys):
    calls.install(monkeypatch, facts=_ready_facts())
    monkeypatch.setattr(
        e2e, "_report", lambda session, run_id, out_dir: (Step(e2e.STEP_REPORT, "done"), {})
    )

    class _Scope:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    # The command opens its own session; the steps are stubs, so whatever it
    # hands them is never touched.
    monkeypatch.setattr("app.repositories.db.session_scope", lambda: _Scope())
    code = main([str(tmp_path / "tender.pdf"), "--out", str(tmp_path / "out")])

    assert code == 0
    printed = capsys.readouterr().out
    assert "corpus" in printed
    assert "report" in printed


def test_the_model_line_names_the_endpoint_a_run_would_call():
    """A key issued by one provider and a base URL set for another is a
    refusal mid-run, so the preflight says which model and host it means."""
    facts = Facts(documents=10, chunks=200, model_label="some-model at https://api.example/v1")

    text = E2EResult(facts=facts, check_only=True).as_text()

    assert "configured: some-model at https://api.example/v1" in text


def test_a_run_stopped_by_the_budget_names_what_it_kept(monkeypatch):
    """A stopped match is not a lost one: the results stay and the same
    command continues from the pairs still missing (D-110)."""
    from types import SimpleNamespace

    from app.services.output import runs

    class _Session:
        def commit(self) -> None:
            pass

    monkeypatch.setattr(runs, "create_run", lambda session, tender_id, manufacturers=None: "run-1")
    monkeypatch.setattr(
        runs,
        "execute_run",
        lambda run_id, session=None: SimpleNamespace(
            status="rate_limited",
            results=7,
            manufacturers=["abb"],
            llm_calls=300,
            error="the call budget of 300 is spent (300 calls, 512000 tokens)",
        ),
    )

    step, run_id = e2e._matching(_Session(), "tender-1")

    assert run_id == "run-1"
    assert step.state == "failed"
    assert "7 result(s) kept" in step.detail
    assert "Run the same command again" in step.detail


def test_the_named_manufacturers_are_the_ones_compared(monkeypatch):
    """The whole corpus is 20 makers; a test run against three of them costs a
    fifteenth as much, so the command has to be able to name them."""
    from types import SimpleNamespace

    from app.services.output import runs

    seen = {}

    class _Session:
        def commit(self) -> None:
            pass

    def fake_create_run(session, tender_id, manufacturers=None):
        seen["manufacturers"] = manufacturers
        return "run-1"

    monkeypatch.setattr(runs, "create_run", fake_create_run)
    monkeypatch.setattr(
        runs,
        "execute_run",
        lambda run_id, session=None: SimpleNamespace(
            status="done", results=3, manufacturers=["delval_flow"], llm_calls=9, error=None
        ),
    )

    step, run_id = e2e._matching(_Session(), "tender-1", ["delval_flow"])

    assert seen["manufacturers"] == ["delval_flow"]
    assert run_id == "run-1"
    assert step.state == "done"


def test_the_command_reads_the_manufacturer_list(monkeypatch, calls, tmp_path):
    calls.install(monkeypatch, facts=_ready_facts())
    seen = {}

    def fake_matching(session, tid, manufacturers=None):
        seen["manufacturers"] = manufacturers
        return Step(e2e.STEP_MATCHING, "done"), "run-1"

    monkeypatch.setattr(e2e, "_matching", fake_matching)
    monkeypatch.setattr(
        e2e, "_report", lambda session, run_id, out_dir: (Step(e2e.STEP_REPORT, "done"), {})
    )

    class _Scope:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.repositories.db.session_scope", lambda: _Scope())
    main([str(tmp_path / "tender.pdf"), "--manufacturers", "delval_flow, Flowserve"])

    assert seen["manufacturers"] == ["delval_flow", "Flowserve"]
