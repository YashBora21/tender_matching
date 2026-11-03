"""A matching run: creation, execution, failure and the per-manufacturer summary.

No database and no LLM: the repositories, the matcher and the store are fakes,
so what is tested is the orchestration - what is run, in what order, and what
is recorded when something goes wrong.
"""

from __future__ import annotations

import pytest

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    ReasonCode,
    RequirementStatus,
    RunStatus,
    VerificationOutcome,
)
from app.services.match.match import MatchResult
from app.services.output.runs import (
    _manufacturer_names,
    create_run,
    execute_run,
    progress,
    summarise,
)

RUN_ID = "11111111-1111-4111-8111-111111111111"
TENDER_ID = "22222222-2222-4222-8222-222222222222"


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeRunRepo:
    def __init__(self, *, manufacturers=("Acme",), status="queued") -> None:
        self.run = {
            "id": RUN_ID,
            "tender_id": TENDER_ID,
            "status": status,
            "started_at": None,
            "config_snapshot": {},
            "corpus_hash": "abc",
            "error": None,
            "metrics": {},
            "manufacturers": list(manufacturers),
        }
        self.stored: list[dict] = []
        self.history: list[tuple[str, str | None]] = []
        self.created: dict = {}
        self.resume_id = None
        self.compared = None
        self.metrics_writes: list[dict] = []

    def create(self, tender_id, *, manufacturers, config_snapshot, corpus_hash=""):
        self.created = {
            "tender_id": tender_id,
            "manufacturers": manufacturers,
            "config_snapshot": config_snapshot,
            "corpus_hash": corpus_hash,
        }
        self.run["manufacturers"] = list(manufacturers)
        return RUN_ID

    def resumable(self, tender_id, manufacturers, snapshot=None):
        self.compared = snapshot
        return self.resume_id

    def get(self, run_id):
        return dict(self.run) if run_id == RUN_ID else None

    def set_status(self, run_id, status, *, error=None):
        self.history.append((status.value, error))
        self.run["status"] = status.value
        if error is not None:
            self.run["error"] = error

    def claim(self, run_id):
        """The lock the row gives: only one pass takes a waiting run."""
        if self.run["status"] not in (
            RunStatus.QUEUED.value,
            RunStatus.RATE_LIMITED.value,
        ):
            return False
        self.set_status(run_id, RunStatus.RETRIEVING)
        return True

    def set_metrics(self, run_id, metrics):
        self.metrics_writes.append(dict(metrics))
        self.run["metrics"] = dict(metrics)

    def results(self, run_id):
        return list(self.stored)

    def count_results(self, run_id):
        return len(self.stored)

    def answered(self, run_id):
        return {
            (str(row["requirement_id"]), str(row["manufacturer_id"]))
            for row in self.stored
        }


class FakeTenderRepo:
    def __init__(self, requirements, items=None, pages=None, tender=None) -> None:
        self.requirements = requirements
        self.items = items or []
        self.pages = pages or []
        self.tender = tender if tender is not None else {"id": TENDER_ID}

    def get_tender(self, tender_id):
        return self.tender

    def list_requirements(self, tender_id):
        return list(self.requirements)

    def list_items(self, tender_id):
        return list(self.items)

    def list_pages(self, tender_id):
        return list(self.pages)

    def count_requirements(self, tender_id):
        return len(self.requirements)


REQUIREMENTS = [
    {
        "id": "r1",
        "item_id": "1",
        "text": "Body shall be SS316",
        "type": "technical",
        "mandatory": True,
        "value": None,
        "unit": None,
    },
    {
        "id": "r2",
        "item_id": "1",
        "text": "Delivery within 180 days of the purchase order",
        "type": "commercial",
        "mandatory": True,
        "value": None,
        "unit": None,
    },
]

ITEMS = [{"item_id": "1", "name": "GATE VALVE SIZE 150 MM", "category": "valves"}]


def _match(**overrides) -> MatchResult:
    values = dict(
        requirement_text="Body shall be SS316",
        status=RequirementStatus.FULL_MATCH,
        score=1.0,
        verification_outcome=VerificationOutcome.PASSED,
        applicability=Applicability.MODEL_LEVEL,
        capability_text="Body material SS316",
        confidence=ConfidenceLevel.HIGH,
    )
    values.update(overrides)
    return MatchResult(**values)


def _recording_matcher(calls, result=None, error=None):
    def match(session, text, **kwargs):
        calls.append({"text": text, **kwargs})
        if error is not None:
            raise error
        return result or _match()

    return match


def _recording_store(repo, names):
    def store(session, outcome, *, run_id, requirement_id, manufacturer_id):
        repo.stored.append(
            {
                "requirement_id": requirement_id,
                "manufacturer_id": manufacturer_id,
                "manufacturer": names.get(manufacturer_id, ""),
                "status": outcome.status.value,
                "verification_outcome": outcome.verification_outcome.value,
                "applicability": outcome.applicability.value,
                "score": outcome.score,
                "capability_text": outcome.capability_text,
                "confidence": outcome.confidence.value if outcome.confidence else None,
                "review_required": outcome.review_required,
                "review_reason": outcome.review_reason,
                "reason_code": (
                    outcome.reason_code.value if outcome.reason_code else None
                ),
                "evidence": None,
            }
        )
        return "result-1"

    return store


def _execute(repo, tenders, matcher, store):
    return execute_run(
        RUN_ID,
        session=FakeSession(),
        repo=repo,
        tender_repo=tenders,
        matcher=matcher,
        store=store,
    )


@pytest.fixture(autouse=True)
def _fake_manufacturer_id(monkeypatch):
    """The run resolves names to ids through the corpus; the fake corpus has
    no id column, so the mapping is supplied here."""
    monkeypatch.setattr(
        "app.services.output.runs.manufacturer_id", lambda session, name: f"mfr-{name}"
    )


def test_a_technical_requirement_is_matched_and_a_commercial_one_is_not():
    """The clause type decides whether there is anything to retrieve: a
    commercial clause is recorded as unsupported and kept out of the score
    (FR-14)."""
    repo = FakeRunRepo()
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)
    calls: list[dict] = []

    outcome = _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    # The LLM was asked about exactly one of the two requirements.
    assert [call["text"] for call in calls] == ["Body shall be SS316"]
    assert calls[0]["category"] == "valves"
    # The item's title and the read attribute travel with the clause: they are
    # what a bare clause is retrieved with (D-96).
    assert calls[0]["item_title"] == "GATE VALVE SIZE 150 MM"
    assert "requirement_attribute" in calls[0]
    assert outcome.results == 2
    assert outcome.status == RunStatus.DONE.value

    by_requirement = {row["requirement_id"]: row for row in repo.stored}
    assert by_requirement["r1"]["status"] == "full_match"
    assert by_requirement["r2"]["status"] == "unsupported_requirement"
    assert by_requirement["r2"]["verification_outcome"] == "not_checked"
    # A commercial clause is known to be out of scope; it is not a review item.
    assert by_requirement["r2"]["review_required"] is False

    assert [status for status, _ in repo.history] == ["retrieving", "scoring", "done"]


def test_the_stored_operator_reaches_the_comparison():
    """A stored "minimum 1500 RPM" stays a `gte` comparison.

    Dropping the operator turned every numeric requirement into a text one,
    which is a different answer, not a cheaper one (D17).
    """
    repo = FakeRunRepo()
    demand = dict(REQUIREMENTS[0], value="1500", unit="rpm", operator="gte")
    tenders = FakeTenderRepo([demand], ITEMS)
    calls: list[dict] = []

    _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert calls[0]["requirement_value"] == "1500"
    assert calls[0]["requirement_unit"] == "rpm"
    assert calls[0]["requirement_operator"] == "gte"


def test_an_item_with_no_category_is_searched_unfiltered_and_flagged():
    """A wrong filter would hide evidence and read as a missing capability."""
    repo = FakeRunRepo()
    tenders = FakeTenderRepo(
        REQUIREMENTS[:1],
        [{"item_id": "1", "name": "PISTON RING", "category": "unknown"}],
    )
    calls: list[dict] = []

    _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert calls[0]["category"] is None
    row = repo.stored[0]
    assert row["review_required"] is True
    assert "retrieved without a category filter" in (row["review_reason"] or "")


def test_a_requirement_with_no_item_is_searched_unfiltered_and_flagged():
    """An unresolved item assignment is flagged, not given the tender's guess.

    Nothing links the requirement to an item, so nothing can say which
    category it is compared in. Searching unfiltered is the safe choice; not
    saying so would present a weaker answer as a normal one (FR-T9).
    """
    repo = FakeRunRepo()
    tenders = FakeTenderRepo([dict(REQUIREMENTS[0], item_id="9")], ITEMS)
    calls: list[dict] = []

    _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert calls[0]["category"] is None
    assert repo.stored[0]["review_required"] is True
    assert "was not found" in (repo.stored[0]["review_reason"] or "")


def test_a_technical_clause_that_parsed_no_attribute_names_its_own_reason():
    """A clause that states a material and no canonical attribute has nothing
    to compare. Its row must not read as *the catalogue is silent*, which is a
    finding about the manufacturer, when the clause is what we never read
    (D-147)."""
    repo = FakeRunRepo()
    silent = _match(
        status=RequirementStatus.NO_EVIDENCE_FOUND,
        score=0.0,
        verification_outcome=VerificationOutcome.NOT_CHECKED,
        capability_text=None,
        confidence=ConfidenceLevel.LOW,
    )
    tenders = FakeTenderRepo([dict(REQUIREMENTS[0], attribute=None)], ITEMS)

    _execute(
        repo,
        tenders,
        _recording_matcher([], result=silent),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    row = repo.stored[0]
    assert row["status"] == "no_evidence_found"
    assert row["reason_code"] == "requirement_not_parsed"
    assert row["review_required"] is True
    assert "requirement_not_parsed" in (row["review_reason"] or "")


def test_a_clause_that_did_parse_an_attribute_keeps_the_code_it_earned():
    """The new code names an unparsed clause, so it must not overwrite the more
    specific reason a comparison reached: a clause with an attribute excluded
    by the category rule still says so (D-147)."""
    repo = FakeRunRepo()
    excluded = _match(
        status=RequirementStatus.NO_EVIDENCE_FOUND,
        score=0.0,
        verification_outcome=VerificationOutcome.NOT_CHECKED,
        capability_text=None,
        confidence=ConfidenceLevel.LOW,
        reason_code=ReasonCode.CATEGORY_MISMATCH,
    )
    tenders = FakeTenderRepo([dict(REQUIREMENTS[0], attribute="body_material")], ITEMS)

    _execute(
        repo,
        tenders,
        _recording_matcher([], result=excluded),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert repo.stored[0]["reason_code"] == "category_mismatch"


def test_an_unknown_clause_is_not_assessed_and_writes_no_row():
    """A clause nothing could type has no attribute to compare. Writing a
    "not checked" row for it would read as a result (PRD 4, D-92)."""
    repo = FakeRunRepo()
    tenders = FakeTenderRepo([dict(REQUIREMENTS[1], id="r3", type="unknown")], ITEMS)
    calls: list[dict] = []

    outcome = _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert calls == []
    assert repo.stored == []
    assert outcome.results == 0
    assert outcome.expected == 0
    assert outcome.status == RunStatus.DONE.value


def test_an_unknown_clause_does_not_count_towards_the_expected_results():
    """The run covers the typed clauses; the unknown one is reported as not
    assessed, not as a result nobody wrote (D-92)."""
    repo = FakeRunRepo()
    tenders = FakeTenderRepo(
        [REQUIREMENTS[0], dict(REQUIREMENTS[1], id="r3", type="unknown")], ITEMS
    )
    calls: list[dict] = []

    outcome = _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert [row["requirement_id"] for row in repo.stored] == ["r1"]
    assert outcome.requirements == 2
    assert outcome.expected == 1
    assert outcome.results == 1


def test_a_run_that_raises_is_recorded_as_failed_with_its_reason():
    """A run that stops says so: it is never left queued for a caller to poll."""
    repo = FakeRunRepo()
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)
    session = FakeSession()

    outcome = execute_run(
        RUN_ID,
        session=session,
        repo=repo,
        tender_repo=tenders,
        matcher=_recording_matcher([], error=RuntimeError("the model refused")),
        store=_recording_store(repo, {}),
    )

    assert outcome.status == RunStatus.FAILED.value
    assert "RuntimeError" in (outcome.error or "")
    assert "the model refused" in (outcome.error or "")
    assert repo.run["status"] == "failed"
    assert repo.run["error"] == outcome.error
    assert session.rollbacks == 1
    assert repo.history[-1][0] == "failed"


def test_a_run_that_hits_the_rate_limit_is_recorded_as_rate_limited():
    """Not `failed`: nothing is wrong with the run, and the answers it has are
    kept. The caller can see why it stopped and that it can be resumed."""
    from app.services.llm import LLMRateLimited

    repo = FakeRunRepo()
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)
    session = FakeSession()

    outcome = execute_run(
        RUN_ID,
        session=session,
        repo=repo,
        tender_repo=tenders,
        matcher=_recording_matcher(
            [], error=LLMRateLimited("the model is rate limited", retry_after=24.9)
        ),
        store=_recording_store(repo, {}),
    )

    assert outcome.status == RunStatus.RATE_LIMITED.value
    assert repo.run["status"] == "rate_limited"
    assert repo.run["error"] == outcome.error
    assert "rate limited" in (outcome.error or "")
    assert repo.history[-1][0] == "rate_limited"
    assert session.rollbacks == 1


def test_a_run_stopped_by_its_own_budget_is_a_stop_not_a_failure():
    """The provider refused nothing: this process spent what it was given.
    The answers found stay, and the run says what stopped it (D-110)."""
    from app.services.llm import LLMBudgetExceeded

    repo = FakeRunRepo()
    technical = [
        dict(REQUIREMENTS[0]),
        dict(REQUIREMENTS[0], id="r3", text="Seal shall be PTFE"),
    ]
    asked: list[str] = []

    def matcher(session, text, **kwargs):
        if asked:
            raise LLMBudgetExceeded("the call budget of 1 is spent (1 calls, 40 tokens)")
        asked.append(text)
        return _match()

    outcome = execute_run(
        RUN_ID,
        session=FakeSession(),
        repo=repo,
        tender_repo=FakeTenderRepo(technical, ITEMS),
        matcher=matcher,
        store=_recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert outcome.status == RunStatus.RATE_LIMITED.value
    assert "call budget" in (outcome.error or "")
    assert len(repo.stored) == 1


def test_a_resumed_run_does_not_pay_twice_for_one_requirement():
    """A run stopped by a limit kept its answers, and resuming it asks only
    about the pairs that are missing (D-110)."""
    repo = FakeRunRepo()
    repo.stored = [
        {
            "requirement_id": "r1",
            "manufacturer_id": "mfr-Acme",
            "manufacturer": "Acme",
            "status": "full_match",
            "verification_outcome": "passed",
            "applicability": "model_level",
            "score": 1.0,
            "capability_text": "Body material SS316",
            "confidence": "high",
            "review_required": False,
            "review_reason": None,
            "evidence": None,
        }
    ]
    calls: list[dict] = []
    technical = [
        dict(REQUIREMENTS[0]),
        dict(REQUIREMENTS[0], id="r3", text="Seal shall be PTFE"),
    ]

    outcome = _execute(
        repo,
        FakeTenderRepo(technical, ITEMS),
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert [call["text"] for call in calls] == ["Seal shall be PTFE"]
    assert outcome.results == 2


def test_a_rate_limited_run_resumes_where_it_stopped():
    """A run stopped by a limit keeps its answers. Running it again asks only
    about the pairs it never answered, so the quota already spent is not spent
    twice (D-110, D-130)."""
    repo = FakeRunRepo(status="rate_limited")
    repo.stored = [
        {
            "requirement_id": "r1",
            "manufacturer_id": "mfr-Acme",
            "manufacturer": "Acme",
            "status": "full_match",
            "verification_outcome": "passed",
            "applicability": "model_level",
            "score": 1.0,
            "capability_text": "Body material SS316",
            "confidence": "high",
            "review_required": False,
            "review_reason": None,
            "evidence": None,
        }
    ]
    calls: list[dict] = []
    technical = [
        dict(REQUIREMENTS[0]),
        dict(REQUIREMENTS[0], id="r3", text="Seal shall be PTFE"),
    ]

    outcome = _execute(
        repo,
        FakeTenderRepo(technical, ITEMS),
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    # The stored pair is not asked about again; the missing one is.
    assert [call["text"] for call in calls] == ["Seal shall be PTFE"]
    assert outcome.results == 2
    assert outcome.status == RunStatus.DONE.value


def test_the_run_reports_what_its_client_spent():
    """Calls, tokens and retries are reported, not left in the log (D4)."""

    class Counting:
        calls = 3
        prompt_tokens = 1200
        completion_tokens = 300
        retries = 1

    repo = FakeRunRepo()
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)

    outcome = execute_run(
        RUN_ID,
        session=FakeSession(),
        repo=repo,
        tender_repo=tenders,
        matcher=_recording_matcher([]),
        store=_recording_store(repo, {}),
        client=Counting(),
    )

    assert outcome.llm_calls == 3
    assert outcome.prompt_tokens == 1200
    assert outcome.completion_tokens == 300
    assert outcome.llm_retries == 1
    assert outcome.as_dict()["llm_calls"] == 3
    assert outcome.as_dict()["prompt_tokens"] == 1200


def test_the_run_stores_what_its_stages_cost():
    """Where the wall clock went is stored with the run, not only logged (D-131)."""
    repo = FakeRunRepo()
    matcher = _recording_matcher(
        [],
        result=_match(stage_seconds={"retrieve": 0.5, "extract": 2.0, "compare": 0.1}),
    )

    outcome = _execute(
        repo,
        FakeTenderRepo([dict(REQUIREMENTS[0])], ITEMS),
        matcher,
        _recording_store(repo, {}),
    )

    stored = repo.run["metrics"]["stage_seconds"]
    # One requirement against one manufacturer: the pair's stages are the run's.
    assert stored["retrieve"] == pytest.approx(0.5)
    assert stored["extract"] == pytest.approx(2.0)
    assert stored["compare"] == pytest.approx(0.1)
    # The stages of the loop itself are measured there, not in the matcher.
    assert stored["store"] >= 0.0
    assert stored["summarise"] >= 0.0
    # `total` is the run's own wall clock, measured in the loop.
    assert stored["total"] > 0.0
    # The figures a caller polls are the ones that were written.
    assert outcome.as_dict()["stage_seconds"] == stored
    assert repo.run["metrics"]["llm_calls"] == 0


def test_a_resumed_run_adds_its_cost_to_the_attempt_before_it():
    """One run row holds the sum of its attempts, not the last pass (D-130)."""

    class OneMore:
        calls = 1
        prompt_tokens = 100
        completion_tokens = 20
        retries = 0

    repo = FakeRunRepo(status="rate_limited")
    repo.run["metrics"] = {
        "stage_seconds": {"retrieve": 1.0, "extract": 4.0, "total": 6.0},
        "llm_calls": 2,
        "prompt_tokens": 500,
        "completion_tokens": 100,
        "llm_retries": 1,
    }
    # The pair the first attempt answered is stored, so only the other is asked.
    repo.stored = [_stored("r1", "mfr-Acme", "full_match", 1.0)]
    matcher = _recording_matcher(
        [],
        result=_match(stage_seconds={"retrieve": 0.5, "extract": 2.0}),
    )

    outcome = execute_run(
        RUN_ID,
        session=FakeSession(),
        repo=repo,
        tender_repo=FakeTenderRepo(
            [dict(REQUIREMENTS[0]), dict(REQUIREMENTS[0], id="r3", text="Seal shall be PTFE")],
            ITEMS,
        ),
        matcher=matcher,
        store=_recording_store(repo, {}),
        client=OneMore(),
    )

    assert outcome.results == 2

    stored = repo.run["metrics"]["stage_seconds"]
    assert stored["extract"] == pytest.approx(6.0)  # 4.0 from the first attempt
    assert stored["retrieve"] == pytest.approx(1.5)
    # The clock starts where the last attempt stopped, so a resumed run does
    # not report six seconds as one.
    assert stored["total"] >= 6.0
    assert outcome.llm_calls == 3
    assert repo.run["metrics"]["prompt_tokens"] == 600


def test_a_running_run_records_its_costs_as_each_pair_lands():
    """A caller polling a run that is still working is shown where it has got
    to, not an empty column it has to wait for (D-134)."""
    repo = FakeRunRepo()
    writes: list[int] = []

    def match(session, text, **kwargs):
        writes.append(len(repo.metrics_writes))
        return _match()

    requirements = [
        {
            "id": "r1",
            "item_id": "1",
            "text": "Body shall be SS316",
            "type": "technical",
            "mandatory": True,
            "value": None,
            "unit": None,
        },
        {
            "id": "r2",
            "item_id": "1",
            "text": "Rated pressure 150 bar",
            "type": "technical",
            "mandatory": True,
            "value": "150",
            "unit": "bar",
        },
    ]

    _execute(repo, FakeTenderRepo(requirements, ITEMS), match, _recording_store(repo, {}))

    # The first pair was asked with nothing stored yet; the second was asked
    # after the first pair's costs had been written.
    assert writes == [0, 1]
    last = repo.metrics_writes[-1]
    assert last["stage_seconds"]["store"] >= 0.0
    assert last["stage_seconds"]["total"] > 0.0


def test_polling_a_finished_run_reports_what_it_cost(monkeypatch):
    """The attempt's figures stay readable after it is gone (D-131)."""
    repo = FakeRunRepo(status="done")
    repo.stored = [_stored("r1", "mfr-Acme", "full_match", 1.0)]
    repo.run["metrics"] = {
        "stage_seconds": {"extract": 3.5},
        "llm_calls": 4,
        "prompt_tokens": 900,
        "completion_tokens": 200,
        "llm_retries": 2,
    }
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo(REQUIREMENTS)
    )

    state = progress(object(), RUN_ID, repo=repo)

    assert state.llm_calls == 4
    assert state.llm_retries == 2
    assert state.as_dict()["stage_seconds"] == {"extract": 3.5}


def test_a_finished_run_is_not_run_again():
    """Its results are the answer; running it again would double them."""
    repo = FakeRunRepo(status="done")
    repo.stored = [{"requirement_id": "r1"}]
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)
    calls: list[dict] = []

    outcome = _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {}),
    )

    assert calls == []
    assert len(repo.stored) == 1
    assert outcome.results == 1
    assert outcome.status == RunStatus.DONE.value


def test_a_run_another_pass_holds_is_not_worked_twice():
    """Two callers of POST /api/runs can be handed the same resumable run
    (D-130), and the route schedules the work for both. The second pass must
    not read the pairs the first is still answering."""
    repo = FakeRunRepo(status="retrieving")
    repo.stored = [{"requirement_id": "r1"}]
    tenders = FakeTenderRepo(REQUIREMENTS, ITEMS)
    calls: list[dict] = []

    outcome = _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {}),
    )

    assert calls == []
    assert repo.history == []
    assert outcome.results == 1
    # What the caller is told is the stored state, not this attempt's guess.
    assert outcome.status == RunStatus.RETRIEVING.value


def test_a_run_that_does_not_exist_is_a_not_found():
    with pytest.raises(NotFoundError):
        execute_run(
            "99999999-9999-4999-8999-999999999999",
            session=FakeSession(),
            repo=FakeRunRepo(),
            tender_repo=FakeTenderRepo(REQUIREMENTS),
            matcher=_recording_matcher([]),
            store=_recording_store(FakeRunRepo(), {}),
        )


# --- creating a run -------------------------------------------------------


def test_a_run_for_a_tender_that_does_not_exist_is_a_not_found(monkeypatch):
    class MissingTender(FakeTenderRepo):
        def get_tender(self, tender_id):
            return None

    monkeypatch.setattr("app.services.output.runs.TenderRepo", lambda session: MissingTender([]))

    with pytest.raises(NotFoundError):
        create_run(FakeSession(), TENDER_ID)


def test_a_run_against_a_tender_still_being_read_is_a_conflict(monkeypatch):
    """The requirement list is partial while the tender is read (D-82).

    Matching it would answer "no evidence" for requirements nobody has read
    yet, which is the absence-of-evidence error PRD 4 forbids.
    """

    class Reading(FakeTenderRepo):
        def get_tender(self, tender_id):
            return {"id": TENDER_ID, "status": "processing"}

    monkeypatch.setattr("app.services.output.runs.TenderRepo", lambda session: Reading([]))

    with pytest.raises(ConflictError) as raised:
        create_run(FakeSession(), TENDER_ID)

    assert raised.value.detail == {"status": "processing"}


def test_a_run_against_a_tender_that_could_not_be_read_is_a_conflict(monkeypatch):
    class Unreadable(FakeTenderRepo):
        def get_tender(self, tender_id):
            return {"id": TENDER_ID, "status": "failed"}

    monkeypatch.setattr("app.services.output.runs.TenderRepo", lambda session: Unreadable([]))

    with pytest.raises(ConflictError):
        create_run(FakeSession(), TENDER_ID)


def test_a_run_against_an_incomplete_tender_is_a_conflict(monkeypatch):
    """A tender with no page, no item or no requirement was not read in full.
    A run over it would report unread requirements as missing capabilities
    (PRD 4, D-91)."""

    class Incomplete(FakeTenderRepo):
        def get_tender(self, tender_id):
            return {"id": TENDER_ID, "status": "incomplete"}

    monkeypatch.setattr("app.services.output.runs.TenderRepo", lambda session: Incomplete([]))

    with pytest.raises(ConflictError):
        create_run(FakeSession(), TENDER_ID)


def test_a_run_against_a_rate_limited_tender_is_allowed(monkeypatch):
    """The read stopped at the provider's limit and kept what it read. A run
    over it is labelled incomplete, not refused (D8, D-75)."""

    class Limited(FakeTenderRepo):
        def get_tender(self, tender_id):
            return {"id": TENDER_ID, "status": "rate_limited"}

    monkeypatch.setattr("app.services.output.runs.TenderRepo", lambda session: Limited([]))
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers", lambda session: [{"name": "Acme"}]
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")

    assert create_run(FakeSession(), TENDER_ID, repo=FakeRunRepo()) == RUN_ID


def test_a_run_against_a_manufacturer_the_corpus_lacks_is_a_validation_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}, {"name": "Beta"}],
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )

    with pytest.raises(ValidationError) as raised:
        create_run(FakeSession(), TENDER_ID, ["Acme", "Nobody"])

    # The message says what the corpus does have, so the fix is obvious.
    assert "Nobody" in raised.value.message
    assert raised.value.detail["known"] == ["Acme", "Beta"]


def test_a_run_without_manufacturers_covers_the_whole_corpus(monkeypatch):
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Beta"}, {"name": "Acme"}],
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo()

    create_run(FakeSession(), TENDER_ID, repo=repo)

    assert repo.created["manufacturers"] == ["Acme", "Beta"]
    assert repo.created["corpus_hash"] == "hash"
    # The snapshot names what the run will be scored with (FR-34).
    assert {
        "scoring_policy_version",
        "prompt_version",
        "llm_model",
        "embedding_model",
        "embedding_dimension",
        "embedding_backend",
        "match_max_candidates",
    } <= set(repo.created["config_snapshot"])


def test_the_same_command_after_a_limit_continues_that_run(monkeypatch):
    """`make e2e` again, or a second POST, must not pay for the pairs the
    stopped run already answered (D-130)."""
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}],
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo(status="rate_limited")
    repo.resume_id = RUN_ID

    run_id = create_run(FakeSession(), TENDER_ID, repo=repo)

    assert run_id == RUN_ID
    assert repo.created == {}


def test_a_run_is_continued_only_when_it_would_ask_the_same_questions():
    """A run read by another model, or with a different candidate cap, is not
    continued: one report would hold answers from two configurations and state
    a single one (D-130). The spend limit is not such a difference."""
    from app.repositories.runs import _answers_alike

    base = {
        "llm_model": "openai/gpt-oss-20b",
        "match_max_candidates": 3,
        "llm_call_budget": 300,
        "manufacturers": ["Acme"],
    }

    assert _answers_alike(base, {**base, "llm_call_budget": 2000})
    assert not _answers_alike(base, {**base, "llm_model": "openai/gpt-oss-120b"})
    assert not _answers_alike(base, {**base, "match_max_candidates": 20})


def test_the_snapshot_an_attempt_is_compared_with_is_the_one_a_run_stores(monkeypatch):
    """Both sides of the comparison must be the same shape.

    The stored snapshot carries `manufacturers`; building the current one
    without them made every attempt look different, so a stopped run was never
    continued and each segment of the paid pass opened a new run instead
    (D-130).
    """
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}],
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo()

    create_run(FakeSession(), TENDER_ID, repo=repo)

    assert repo.compared == repo.created["config_snapshot"]
    assert repo.compared["manufacturers"] == ["Acme"]


def test_a_new_run_is_created_when_nothing_can_be_continued(monkeypatch):
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}],
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo()

    assert create_run(FakeSession(), TENDER_ID, repo=repo) == RUN_ID
    assert repo.created["manufacturers"] == ["Acme"]


def _no_model(monkeypatch) -> None:
    """The state of a machine with no model configured: a fresh clone."""
    from app.core.config import get_settings

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "model-from-env")
    get_settings.cache_clear()


def test_a_run_that_must_read_a_datasheet_is_refused_without_a_model(monkeypatch):
    """No model means no answer, and that is a fact about the request.

    The run used to be created and scheduled anyway, and then never move off
    `queued`, so a caller polled a run that was never going to start and every
    report route answered 409 with no reason (D-108).
    """
    _no_model(monkeypatch)
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers", lambda session: [{"name": "Acme"}]
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo(REQUIREMENTS)
    )

    with pytest.raises(ValidationError) as raised:
        create_run(FakeSession(), TENDER_ID)

    # The message names the setting to change, not just the failure.
    assert "LLM_API_KEY" in raised.value.message


def test_a_tender_of_only_commercial_lines_is_started_without_a_model(monkeypatch):
    """Nothing in it is read from a datasheet, so a missing key is no reason
    to refuse the report (FR-14, D-108)."""
    _no_model(monkeypatch)
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers", lambda session: [{"name": "Acme"}]
    )
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo",
        lambda session: FakeTenderRepo([dict(REQUIREMENTS[1])]),
    )
    monkeypatch.setattr("app.services.output.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo()

    assert create_run(FakeSession(), TENDER_ID, repo=repo) == RUN_ID


def test_a_run_stopped_by_a_missing_model_records_why(monkeypatch):
    """The state is the report: a scheduled run that cannot ask lands `failed`
    with the reason, rather than staying `queued` for a caller to wait on."""
    _no_model(monkeypatch)
    repo = FakeRunRepo()

    outcome = execute_run(
        RUN_ID,
        session=FakeSession(),
        repo=repo,
        tender_repo=FakeTenderRepo(REQUIREMENTS, ITEMS),
        store=_recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert outcome.status == RunStatus.FAILED.value
    assert "LLM_API_KEY" in outcome.error
    assert repo.run["status"] == RunStatus.FAILED.value


def test_manufacturers_are_deduplicated_in_the_order_asked_for(monkeypatch):
    monkeypatch.setattr(
        "app.services.output.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}, {"name": "Beta"}],
    )
    session = object()

    assert _manufacturer_names(session, ["Beta", "Acme", "Beta"]) == ["Beta", "Acme"]


def test_an_empty_corpus_cannot_be_run_against(monkeypatch):
    monkeypatch.setattr("app.services.output.runs.list_manufacturers", lambda session: [])

    with pytest.raises(ValidationError) as raised:
        _manufacturer_names(object(), None)

    assert "no manufacturers" in raised.value.message


# --- the summary ----------------------------------------------------------


def _stored(requirement_id, manufacturer_id, status, score, **overrides):
    values = {
        "requirement_id": requirement_id,
        "manufacturer_id": manufacturer_id,
        "manufacturer": manufacturer_id,
        "status": status,
        "verification_outcome": "passed",
        "applicability": "model_level",
        "score": score,
        "capability_text": None,
        "confidence": "high",
        "review_required": False,
        "review_reason": None,
        "evidence": None,
    }
    values.update(overrides)
    return values


def test_the_summary_ranks_on_the_stored_rows():
    from app.core.config import get_settings

    requirements = [
        {"id": "r1", "mandatory": True},
        {"id": "r2", "mandatory": False},
    ]
    results = [
        _stored("r1", "mfr-a", "full_match", 1.0),
        _stored("r2", "mfr-a", "full_match", 1.0),
        _stored("r1", "mfr-b", "partial_match", 0.5),
        _stored("r2", "mfr-b", "missing_capability", 0.0),
    ]

    summary = summarise(results, requirements, get_settings().scoring)

    assert [row["manufacturer_id"] for row in summary] == ["mfr-a", "mfr-b"]
    assert summary[0]["rank"] == 1
    assert summary[0]["compatibility_percent"] == 100.0
    assert summary[1]["compatibility_percent"] == pytest.approx(33.33, abs=0.01)
    # The optional miss does not fail the mandatory rule.
    assert summary[1]["fails_mandatory"] is False


def test_a_mandatory_missing_capability_fails_the_manufacturer():
    from app.core.config import get_settings

    summary = summarise(
        [_stored("r1", "mfr-a", "missing_capability", 0.0)],
        [{"id": "r1", "mandatory": True}],
        get_settings().scoring,
    )

    assert summary[0]["fails_mandatory"] is True


def test_no_evidence_counts_towards_coverage_and_not_against_the_score():
    """The corpus being silent is neither a pass nor a fail (PRD 4)."""
    from app.core.config import get_settings

    summary = summarise(
        [
            _stored("r1", "mfr-a", "full_match", 1.0),
            _stored("r2", "mfr-a", "no_evidence_found", 0.0),
        ],
        [{"id": "r1", "mandatory": True}, {"id": "r2", "mandatory": True}],
        get_settings().scoring,
    )

    assert summary[0]["compatibility_percent"] == 100.0
    assert summary[0]["coverage_percent"] == 50.0


def test_progress_counts_what_is_stored(monkeypatch):
    repo = FakeRunRepo(manufacturers=("Acme", "Beta"))
    repo.stored = [_stored("r1", "mfr-Acme", "full_match", 1.0)]
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo(REQUIREMENTS)
    )

    state = progress(object(), RUN_ID, repo=repo)

    assert state.requirements == 2
    assert state.expected == 4
    assert state.results == 1
    assert state.as_dict()["progress_percent"] == 25.0


def test_progress_expects_only_the_assessable_requirements(monkeypatch):
    """An unknown clause is not assessed, so it is not an assessment a caller
    waits for (D-92). It is still counted in the tender's requirements."""
    requirements = [*REQUIREMENTS, {"id": "r3", "type": "unknown", "text": "5.2"}]
    repo = FakeRunRepo(manufacturers=("Acme", "Beta"))
    monkeypatch.setattr(
        "app.services.output.runs.TenderRepo", lambda session: FakeTenderRepo(requirements)
    )

    state = progress(object(), RUN_ID, repo=repo)

    assert state.requirements == 3
    assert state.expected == 4


# --- the report query against the real schema -----------------------------


class _Rows:
    rowcount = 1

    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _RecordingSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return _Rows(self.rows)

    def commit(self) -> None:
        pass


def _evidence_row(path, url):
    """One `results` row, in the order the query selects."""
    return (
        "result-1",
        "req-1",
        "mfr-1",
        "Acme",
        "full_match",
        "passed",
        "model_level",
        1.0,
        "Body material SS316",
        "high",
        False,
        None,
        None,
        "fact-1",
        12,
        "Body: ASTM A351 CF8M",
        "doc-1",
        path,
        "catalogue",
        "b" * 64,
        url,
    )


def test_claiming_a_run_filters_on_the_states_an_attempt_starts_from():
    """The row's state is the lock (D-132). An update that named no state would
    let every caller take the run, which is the double spend the claim exists
    to stop."""
    from app.repositories.runs import RunRepo

    session = _RecordingSession([])
    claimed = RunRepo(session).claim(RUN_ID)

    assert claimed is True
    where = str(session.statement.whereclause)
    assert "run.id = " in where
    assert "run.status IN" in where
    assert session.statement.compile().params["status"] == RunStatus.RETRIEVING.value


def test_the_report_query_reads_only_columns_the_schema_has():
    """A document row has no `filename` column (D20).

    The fake repositories in the report tests hid this: the query was built
    from a column that does not exist, so every real report raised
    `AttributeError` before it could read anything.
    """
    from app.repositories.runs import RunRepo

    session = _RecordingSession([_evidence_row("manufacturers/acme/raw/d-series.pdf", None)])
    rows = RunRepo(session).results("run-1")

    assert rows[0]["evidence"]["document"] == "d-series.pdf"
    # The name comes from the stored path, and the query reads that column.
    from sqlalchemy.dialects import postgresql

    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "document.path" in sql


def test_a_document_with_no_path_is_named_by_its_address():
    from app.repositories.runs import RunRepo, _document_name

    assert _document_name(None, "https://manufacturer.test/files/d-series.pdf") == (
        "d-series.pdf"
    )
    assert _document_name(None, None) is None
    assert _document_name(r"C:\corpus\acme\datasheet.pdf", None) == "datasheet.pdf"
    # And the same through the row the report reads.
    session = _RecordingSession(
        [_evidence_row(None, "https://manufacturer.test/files/d-series.pdf")]
    )
    assert RunRepo(session).results("run-1")[0]["evidence"]["document"] == "d-series.pdf"
