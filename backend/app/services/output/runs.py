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
  not be inferred stops retrieval and flags review until its scope is resolved.

The state is written before the work each time, so a run interrupted
mid-way is visibly interrupted rather than silently queued.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.corpus import corpus_hash, list_manufacturers, manufacturer_id
from app.repositories.runs import RunRepo, tender_inputs, inputs_are_current
from app.repositories.tender import TenderRepo
from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    ReasonCode,
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
    from app.services.tender.source_policy import context_only
    return [
        requirement
        for requirement in requirements
        if not context_only(requirement) and _requirement_type(requirement.get("type")) is not RequirementType.UNKNOWN
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
        TenderStatus.RATE_LIMITED.value,
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
    # Seconds the run spent in each stage, including the stages of the
    # attempts that a resumed run did not repeat (D-131).
    stage_seconds: dict = field(default_factory=dict)

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
            "stage_seconds": self.stage_seconds,
            "error": self.error,
        }


# The run's spend, and the client attribute each figure is read from.
_SPEND = (
    ("llm_calls", "calls"),
    ("prompt_tokens", "prompt_tokens"),
    ("completion_tokens", "completion_tokens"),
    ("llm_retries", "retries"),
)


def config_snapshot() -> dict:
    """What a report must record about the run that produced it (FR-34).

    Every one of these can change the answer, so a report that does not name
    them cannot be reproduced.
    """
    from app.services.requirement.extract import PROMPT_VERSION
    from app.services.match.score import SCORING_POLICY_VERSION
    from app.services.match.facts import contract_version
    from app.services.match.prose_binding import POLICY_VERSION
    from app.services.match.page_context import READER_VERSION
    from app.services.requirement.structured_values import fingerprint
    from app.services.tender.classify import classification_policy_fingerprint
    import pymupdf

    settings = get_settings()
    return {
        "app_version": settings.app_version,
        "schema_version": settings.schema_version,
        "prompt_version": PROMPT_VERSION,
        "fact_prompt_version": contract_version(),
        "model_binding_policy": POLICY_VERSION,
        "page_context_reader": READER_VERSION,
        "structured_value_policy": fingerprint(),
        "clause_classification_policy": classification_policy_fingerprint(),
        "pdf_engine_version": pymupdf.VersionBind,
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
        "match_extraction_batch_size": settings.match_extraction_batch_size,
        "match_extraction_batch_chars": settings.match_extraction_batch_chars,
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


def _require_current_extraction(requirements):
    from app.services.requirement.extract import PROMPT_VERSION
    if any('prompt_version' in row and row['prompt_version'] != PROMPT_VERSION for row in requirements):
        raise ConflictError('tender extraction must be refreshed before matching', {})


def _source_issues(tender_id, repository):
    from app.services.tender.integrity import tender_source_issues
    return tender_source_issues(tender_id, repository.list_attachments(tender_id))


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

    if _source_issues(tender_id, tender_repo):
        raise ConflictError('tender source integrity must be restored before matching', {})

    # A clause only a model can read needs a model, and asking that here is
    # what keeps a run from being created, scheduled, and then never moving off
    # `queued` because there was nothing to call (D-108). A tender that is
    # nothing but commercial lines is answered from its clause types alone, so
    # it is not refused for want of a key.
    requirements = tender_repo.list_requirements(tender_id)
    _require_current_extraction(requirements)
    if needs_model(requirements):
        reason = config_error()
        if reason:
            raise ValidationError(reason)

    names = _manufacturer_names(session, manufacturers)
    run_repo = repo or RunRepo(session)
    # The snapshot this attempt would store, exactly as `create` stores it:
    # comparing it against one that lacks `manufacturers` would find the two
    # different on every tender and so never continue anything (D-130).
    from app.services.output.report import capture_report_context
    bound_inputs = tender_inputs(requirements, tender_repo.list_items(tender_id), tender_id=tender_id)
    bound_inputs["report_context"] = capture_report_context(tender_repo, tender_id, requirements)
    snapshot = {**config_snapshot(), "manufacturers": names,
                "tender_inputs": bound_inputs}
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

    from app.services.match.match import match_requirement, merge_stages, stage
    from app.services.match.match import store_match

    run_repo = repo or RunRepo(session)
    tenders = tender_repo or TenderRepo(session)
    match = matcher or match_requirement
    # Reuse each expanded query vector across manufacturers within this run.
    if hasattr(session, "info"):
        session.info["requirement_embeddings"] = {}
        session.info["verification_tables"] = {}
        session.info['verification_native_lines'] = {}
        session.info['verification_sources'] = {}
        session.info['verification_page_contexts'] = {}
        session.info['verification_context_cache'] = {}
        session.info['part_catalogue_evidence'] = {}
        session.info['source_table_parts'] = {}
    write = store or store_match

    run = run_repo.get(run_id)
    if run is None:
        raise NotFoundError(f"no run with id {run_id}")

    current_requirements = tenders.list_requirements(run["tender_id"])
    bound = run["config_snapshot"].get("tender_inputs")
    requirements = (bound["requirements"] if bound is not None else
                    tenders.list_requirements(run["tender_id"], requirement_ids=[
                        row["requirement_id"] for row in run_repo.results(run_id)]))
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
        _apply_costs(progress, run.get("metrics") or {})
        return progress

    # What the attempts before this pass cost, if any: a resumed run is the
    # same row, so its report states the whole run and not only the last pass.
    prior = run.get("metrics") or {}
    spent = prior.get("stage_seconds") or {}
    timings = {key: value for key, value in spent.items() if key != "total"}
    # The clock starts where the last attempt stopped, so `total` is the run's
    # wall clock rather than this pass's (D-130 keeps one row for both).
    started = time.monotonic() - float(spent.get("total", 0.0) or 0.0)

    try:
        if not inputs_are_current(run["config_snapshot"], current_requirements):
            raise ConflictError("run inputs are obsolete or unbound; create a new run for the active requirement revision", {})
        if _source_issues(run["tender_id"], tenders):
            raise ConflictError('tender source integrity must be restored before matching', {})
        _require_current_extraction(requirements)
        if matcher is None and client is None and needs_model(requirements):
            # One client for the run: its counters are what the run spent, and a
            # client per requirement would lose them (D4). Built inside the
            # guarded block because an unconfigured model raises here: a run
            # that cannot ask has to land `failed` with that reason, not stay
            # `queued` for a caller to poll and never hear back.
            client = LLMClient(purpose='matching')

        # One pass works a run at a time. The caller that arrives second is
        # told what the run already holds instead of starting a second pass
        # over the pairs the first is still answering (D-132).
        if not run_repo.claim(run_id):
            held = run_repo.get(run_id) or run
            progress.status = held["status"]
            _apply_costs(progress, held.get("metrics") or {})
            return progress

        items = {item["item_id"]: item for item in bound["items"]}
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
                merge_stages(timings, outcome.stage_seconds)
                with stage(timings, "store"):
                    written = write(
                        session,
                        outcome,
                        run_id=run_id,
                        requirement_id=requirement["id"],
                        manufacturer_id=identifier,
                    )
                progress.results += 1
                # The costs travel with the pair, so a caller polling a run that
                # is still working sees where it has got to instead of an empty
                # column (D-134). One more JSONB write, in the same commit.
                _record_costs(progress, run_repo, run_id, client, prior, timings, started)
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
        with stage(timings, "summarise"):
            summary = summarise(
                run_repo.results(run_id), requirements, get_settings().scoring
            )
        logger.info(
            "run %s done: %d results, %d manufacturers",
            run_id,
            progress.results,
            len(summary),
        )
        run_repo.set_status(run_id, RunStatus.DONE)
        progress.status = RunStatus.DONE.value
        _record_costs(progress, run_repo, run_id, client, prior, timings, started)
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
        progress.status = RunStatus.RATE_LIMITED.value
        progress.error = message
        _record_costs(progress, run_repo, run_id, client, prior, timings, started)
        session.commit()
        return progress
    except Exception as exc:  # noqa: BLE001 - the state is the report
        # A run that stops says so, with the reason. It is never left
        # `queued` or `retrieving` for a caller to poll forever.
        session.rollback()
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("run %s failed: %s", run_id, exc)
        run_repo.set_status(run_id, RunStatus.FAILED, error=message)
        progress.status = RunStatus.FAILED.value
        progress.error = message
        _record_costs(progress, run_repo, run_id, client, prior, timings, started)
        session.commit()
        return progress


def _record_costs(progress: RunProgress, run_repo, run_id: str, client, prior, timings, started):
    """Write what the run cost. The caller commits: it is one state change."""
    _apply_costs(progress, prior, client)
    progress.stage_seconds = {**timings, "total": round(time.monotonic() - started, 3)}
    run_repo.set_metrics(run_id, _costs_of(progress))


def _apply_costs(progress: RunProgress, metrics: dict, client=None) -> None:
    """What the run cost, added to the attempts that came before it.

    A resumed run keeps its row (D-130), so its figures are the sum of its
    attempts: naming only the last pass would understate a run the provider
    stopped twice. With no client the stored figures are the whole answer.
    """
    progress.stage_seconds = metrics.get("stage_seconds") or {}
    for name, attribute in _SPEND:
        live = int(getattr(client, attribute, 0) or 0) if client is not None else 0
        setattr(progress, name, int(metrics.get(name, 0) or 0) + live)


def _costs_of(progress: RunProgress) -> dict:
    """The run row's `metrics`: the same figures the caller is given."""
    return {
        "stage_seconds": progress.stage_seconds,
        **{name: getattr(progress, name) for name, _ in _SPEND},
    }


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
        outcome = _unsupported(requirement_type, text)
        if requirement_type is RequirementType.UNKNOWN and requirement.get('review_reason'):
            outcome.review_reason = '; '.join((outcome.review_reason, requirement['review_reason']))
        return outcome, category

    if not text:
        return _unsupported(RequirementType.UNKNOWN, ""), category

    from app.repositories.part_coverage import source_part, indexed_part_manufacturers
    from app.services.match.match import MatchResult
    normalized = requirement.get("normalization")
    effective = requirement
    if normalized is not None:
        if normalized.get("review_required") or not normalized.get("attribute"):
            reason = normalized.get("review_reason") or "unresolved structured attribute mapping"
            return MatchResult(requirement_text=text, status=RequirementStatus.NO_EVIDENCE_FOUND,
                               verification_outcome=VerificationOutcome.NOT_CHECKED,
                               applicability=Applicability.UNKNOWN, review_required=True,
                               review_reason=reason, reason=reason,
                               reason_code=ReasonCode.REQUIREMENT_NOT_PARSED), category
        effective = {**requirement, **{key: normalized[key] for key in ("attribute", "value", "unit") if key in normalized}}
    part = source_part(session, requirement)
    if part and manufacturer_name not in indexed_part_manufacturers(session, part):
        reason = 'No indexed evidence for the required source part in this manufacturer catalogue; comparison was not run.'
        return MatchResult(requirement_text=text, status=RequirementStatus.NO_EVIDENCE_FOUND,
                           verification_outcome=VerificationOutcome.NOT_CHECKED,
                           applicability=Applicability.UNKNOWN, review_required=True,
                           review_reason=reason, reason=reason, reason_code=ReasonCode.CORPUS_GAP), category
    if category is None:
        reason = ('The BOQ item was not found; its scope is unresolved.' if not item else
                  'The BOQ item category is unresolved; classify it before manufacturer retrieval.')
        return MatchResult(requirement_text=text, status=RequirementStatus.NO_EVIDENCE_FOUND,
                           verification_outcome=VerificationOutcome.NOT_CHECKED,
                           applicability=Applicability.UNKNOWN, review_required=True,
                           review_reason=reason, reason=reason, reason_code=ReasonCode.SCOPE_UNRESOLVED), category

    outcome = matcher(
        session,
        text,
        # What the clause is about, and what the normalizer read from it: the
        # retrieval query is built from all three, so a bare fragment is not
        # searched for on its own (D-96).
        item_title=item.get("name"),
        requirement_attribute=effective.get("attribute"),
        requirement_value=effective.get("value"),
        requirement_unit=effective.get("unit"),
        # The stored operator is what makes "minimum 1500 RPM" a `gte`
        # comparison instead of a text one (D17). Dropping it silently
        # downgraded every numeric requirement.
        requirement_operator=requirement.get("operator"),
        manufacturer_name=manufacturer_name,
        category=category,
        client=client,
    )

    if (
        outcome.status is RequirementStatus.NO_EVIDENCE_FOUND
        and not (requirement.get("attribute") or "").strip()
    ):
        # The clause was typed technical but nothing in it was ever read as an
        # attribute - "Cast carbon steel" states a material and no canonical
        # term. The row would otherwise read as *the catalogue is silent*, which
        # is a finding about the manufacturer, when the truth is that we had
        # nothing to compare (D-147). The status stays *no evidence found*: the
        # product was not checked. The code names whose limit stopped it, and
        # the pair goes to review like every other unconfirmed claim.
        outcome.reason_code = ReasonCode.REQUIREMENT_NOT_PARSED
        outcome.review_required = True
        outcome.review_reason = "; ".join(
            part
            for part in (
                outcome.review_reason,
                "requirement_not_parsed: the clause named no attribute to compare",
            )
            if part
        )
        outcome.reason = outcome.review_reason
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

    bound = run["config_snapshot"].get("tender_inputs")
    requirements = (bound["requirements"] if bound is not None else
                    TenderRepo(session).list_requirements(run["tender_id"], requirement_ids=[
                        row["requirement_id"] for row in run_repo.results(run_id)]))
    state = RunProgress(
        run_id=run_id,
        status=run["status"],
        tender_id=run["tender_id"],
        manufacturers=run["manufacturers"],
        requirements=len(requirements),
        expected=len(assessable(requirements)) * len(run["manufacturers"]),
        results=run_repo.count_results(run_id),
        error=run["error"],
    )
    # The spend and the stage times are what the run stored, so a caller that
    # polls after it finishes is told the same figures the attempt reported
    # (D-131). While it works they are not written yet, and read zero.
    _apply_costs(state, run.get("metrics") or {})
    return state
