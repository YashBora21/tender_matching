"""A matching run: creation, execution, failure and the per-manufacturer summary.

No database and no LLM: the repositories, the matcher and the store are fakes,
so what is tested is the orchestration - what is run, in what order, and what
is recorded when something goes wrong.
"""

from __future__ import annotations

import pytest

from app.core.errors import NotFoundError, ValidationError
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    RequirementStatus,
    RunStatus,
    VerificationOutcome,
)
from app.services.matching import MatchResult
from app.services.runs import (
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
            "manufacturers": list(manufacturers),
        }
        self.stored: list[dict] = []
        self.history: list[tuple[str, str | None]] = []
        self.created: dict = {}

    def create(self, tender_id, *, manufacturers, config_snapshot, corpus_hash=""):
        self.created = {
            "tender_id": tender_id,
            "manufacturers": manufacturers,
            "config_snapshot": config_snapshot,
            "corpus_hash": corpus_hash,
        }
        self.run["manufacturers"] = list(manufacturers)
        return RUN_ID

    def get(self, run_id):
        return dict(self.run) if run_id == RUN_ID else None

    def set_status(self, run_id, status, *, error=None):
        self.history.append((status.value, error))
        self.run["status"] = status.value
        if error is not None:
            self.run["error"] = error

    def results(self, run_id):
        return list(self.stored)

    def count_results(self, run_id):
        return len(self.stored)


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
        "app.services.runs.manufacturer_id", lambda session, name: f"mfr-{name}"
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
    assert outcome.results == 2
    assert outcome.status == RunStatus.DONE.value

    by_requirement = {row["requirement_id"]: row for row in repo.stored}
    assert by_requirement["r1"]["status"] == "full_match"
    assert by_requirement["r2"]["status"] == "unsupported_requirement"
    assert by_requirement["r2"]["verification_outcome"] == "not_checked"
    # A commercial clause is known to be out of scope; it is not a review item.
    assert by_requirement["r2"]["review_required"] is False

    assert [status for status, _ in repo.history] == ["retrieving", "scoring", "done"]


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


def test_an_unknown_clause_type_is_unsupported_and_flagged():
    repo = FakeRunRepo()
    tenders = FakeTenderRepo(
        [dict(REQUIREMENTS[1], id="r3", type="unknown")], ITEMS
    )
    calls: list[dict] = []

    _execute(
        repo,
        tenders,
        _recording_matcher(calls),
        _recording_store(repo, {"mfr-Acme": "Acme"}),
    )

    assert calls == []
    assert repo.stored[0]["status"] == "unsupported_requirement"
    # Nothing established that an unknown clause is technical, so a person
    # has to look at it.
    assert repo.stored[0]["review_required"] is True


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

    monkeypatch.setattr("app.services.runs.TenderRepo", lambda session: MissingTender([]))

    with pytest.raises(NotFoundError):
        create_run(FakeSession(), TENDER_ID)


def test_a_run_against_a_manufacturer_the_corpus_lacks_is_a_validation_error(monkeypatch):
    monkeypatch.setattr(
        "app.services.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}, {"name": "Beta"}],
    )
    monkeypatch.setattr(
        "app.services.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )

    with pytest.raises(ValidationError) as raised:
        create_run(FakeSession(), TENDER_ID, ["Acme", "Nobody"])

    # The message says what the corpus does have, so the fix is obvious.
    assert "Nobody" in raised.value.message
    assert raised.value.detail["known"] == ["Acme", "Beta"]


def test_a_run_without_manufacturers_covers_the_whole_corpus(monkeypatch):
    monkeypatch.setattr(
        "app.services.runs.list_manufacturers",
        lambda session: [{"name": "Beta"}, {"name": "Acme"}],
    )
    monkeypatch.setattr(
        "app.services.runs.TenderRepo", lambda session: FakeTenderRepo([])
    )
    monkeypatch.setattr("app.services.runs.corpus_hash", lambda session: "hash")
    repo = FakeRunRepo()

    create_run(FakeSession(), TENDER_ID, repo=repo)

    assert repo.created["manufacturers"] == ["Acme", "Beta"]
    assert repo.created["corpus_hash"] == "hash"
    # The snapshot names what the run will be scored with (FR-34).
    assert {"scoring_policy_version", "prompt_version", "llm_model"} <= set(
        repo.created["config_snapshot"]
    )


def test_manufacturers_are_deduplicated_in_the_order_asked_for(monkeypatch):
    monkeypatch.setattr(
        "app.services.runs.list_manufacturers",
        lambda session: [{"name": "Acme"}, {"name": "Beta"}],
    )
    session = object()

    assert _manufacturer_names(session, ["Beta", "Acme", "Beta"]) == ["Beta", "Acme"]


def test_an_empty_corpus_cannot_be_run_against(monkeypatch):
    monkeypatch.setattr("app.services.runs.list_manufacturers", lambda session: [])

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
        "app.services.runs.TenderRepo", lambda session: FakeTenderRepo(REQUIREMENTS)
    )

    state = progress(object(), RUN_ID, repo=repo)

    assert state.requirements == 2
    assert state.expected == 4
    assert state.results == 1
    assert state.as_dict()["progress_percent"] == 25.0
