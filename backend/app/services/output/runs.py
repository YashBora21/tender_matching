"""A matching run: one tender against a set of manufacturers (FR-31, FR-32).

A run is the unit the API starts, polls and reports on. It walks every
requirement of a tender and answers it for every manufacturer asked for, one
requirement at a time, storing each result as it is found:

    queued -> retrieving -> scoring -> done
                            \\-> failed

Two things it does **before** retrieval:

- **The clause type decides whether there is anything to retrieve.** A
  commercial, eligibility or informational clause is *unsupported*: it is not
  a capability that can be documented, so it is recorded with its source and
  kept out of the score (FR-14). `unknown` is unsupported too, and flagged
  for review, because nothing has established that it is technical.
- **The item decides the category** (FR-16). An item whose category could
  not be inferred is not filtered by a category at all, and the requirement
  is flagged: a wrong filter would hide evidence and read as a missing
  capability.

The state is written before the work each time, so a run interrupted
mid-way is visibly interrupted rather than silently queued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.corpus import corpus_hash, list_manufacturers, manufacturer_id
from app.repositories.runs import RunRepo
from app.repositories.tender import TenderRepo
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    RequirementStatus,
    RequirementType,
    RunStatus,
    TenderStatus,
    VerificationOutcome,
)
from app.services.llm import LLMBudgetExceeded, LLMClient, LLMRateLimited
from app.services.llm import config_error
from app.services.match.score import (
    ScoredResult,
    score_manufacturer,
    rank,
)

logger = get_logger(__name__)

# The clause types a manufacturer can document. Everything else is
# unsupported and is excluded from the score (FR-14).
MATCHABLE = frozenset({RequirementType.TECHNICAL, RequirementType.CERTIFICATION})


def assessable(requirements: list[dict]) -> list[dict]:
    """The requirements a run assesses: every clause that was given a type.

    An `unknown` clause is one nothing could type - a fragment, a legal line,
    or a clause the model could not read. There is no attribute to retrieve
    against, so writing a result for it would turn "we could not read this"
    into "we checked it and found nothing", which is the absence-of-evidence
    trap (PRD 4, D-92). It is reported as *not assessed* instead.
    """
    return [
        requirement
        for requirement in requirements
        if _requirement_type(requirement.get("type")) is not RequirementType.UNKNOWN
    ]


def needs_model(requirements: list[dict]) -> bool:
    """Whether a run over these requirements has to ask the model anything.

    Only a technical or certification clause is searched and read, so a tender
    that is nothing but commercial and legal lines is answered from its clause
    types alone. Refusing to start that run because no model is configured
    would withhold a report that needed no model to be honest.
    """
    return any(
        _requirement_type(requirement.get("type")) in MATCHABLE
        for requirement in assessable(requirements)
    )


# A tender in one of these states has no complete requirement list: it is still
# being read, or the read stopped without finishing. A run against it would
# report unread requirements as a manufacturer having no evidence (D-82).
_UNREAD = frozenset(
    {
        TenderStatus.QUEUED.value,
        TenderStatus.PROCESSING.value,
        TenderStatus.FAILED.value,
        TenderStatus.INCOMPLETE.value,
    }
)


@dataclass
class RunProgress:
    """What a caller polls for: the state, and how far it has got."""

    run_id: str
    status: str
    tender_id: str
    manufacturers: list[str]
    requirements: int = 0
    expected: int = 0
    results: int = 0
    error: Optional[str] = None
    # What the run's own LLM client spent (FR-34). Zero when the matcher was
    # injected and could not report, which is a fake in a test, not production.
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "tender_id": self.tender_id,
            "status": self.status,
            "manufacturers": self.manufacturers,
            "requirements": self.requirements,
            "expected_results": self.expected,
            "results": self.results,
            "progress_percent": (
                round(self.results / self.expected * 100, 1) if self.expected else 0.0
            ),
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "llm_retries": self.llm_retries,
            "error": self.error,
        }


def config_snapshot() -> dict:
    """What a report must record about the run that produced it (FR-34).

    Every one of these can change the answer, so a report that does not name
    them cannot be reproduced.
    """
    from app.services.requirement.extract import PROMPT_VERSION
    from app.services.match.score import SCORING_POLICY_VERSION

    settings = get_settings()
    return {
        "app_version": settings.app_version,
        "schema_version": settings.schema_version,
        "prompt_version": PROMPT_VERSION,
        "scoring_policy_version": SCORING_POLICY_VERSION,
        "llm_model": settings.llm_model,
        # Which OpenAI-compatible endpoint answered: the same model name served
        # elsewhere is a different answer, and a report has to say which one it
        # is quoting (D-109).
        "llm_base_url": settings.llm_base_url,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        # The backend decides whether the query vector is normalised, which
        # decides the cosine order, which decides which passages are read
        # (D18).
        "embedding_backend": settings.embedding_backend,
        # How many passages a requirement may be read against: a smaller cap
        # finds less evidence, so the report has to name it (D19).
        "match_max_candidates": settings.match_max_candidates,
        # What the run was allowed to spend. A run that stopped at its own
        # budget is reproducible only if the budget is on the record (D-110).
        "llm_call_budget": settings.llm_call_budget,
        "llm_token_budget": settings.llm_token_budget,
        "scoring": settings.scoring.model_dump(),
        "comparison": settings.comparison.model_dump(),
    }


def _manufacturer_names(session: Session, wanted: Optional[list[str]]) -> list[str]:
    """The manufacturers to run against: the ones asked for, or the whole corpus.

    A name that is not in the corpus is a mistake in the request, not an
    empty result: a run against a manufacturer that does not exist would
    report every requirement as no evidence found, which reads as a
    manufacturer that fails everything.
    """
    available = {row["name"]: row for row in list_manufacturers(session)}
    if not available:
        raise ValidationError("the corpus holds no manufacturers")

    if not wanted:
        return sorted(available)

    chosen: list[str] = []
    unknown: list[str] = []
    for name in wanted:
        cleaned = (name or "").strip()
        if cleaned in available:
            if cleaned not in chosen:
                chosen.append(cleaned)
        else:
            unknown.append(cleaned)
    if unknown:
        raise ValidationError(
            "the corpus has no manufacturer named " + ", ".join(sorted(unknown)),
            {"known": sorted(available)},
        )
    return chosen


def create_run(
    session: Session,
    tender_id: str,
    manufacturers: Optional[list[str]] = None,
    *,
    repo: Optional[RunRepo] = None,
) -> str:
    """Start a run: write its row and return its id.

    No matching happens here. The caller schedules `execute_run`, so the HTTP
    request that starts a run is not the request that waits for it.
    """
    tender_repo = TenderRepo(session)
    tender = tender_repo.get_tender(tender_id)
    if tender is None:
        raise NotFoundError(f"no tender with id {tender_id}")
    # A run compares the tender's requirements, and while the read is running
    # the list is only what has been read so far (D-82). Matching a partial
    # list would answer "no evidence" for requirements nobody has read yet,
    # which is the absence-of-evidence error PRD 4 forbids. A read that failed
    # has no complete list either.
    if tender.get("status") in _UNREAD:
        raise ConflictError(
            f"tender {tender_id} has not been read yet (status {tender.get('status')})",
            {"status": tender.get("status")},
        )

    # A clause only a model can read needs a model, and asking that here is
    # what keeps a run from being created, scheduled, and then never moving off
    # `queued` because there was nothing to call (D-108). A tender that is
    # nothing but commercial lines is answered from its clause types alone, so
    # it is not refused for want of a key.
    if needs_model(tender_repo.list_requirements(tender_id)):
        reason = config_error()
        if reason:
            raise ValidationError(reason)

    names = _manufacturer_names(session, manufacturers)
    run_repo = repo or RunRepo(session)
    snapshot = config_snapshot()
    # A run that a limit stopped is continued, not duplicated: it already paid
    # for the pairs it stored, and the command the caller runs again is the
    # same command - as long as it would ask the same questions (D-130).
    resumable = run_repo.resumable(tender_id, names, snapshot)
    if resumable is not None:
        return resumable
    return run_repo.create(
        tender_id,
        manufacturers=names,
        config_snapshot=snapshot,
        corpus_hash=corpus_hash(session),
    )


def execute_run(
    run_id: str,
    *,
    session: Optional[Session] = None,
    repo: Optional[RunRepo] = None,
    tender_repo: Optional[TenderRepo] = None,
    matcher=None,
    store=None,
    client=None,
) -> RunProgress:
    """Answer every requirement of the run's tender for every manufacturer.

    The dependencies are injectable so the flow can be driven without a
    database or an LLM in a test; production passes nothing and gets the real
    ones.
    """
    if session is None:
        from app.repositories.db import session_scope

        with session_scope() as own:
            return execute_run(
                run_id,
                session=own,
                repo=repo,
                tender_repo=tender_repo,
                matcher=matcher,
                store=store,
                client=client,
            )

    from app.services.match.match import match_requirement, store_match

    run_repo = repo or RunRepo(session)
    tenders = tender_repo or TenderRepo(session)
    match = matcher or match_requirement
    write = store or store_match

    run = run_repo.get(run_id)
    if run is None:
        raise NotFoundError(f"no run with id {run_id}")

    requirements = tenders.list_requirements(run["tender_id"])
    # Only the clauses that were typed are assessed. An unknown clause has no
    # attribute to compare, and a row for it would read as a result (D-92).
    to_assess = assessable(requirements)
    names = run["manufacturers"]
    progress = RunProgress(
        run_id=run_id,
        status=run["status"],
        tender_id=run["tender_id"],
        manufacturers=names,
        requirements=len(requirements),
        expected=len(to_assess) * len(names),
        results=run_repo.count_results(run_id),
        error=run["error"],
    )

    # A finished run is not run again: the results it holds are the answer,
    # and re-running would double them. A run stopped by the provider's limit
    # is not finished: its stored answers are skipped and only the missing
    # pairs are asked about, so it continues instead of starting over (D-130).
    if run["status"] in (
        RunStatus.DONE.value,
        RunStatus.FAILED.value,
    ):
        return progress

    try:
        if matcher is None and client is None and needs_model(requirements):
            # One client for the run: its counters are what the run spent, and a
            # client per requirement would lose them (D4). Built inside the
            # guarded block because an unconfigured model raises here: a run
            # that cannot ask has to land `failed` with that reason, not stay
            # `queued` for a caller to poll and never hear back.
            client = LLMClient()

        run_repo.set_status(run_id, RunStatus.RETRIEVING)
        session.commit()

        items = {item["item_id"]: item for item in tenders.list_items(run["tender_id"])}
        # The pairs this pass must not ask about again. A run that stopped at a
        # limit kept its answers, and a resumed run pays only for the pairs
        # that are missing (D-110).
        answered = run_repo.answered(run_id)
        for name in names:
            identifier = manufacturer_id(session, name)
            if identifier is None:  # pragma: no cover - create_run checked it
                raise ValidationError(f"the corpus has no manufacturer named {name}")
            for requirement in to_assess:
                pair = (str(requirement["id"]), str(identifier))
                if pair in answered:
                    continue
                outcome, category = _assess(
                    session,
                    requirement,
                    items,
                    manufacturer_name=name,
                    matcher=match,
                    client=client,
                )
                written = write(
                    session,
                    outcome,
                    run_id=run_id,
                    requirement_id=requirement["id"],
                    manufacturer_id=identifier,
                )
                progress.results += 1
                # The result is committed as it is found: a run that dies
                # halfway leaves the answers it already has, and the count
                # a caller polls is the count that exists.
                session.commit()
                logger.debug(
                    "run %s: %s %s -> %s (%s)",
                    run_id,
                    name,
                    requirement["id"],
                    outcome.status.value,
                    category,
                )

        run_repo.set_status(run_id, RunStatus.SCORING)
        session.commit()
        summary = summarise(run_repo.results(run_id), requirements, get_settings().scoring)
        logger.info(
            "run %s done: %d results, %d manufacturers",
            run_id,
            progress.results,
            len(summary),
        )
        run_repo.set_status(run_id, RunStatus.DONE)
        progress.status = RunStatus.DONE.value
        _record_usage(progress, client)
        session.commit()
        return progress
    except (LLMRateLimited, LLMBudgetExceeded) as exc:
        # The run stopped before every requirement was answered, whether the
        # provider refused the call or this process spent the budget it was
        # given. The results already found are committed and kept; the run
        # records why it stopped, and is distinct from a failed run. It is not
        # `done`: a half-read tender must not read as a finished answer, and a
        # resumed run starts from the pairs still missing (D-110).
        session.rollback()
        message = str(exc)
        logger.warning("run %s stopped: %s", run_id, message)
        run_repo.set_status(run_id, RunStatus.RATE_LIMITED, error=message)
        session.commit()
        progress.status = RunStatus.RATE_LIMITED.value
        progress.error = message
        _record_usage(progress, client)
        return progress
    except Exception as exc:  # noqa: BLE001 - the state is the report
        # A run that stops says so, with the reason. It is never left
        # `queued` or `retrieving` for a caller to poll forever.
        session.rollback()
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("run %s failed: %s", run_id, exc)
        run_repo.set_status(run_id, RunStatus.FAILED, error=message)
        session.commit()
        progress.status = RunStatus.FAILED.value
        progress.error = message
        _record_usage(progress, client)
        return progress


def _record_usage(progress: RunProgress, client) -> None:
    """What the run's client cost, when it can say."""
    if client is None:
        return
    progress.llm_calls = int(getattr(client, "calls", 0) or 0)
    progress.prompt_tokens = int(getattr(client, "prompt_tokens", 0) or 0)
    progress.completion_tokens = int(getattr(client, "completion_tokens", 0) or 0)
    progress.llm_retries = int(getattr(client, "retries", 0) or 0)


def _assess(
    session: Session,
    requirement: dict,
    items: dict,
    *,
    manufacturer_name: str,
    matcher,
    client,
):
    """One requirement against one manufacturer, with the flags it earns.

    Returns the outcome and the category it was answered in, for the log.
    """
    text = (requirement.get("text") or requirement.get("source_text") or "").strip()
    item = items.get(requirement.get("item_id")) or {}
    category = item.get("category") or None
    if category == "unknown":
        category = None

    requirement_type = _requirement_type(requirement.get("type"))
    if requirement_type not in MATCHABLE:
        return _unsupported(requirement_type, text), category

    if not text:
        return _unsupported(RequirementType.UNKNOWN, ""), category

    outcome = matcher(
        session,
        text,
        # What the clause is about, and what the normalizer read from it: the
        # retrieval query is built from all three, so a bare fragment is not
        # searched for on its own (D-96).
        item_title=item.get("name"),
        requirement_attribute=requirement.get("attribute"),
        requirement_value=requirement.get("value"),
        requirement_unit=requirement.get("unit"),
        # The stored operator is what makes "minimum 1500 RPM" a `gte`
        # comparison instead of a text one (D17). Dropping it silently
        # downgraded every numeric requirement.
        requirement_operator=requirement.get("operator"),
        manufacturer_name=manufacturer_name,
        category=category,
        client=client,
    )

    # The item's category is what scopes the search. When it is unknown the
    # search ran unfiltered, and that is a fact the reviewer needs: the answer
    # is weaker than it looks. The reason is inferred from the item's own
    # words again, so the flag says why and not only that.
    if category is None:
        from app.services.match.category import infer_category

        if item:
            reason = infer_category(item.get("name") or "").reason
            reason = reason or "the item's category was not recognised"
        else:
            # The requirement hangs from no item, so nothing scoped the
            # search. That is an unresolved item assignment, not a category
            # the tender declared (FR-T9): it is flagged, never guessed.
            reason = "the requirement's bill-of-quantities item was not found"
        outcome.review_required = True
        outcome.review_reason = "; ".join(
            part
            for part in (outcome.review_reason, f"{reason}; retrieved without a category filter")
            if part
        )
    return outcome, category


def _requirement_type(value) -> RequirementType:
    try:
        return RequirementType(value)
    except (ValueError, TypeError):
        return RequirementType.UNKNOWN


def _unsupported(requirement_type: RequirementType, text: str):
    """A requirement no document can answer, recorded as such (FR-14)."""
    from app.services.match.match import MatchResult

    reason = (
        "the clause is "
        f"{requirement_type.value}: it is not a capability that a document can state"
    )
    return MatchResult(
        requirement_text=text,
        status=RequirementStatus.UNSUPPORTED_REQUIREMENT,
        verification_outcome=VerificationOutcome.NOT_CHECKED,
        applicability=Applicability.UNKNOWN,
        confidence=ConfidenceLevel.LOW,
        # An unknown clause has not been shown to be technical, so it is
        # flagged; a commercial clause is known to be out of scope and is not.
        review_required=requirement_type is RequirementType.UNKNOWN,
        review_reason=reason if requirement_type is RequirementType.UNKNOWN else None,
        reason=reason,
    )


def summarise(results: list[dict], requirements: list[dict], policy) -> list[dict]:
    """Compatibility, coverage and ranking for every manufacturer in a run.

    Built from the stored results, so the summary and the rows it summarises
    can never disagree. Weighting, the mandatory-failure flag and the
    family-level cap are the scoring module's, not this function's (PRD 8).
    """
    mandatory = {row["id"]: row.get("mandatory", True) for row in requirements}
    order = {
        row["id"]: index for index, row in enumerate(requirements)
    }

    by_manufacturer: dict[str, list[ScoredResult]] = {}
    for row in sorted(results, key=lambda r: order.get(r["requirement_id"], 0)):
        by_manufacturer.setdefault(row["manufacturer_id"], []).append(
            ScoredResult(
                requirement_id=row["requirement_id"],
                manufacturer_id=row["manufacturer_id"],
                status=RequirementStatus(row["status"]),
                score=row["score"] or 0.0,
                mandatory=mandatory.get(row["requirement_id"], True),
                verification_outcome=VerificationOutcome(row["verification_outcome"]),
                applicability=Applicability(row["applicability"]),
                review_required=row["review_required"],
                review_reason=row["review_reason"],
                capability_text=row["capability_text"],
                evidence_ref=(row["evidence"] or {}).get("fact_id"),
                confidence=row["confidence"],
            )
        )

    names = {row["manufacturer_id"]: row["manufacturer"] for row in results}
    summaries = [
        score_manufacturer(identifier, scored, policy)
        for identifier, scored in by_manufacturer.items()
    ]
    ranked = rank(summaries)
    return [
        {
            "manufacturer_id": summary.manufacturer_id,
            "manufacturer": names.get(summary.manufacturer_id, ""),
            "rank": position,
            "compatibility_percent": summary.compatibility_percent,
            "coverage_percent": summary.coverage_percent,
            "status_counts": summary.status_counts,
            "fails_mandatory": summary.fails_mandatory,
            "review_count": summary.review_count,
        }
        for position, summary in enumerate(ranked, start=1)
    ]


def progress(session: Session, run_id: str, *, repo: Optional[RunRepo] = None) -> RunProgress:
    """The state of a run, and how far it has got."""
    run_repo = repo or RunRepo(session)
    run = run_repo.get(run_id)
    if run is None:
        raise NotFoundError(f"no run with id {run_id}")

    requirements = TenderRepo(session).list_requirements(run["tender_id"])
    return RunProgress(
        run_id=run_id,
        status=run["status"],
        tender_id=run["tender_id"],
        manufacturers=run["manufacturers"],
        requirements=len(requirements),
        expected=len(assessable(requirements)) * len(run["manufacturers"]),
        results=run_repo.count_results(run_id),
        error=run["error"],
    )
