"""The tender pipeline: stages, checkpoints and invalidation.

A tender is processed in stages, and each stage is keyed by a hash of
everything that can change its output. A stage whose key has not changed is
not run again, and neither are the stages after it. So a change to the
source file re-reads the items and the requirements, a change to the prompt
re-extracts and re-resolves, and a change to the classifier re-reads the
attachments - and nothing else moves.

The chain is explicit:

    attachments -> items -> requirements -> extraction -> resolution

and each stage's key is the hash of its inputs *and* the key of the stage
before it, so a change upstream invalidates everything downstream and a
change to one stage invalidates only what comes after it.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.tender import LLMCache, StoredRequirement, TenderRepo
from app.schemas.enums import RequirementType, TenderReasonCode
from app.schemas.models import EXTRACTION_VERSION
from app.services.requirement.atomize import atomize_records
from app.services.requirement.extract import (
    ExtractedRequirement,
    extract_requirements,
)
from app.services.llm import LLMClient, LLMRateLimited, LLMBudgetExceeded
from app.services.tender.classify import NON_TECHNICAL_REASON, is_non_technical
from app.services.requirement.retype import retype_technical
from app.services.tender.ingest import (
    PageRead,
    RequirementRecord,
    SpecificationRead,
    TenderItem,
    assign_to_items,
    drop_fragments,
    english_half,
    is_mangled,
    read_boq_items,
    read_specification,
    requirements_from_items,
)

logger = get_logger(__name__)

STAGE_ATTACHMENTS = "attachments"
STAGE_ITEMS = "items"
STAGE_REQUIREMENTS = "requirements"
STAGE_EXTRACTION = "extraction"
STAGE_RESOLUTION = "resolution"

STAGES = (
    STAGE_ATTACHMENTS,
    STAGE_ITEMS,
    STAGE_REQUIREMENTS,
    STAGE_EXTRACTION,
    STAGE_RESOLUTION,
)

# A stage that could not finish. `rate_limited` is resumable: the next run
# tries it again, and the work already done is kept.
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_SKIPPED = "skipped"


def _reader_version() -> str:
    """The version of the reader, read where it is used.

    A change to the reader is a change to what the stage produces, so it
    is part of the stage's key.
    """
    from app.services.tender.ingest import READER_VERSION

    return READER_VERSION


def _classifier_version() -> str:
    """The version of the classifier, read where it is used."""
    from app.services.tender.collect import CLASSIFIER_VERSION

    return CLASSIFIER_VERSION


def _prompt_version() -> str:
    """The version of the extraction prompt.

    It is read from the extractor at the moment it is used, so the key of a
    stage always names the prompt that produced it.
    """
    from app.services.requirement.extract import PROMPT_VERSION

    return PROMPT_VERSION


def _atomizer_version() -> str:
    """The version of the atomizer, read where it is used.

    A change to the split changes what the requirements stage produces, so it
    is part of that stage's key and invalidates everything after it.
    """
    from app.services.requirement.atomize import ATOMIZER_VERSION

    return ATOMIZER_VERSION


def content_hash(*parts) -> str:
    """A stable hash of a stage's inputs.

    The parts are separated so that two different inputs cannot produce the
    same hash by accident.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()


@dataclass
class StageState:
    """What one stage did, and what it depends on."""

    name: str
    status: str = STATUS_SKIPPED
    # The hash of the inputs. A stage whose key is unchanged is not run.
    key: str = ""
    depends_on: str = ""
    detail: Optional[str] = None
    rows: int = 0
    # The LLM calls this stage made. A cached answer is not a call.
    llm_calls: int = 0


@dataclass
class PipelineResult:
    """What one tender's run produced."""

    tender_id: str
    stages: dict[str, StageState] = field(default_factory=dict)
    items: list[TenderItem] = field(default_factory=list)
    requirements: list[RequirementRecord] = field(default_factory=list)
    # What became of every page of the specification (FR-T2). Written to the
    # ledger, and counted by the audit: a tender with no page here was not
    # read, whatever its requirements say.
    pages: list[PageRead] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    # What the extraction cost, and whether it stopped on the provider's limit.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0
    rate_limited: bool = False
    rate_limited_detail: Optional[str] = None
    # The reason the tender could not be read, when it could not.
    failure: Optional[str] = None
    # The same fact as a stable key, so a screen can name the outcome without
    # reading the sentence (D-156).
    failure_code: Optional[TenderReasonCode] = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def stage(self, name: str) -> StageState:
        return self.stages.get(name, StageState(name=name))


def _attachment_by_kind(attachments: list[dict], *kinds: str) -> Optional[dict]:
    """The attachment that plays one of these roles."""
    for record in attachments:
        if record.get("kind") in kinds and record.get("status") not in (
            "failed",
            "restricted",
        ):
            return record
    return None


# The words a file's own name or address uses for the two parts of a tender.
# They are a tie-break among scans and nothing more: a hint never outranks a
# real `boq-pdf` or `spec`, and a scan that hints at nothing keeps its place
# in the list (D-156).
_BOQ_WORDS = ("boq", "bom", "bill", "price", "item")
_SPEC_WORDS = ("spec", "technical", "annexure", "requirement", "tcs")


def _scanned_hinted(
    attachments: list[dict], words: tuple[str, ...]
) -> Optional[dict]:
    """The scanned attachment whose own name hints at this role, if any.

    Two scans in one tender are the case this exists for: a terms-and-
    conditions page and a specification sheet are both `scanned`, and which is
    which is only visible in the file's name or the link it came from.
    """
    for record in attachments:
        if record.get("kind") != "scanned" or record.get("status") in (
            "failed",
            "restricted",
        ):
            continue
        haystack = f"{record.get('path') or ''} {record.get('url') or ''}".lower()
        if any(word in haystack for word in words):
            return record
    return None


@dataclass
class Roles:
    """Which attachment plays which part of a tender.

    The roles are resolved once, so the stage that reads the items and the
    stage that reads the requirements agree on what is what. A scanned
    document can be either, so it is tried as the bill of quantities first
    and as the specification after.
    """

    boq: Optional[dict] = None
    specification: Optional[dict] = None

    @property
    def has_both(self) -> bool:
        return self.boq is not None and self.specification is not None


def resolve_roles(attachments: list[dict]) -> Roles:
    """Decide which attachment is the bill of quantities and which is the
    specification.

    A tender links to several documents and only some of them are the
    requirements. The bill of quantities is a CSV or a table; a scanned
    document is tried as the bill of quantities, and then as the
    specification, because the same kind of file can be either.
    """
    roles = Roles()

    roles.boq = _attachment_by_kind(attachments, "boq-csv", "boq-pdf")
    if roles.boq is None:
        roles.boq = _scanned_hinted(attachments, _BOQ_WORDS)
    if roles.boq is None:
        roles.boq = _attachment_by_kind(attachments, "scanned")
    if roles.boq is None:
        roles.boq = _attachment_by_kind(attachments, "spec")

    # The specification is the table of item descriptions, which is often the
    # same file as the bill of quantities. A CSV cannot be read as a
    # specification - it has no pages - so a PDF is preferred.
    roles.specification = _attachment_by_kind(attachments, "boq-pdf")
    if roles.specification is None:
        roles.specification = _attachment_by_kind(attachments, "spec")
    if roles.specification is None:
        # Among scans the file's own name decides, because a tender can scan
        # two different things and the order they were listed in says nothing.
        roles.specification = _scanned_hinted(attachments, _SPEC_WORDS)
    if roles.specification is None and roles.boq is not None:
        if str(roles.boq.get("path") or "").lower().endswith(".pdf"):
            roles.specification = roles.boq
    return roles


def stage_keys(
    attachments: list[dict],
    *,
    prompt_version: str,
    schema_version: str,
    model: str,
    vocabulary_hash: str,
    fetch_config: str = "",
) -> dict[str, str]:
    """The key of every stage, from the inputs each one reads.

    Every key folds in the key of the stage before it, so a change upstream
    changes everything downstream and a change to one stage changes only
    what comes after it.
    """
    # The attachments: their content hashes and the roles they play.
    attachment_parts = sorted(
        (record.get("kind") or "", record.get("sha256") or "", record.get("status") or "")
        for record in attachments
    )
    attachments_key = content_hash(
        "attachments", fetch_config, attachment_parts, _classifier_version()
    )

    roles = resolve_roles(attachments)

    # The items: the bill of quantities, how it is read, and the version of
    # the reader. The version is part of the key because a change to the
    # reader changes what the stage produces.
    boq = roles.boq
    items_key = content_hash(
        "items",
        attachments_key,
        boq.get("sha256") if boq else "",
        _reader_version(),
        # The item's category is inferred as part of this stage, so the
        # vocabulary that infers it is one of its inputs (FR-16).
        _categories_hash(),
    )

    # The requirements: the specification, the items they hang from, the
    # version of the reader, and the version of the atomizer that splits a
    # compound clause (FR-13).
    spec = roles.specification
    requirements_key = content_hash(
        "requirements",
        items_key,
        spec.get("sha256") if spec else "",
        _reader_version(),
        _atomizer_version(),
    )

    # The extraction: the requirements, and what read them.
    extraction_key = content_hash(
        "extraction", requirements_key, model, prompt_version, schema_version
    )

    # The resolution: what was extracted, and the vocabulary that reads it.
    resolution_key = content_hash(
        "resolution", extraction_key, vocabulary_hash, RESOLVER_VERSION
    )

    return {
        STAGE_ATTACHMENTS: attachments_key,
        STAGE_ITEMS: items_key,
        STAGE_REQUIREMENTS: requirements_key,
        STAGE_EXTRACTION: extraction_key,
        STAGE_RESOLUTION: resolution_key,
    }


# The version of the resolution below. A change to the normalizer or the
# alias table changes what the stage produces, so it is part of its key.
RESOLVER_VERSION = "1"


def _vocabulary_hash() -> str:
    """A hash of the attribute vocabulary.

    The vocabulary decides how a requirement is read, so a change to it
    invalidates the resolution and nothing before it.
    """
    from app.comparison.vocabulary import vocabulary_path

    path = Path(vocabulary_path())
    if not path.exists():
        return ""
    return content_hash(path.read_bytes())


def _categories_hash() -> str:
    """A hash of the category vocabulary.

    It decides what category an item gets, so a change to it invalidates the
    item stage and everything after it.
    """
    from app.services.match.category import categories_path

    path = Path(categories_path())
    if not path.exists():
        return ""
    return content_hash(path.read_bytes())


def run_pipeline(
    tender_id: str,
    tender_dir: Path,
    attachments: list[dict],
    *,
    repo: Optional[TenderRepo] = None,
    cache: Optional[LLMCache] = None,
    client=None,
    force: bool = False,
) -> PipelineResult:
    """Run the stages of one tender, skipping the ones that have not changed.

    `repo` is where the stage checkpoints live; without it, every stage runs.
    """
    settings = get_settings()
    result = PipelineResult(tender_id=tender_id)
    result.attachments = attachments

    keys = stage_keys(
        attachments,
        prompt_version=_prompt_version(),
        schema_version=settings.schema_version,
        model=settings.llm_model or "",
        vocabulary_hash=_vocabulary_hash(),
        fetch_config=settings.fetch_allowed_domains,
    )
    previous = repo.stage_keys(tender_id) if repo is not None else {}
    previous_status = (
        repo.stage_statuses(tender_id)
        if repo is not None and hasattr(repo, "stage_statuses")
        else {}
    )

    def changed(stage: str) -> bool:
        if force or keys[stage] != previous.get(stage):
            return True
        # A stage that stopped short did not finish, even though it read the
        # same input: its key is unchanged and its state says why it stopped.
        # Skipping it would turn an incomplete read into a complete one
        # (AGENTS #3, D15).
        return previous_status.get(stage) in (STATUS_RATE_LIMITED, STATUS_FAILED)

    # --- the attachments ---
    state = StageState(
        name=STAGE_ATTACHMENTS,
        key=keys[STAGE_ATTACHMENTS],
        depends_on=previous.get(STAGE_ATTACHMENTS, ""),
        rows=len(attachments),
        status=STATUS_DONE if changed(STAGE_ATTACHMENTS) else STATUS_SKIPPED,
    )
    result.stages[STAGE_ATTACHMENTS] = state
    if changed(STAGE_ATTACHMENTS) and repo is not None:
        for attachment in attachments:
            repo.upsert_attachment(tender_id, attachment)

    # --- the items ---
    items_state = StageState(
        name=STAGE_ITEMS,
        key=keys[STAGE_ITEMS],
        depends_on=keys[STAGE_ATTACHMENTS],
    )
    roles = resolve_roles(attachments)
    boq = roles.boq
    if boq is None:
        items_state.status = STATUS_FAILED
        items_state.detail = _no_bill_reason(attachments)
        result.stages[STAGE_ITEMS] = items_state
        result.failure = items_state.detail
        if repo is not None:
            _save_failed(repo, result, settings)
        return result
    result.items = _read_items(boq, tender_id, tender_dir)
    # The item's own words decide its category, not the attachment's hint
    # (FR-16). It is part of the items stage: the items are what it reads.
    assign_categories(result.items)
    items_state.status = STATUS_DONE if changed(STAGE_ITEMS) else STATUS_SKIPPED
    items_state.rows = len(result.items)
    result.stages[STAGE_ITEMS] = items_state
    if changed(STAGE_ITEMS) and repo is not None:
        repo.replace_items(
            tender_id,
            [
                {
                    "item_id": item.item_id,
                    "name": item.title,
                    "category": item.category,
                    "boq_ref": item.source_row,
                }
                for item in result.items
            ],
        )

    # --- the requirements ---
    requirements_state = StageState(
        name=STAGE_REQUIREMENTS,
        key=keys[STAGE_REQUIREMENTS],
        depends_on=keys[STAGE_ITEMS],
    )
    spec = roles.specification
    if spec is None:
        requirements_state.status = STATUS_FAILED
        requirements_state.detail = "no specification was found"
        result.stages[STAGE_REQUIREMENTS] = requirements_state
        result.failure = requirements_state.detail
        result.failure_code = TenderReasonCode.NO_SPECIFICATION_FOUND
        if repo is not None:
            _save_failed(repo, result, settings)
        return result
    backfill = False
    if changed(STAGE_REQUIREMENTS):
        # Before a single model call: a specification nobody can read is not a
        # specification that states nothing. One page is read here, OCR
        # included, so a hopeless scan costs a second of local work instead of
        # a call per clause (D-156).
        specification_path = tender_dir / (spec.get("path") or "")
        if specification_path.exists() and _reads_nothing(specification_path):
            requirements_state.status = STATUS_FAILED
            requirements_state.detail = (
                f"the specification {specification_path.name} is a scan and the "
                "OCR read no text from it"
            )
            result.stages[STAGE_REQUIREMENTS] = requirements_state
            result.failure = requirements_state.detail
            result.failure_code = TenderReasonCode.SPECIFICATION_UNREADABLE
            if repo is not None:
                _save_failed(repo, result, settings)
            return result
        read = _read_specification(spec, tender_dir)
        if read.error:
            # The document could not be opened, so no page of it was looked
            # at. That is a document that could not be read, not a document
            # with no requirements (PRD 4).
            requirements_state.status = STATUS_FAILED
            requirements_state.detail = (
                f"the specification could not be read: {read.error}"
            )
            result.stages[STAGE_REQUIREMENTS] = requirements_state
            result.failure = requirements_state.detail
            if repo is not None:
                _save_failed(repo, result, settings)
            return result
        result.pages = read.pages
        result.requirements, unassigned = assign_to_items(read.rows, result.items)
        for row in unassigned:
            # A requirement that names no item is kept and flagged: an
            # ambiguous mapping is never guessed at (FR-T9).
            tag = row.get("page_tag") or ""
            classified = is_non_technical(tag) if tag else False
            result.requirements.append(
                RequirementRecord(
                    text=row.get("description", ""),
                    item_id="",
                    requirement_type=RequirementType.UNKNOWN,
                    page=row.get("page"),
                    source_section=row.get("title"),
                    extraction_method=row.get("extraction_method") or "table",
                    page_tag=tag or None,
                    review_required=True,
                    review_reason=(
                        f"{NON_TECHNICAL_REASON} {tag}"
                        if classified
                        else "the specification row names no item in the bill of quantities"
                    ),
                )
            )
        # A clause that is a fragment of a line is not a requirement: it is
        # either the rest of the clause before it, or noise (step 3).
        result.requirements, fragments = drop_fragments(result.requirements)
        if fragments.dropped or fragments.merged:
            # Kept in the stage's detail so the report can say how much of the
            # tender was noise instead of requirements.
            requirements_state.detail = json.dumps(
                {"dropped": fragments.dropped, "merged": fragments.merged}
            )
            logger.info(
                "tender %s: %d fragment(s) dropped, %d merged",
                tender_id,
                fragments.dropped,
                fragments.merged,
            )
        # A bill of quantities row that states an obligation is a clause in
        # its own right, and its item is the parent it sits under. Without
        # this the row is stored as an item and no requirement ever points at
        # it: the clause is never assessed, and the report cannot say that it
        # was skipped (D-141). A clause the specification already stated for
        # the same item is not stated twice.
        stated = {
            (requirement.item_id or "", requirement.text.strip().lower())
            for requirement in result.requirements
        }
        for requirement in requirements_from_items(result.items):
            key = (requirement.item_id, requirement.text.lower())
            if key in stated:
                continue
            stated.add(key)
            result.requirements.append(requirement)

        # A compound clause becomes one requirement per fact before the model
        # reads it: the atomizer is part of the requirements stage, so each
        # atom is extracted and compared on its own (FR-13, FR-T8, D-105).
        clauses = len(result.requirements)
        result.requirements = atomize_records(result.requirements)
        if len(result.requirements) != clauses:
            logger.info(
                "tender %s: %d clause(s) atomized into %d requirement(s)",
                tender_id,
                clauses,
                len(result.requirements),
            )
        requirements_state.status = STATUS_DONE
    else:
        # The stage already ran over this input. The stored rows are its
        # output, and they carry the model's types: reading the file again
        # would replace them with raw clauses and give every requirement a new
        # id, which is not a no-op (D-93).
        result.requirements = _stored_records(repo, tender_id)
        requirements_state.status = STATUS_SKIPPED
        # The ledger is a state of the *pages*, not of the requirements, so a
        # tender read before the ledger was written (D-91) has requirements and
        # no page states at all - and its stage key has not changed, so it
        # would never be written again. Re-reading the pages costs no model
        # call, and every page ends in a state (FR-T2).
        backfill = repo is not None and _ledger_is_empty(repo, tender_id)
        if backfill:
            result.pages = _read_specification(spec, tender_dir).pages
    requirements_state.rows = len(result.requirements)
    result.stages[STAGE_REQUIREMENTS] = requirements_state
    if result.pages and repo is not None and (
        requirements_state.status == STATUS_DONE or backfill
    ):
        # The ledger is written with the pages that were just read, so every
        # page the tender declares ends in a state (FR-T2). It is written
        # before the extraction runs: a read that stops at the provider's
        # limit still has a ledger saying what was read.
        repo.replace_pages(
            tender_id,
            _page_rows(
                result.pages,
                _attachment_id(repo, tender_id, spec),
            ),
        )
    # --- the extraction (the only stage that calls the LLM) ---
    extraction_state = StageState(
        name=STAGE_EXTRACTION,
        key=keys[STAGE_EXTRACTION],
        depends_on=keys[STAGE_REQUIREMENTS],
    )
    if changed(STAGE_EXTRACTION):
        if client is None:
            # One client for the whole stage: its counters are the stage's
            # cost, and a client per line would lose them.
            client = LLMClient()
        # A client can outlive a stage (a caller passes one, or a test reuses
        # one), so what the stage spent is the difference, not the total.
        before = (
            int(getattr(client, "calls", 0) or 0),
            int(getattr(client, "prompt_tokens", 0) or 0),
            int(getattr(client, "completion_tokens", 0) or 0),
            int(getattr(client, "retries", 0) or 0),
        )
        has_calls = getattr(client, "calls", None) is not None
        # What has been read so far is written down while the stage runs, so a
        # caller can watch the requirements appear instead of waiting for the
        # last one (D-82). The writes are throttled, and each is a complete
        # snapshot of the requirements read up to that moment: the table never
        # holds half a requirement.
        last_write = 0.0

        def report(read_so_far) -> None:
            nonlocal last_write
            if repo is None:
                return
            now = time.monotonic()
            if now - last_write < settings.stream_interval_seconds:
                return
            last_write = now
            repo.replace_requirements(
                tender_id,
                [
                    to_stored(
                        requirement,
                        tender_id,
                        model=settings.llm_model,
                        prompt_version=_prompt_version(),
                        schema_version=settings.schema_version,
                    )
                    for requirement in read_so_far
                ],
            )
            # Committed now: the reader of the live list is another session,
            # and a write it cannot see is a requirement it cannot show.
            repo.session.commit()

        extracted, calls, hits, limited, limit_detail = _extract_all(
            result.requirements, cache=cache, client=client, on_read=report
        )
        result.requirements = extracted
        # The model's `unknown` is a proposal, not a verdict: a clause that
        # states a value and a direction is technical whatever it was called
        # (FR-14, FR-15). The re-typing is deterministic and never overrides
        # a type the model did give.
        retyped = retype_technical(result.requirements)
        if retyped:
            logger.info(
                "tender %s: %d clause(s) re-typed as technical", tender_id, retyped
            )
        after = (
            int(getattr(client, "calls", 0) or 0),
            int(getattr(client, "prompt_tokens", 0) or 0),
            int(getattr(client, "completion_tokens", 0) or 0),
            int(getattr(client, "retries", 0) or 0),
        )
        # The client counts every attempt, including the one the provider
        # refused, so its counter is the honest cost when it has one. The
        # loop count is the fallback.
        if has_calls:
            calls = after[0] - before[0]
        extraction_state.llm_calls = calls
        result.llm_calls += calls
        result.llm_cache_hits += hits
        result.prompt_tokens = after[1] - before[1]
        result.completion_tokens = after[2] - before[2]
        result.llm_retries = after[3] - before[3]
        if limited:
            # The provider's limit was hit. The stage is resumable: the next
            # run picks it up where it stopped.
            extraction_state.status = STATUS_RATE_LIMITED
            extraction_state.detail = limit_detail or "the model was rate limited"
            result.rate_limited = True
            result.rate_limited_detail = extraction_state.detail
        else:
            extraction_state.status = STATUS_DONE
    else:
        extraction_state.status = STATUS_SKIPPED
    extraction_state.rows = len(result.requirements)
    result.stages[STAGE_EXTRACTION] = extraction_state

    # --- the resolution ---
    resolution_state = StageState(
        name=STAGE_RESOLUTION,
        key=keys[STAGE_RESOLUTION],
        depends_on=keys[STAGE_EXTRACTION],
    )
    resolution_state.status = STATUS_DONE if changed(STAGE_RESOLUTION) else STATUS_SKIPPED
    resolution_state.rows = len(result.requirements)
    result.stages[STAGE_RESOLUTION] = resolution_state

    # --- what is kept ---
    if repo is not None:
        _persist(repo, result, settings)
    return result


def _save_failed(repo, result: PipelineResult, settings) -> None:
    """Record a run that could not finish.

    The stages that did finish are recorded as they finished, and the one
    that failed is recorded as failed. A failed stage is not a permanent
    failure: the next run tries it again.
    """
    for name, state in result.stages.items():
        repo.save_stage(
            result.tender_id,
            name,
            status=state.status,
            key=state.key,
            extractor_version=settings.schema_version,
            prompt_version=_prompt_version(),
            detail=state.detail,
        )


def _persist(repo, result: PipelineResult, settings) -> None:
    """Write what the run produced, and the checkpoints that describe it.

    The requirements are written once, at the end: they are the output of
    the stages, and writing them per stage would leave a half-read tender in
    the database.

    A run where both the reading and the extraction were skipped produced no
    new requirement: rewriting them would give every row a new id and drop
    what the model read, which is not a no-op (D-93). The checkpoints are
    still saved.
    """
    if _requirements_rewritten(result):
        repo.replace_requirements(
            result.tender_id,
            [
                to_stored(
                    requirement,
                    result.tender_id,
                    model=settings.llm_model,
                    prompt_version=_prompt_version(),
                    schema_version=settings.schema_version,
                )
                for requirement in result.requirements
            ],
        )

    for name, state in result.stages.items():
        repo.save_stage(
            result.tender_id,
            name,
            status=state.status,
            key=state.key,
            extractor_version=settings.schema_version,
            prompt_version=_prompt_version(),
            detail=state.detail,
        )


def _requirements_rewritten(result: PipelineResult) -> bool:
    """Whether this run produced requirements the database does not have.

    Only a stage that actually ran changes them. If the reader and the
    extractor both skipped, the stored rows are already the answer.
    """
    return any(
        result.stages.get(name) is None
        or result.stages[name].status != STATUS_SKIPPED
        for name in (STAGE_REQUIREMENTS, STAGE_EXTRACTION)
    )


def _no_bill_reason(attachments: list[dict]) -> str:
    """Why there is no bill of quantities.

    "No bill of quantities was found" says what is missing; this says what
    the tender has instead, because a tender whose attachments are all
    certificates is a different thing from a tender whose attachments could
    not be fetched, and the two need different attention.
    """
    kinds = [record.get("kind") for record in attachments]
    if not kinds:
        return "the tender has no attachments"
    if all(kind == "form" for kind in kinds):
        return (
            "the tender's attachments are all forms, not a bill of quantities "
            "or a specification"
        )
    if "scanned" in kinds:
        return "the tender's attachments are scanned and no bill of quantities was read"
    return "no bill of quantities was found among the attachments"


def _read_items(attachment: dict, tender_id: str, tender_dir: Path) -> list[TenderItem]:
    """The items of a bill of quantities.

    A file that cannot be read gives no items: a broken attachment is not a
    tender with no items, and the stage records the failure.
    """
    path = tender_dir / (attachment.get("path") or "")
    if not path.exists():
        return []
    return read_boq_items(path, tender_id, attachment.get("category") or "unknown")


def assign_categories(items: list[TenderItem]) -> list[TenderItem]:
    """Give every item the category its own words name (FR-16).

    The category an attachment declares is the tender's coarse hint and is
    the same for every item in it. An item is the thing a requirement
    belongs to, so the item's own title and description decide, and an item
    that names no category - or two - is left `unknown` and flagged rather
    than given the tender's guess (FR-T9).
    """
    from app.services.match.category import infer_category

    for item in items:
        guess = infer_category(item.title, item.description)
        item.category = guess.category
        item.category_term = guess.term
        item.category_ambiguous = guess.ambiguous
        item.category_reason = guess.reason
    return items


def _reads_nothing(path: Path) -> bool:
    """True when a document's first page holds no text a reader could use.

    A scan is not a document that states nothing: it is a document nobody has
    read yet, and the two answers must not be merged (PRD 4). One page is
    enough to tell them apart, and it is read here - OCR included - before any
    model call, so a hopeless specification costs a second of local work
    instead of a call per clause (D-156).

    A file that cannot be opened is not this question's business: the reader
    reports that failure itself, and answering True here would report it as
    "unreadable by OCR".
    """
    from app.services.tender.parse import ocr_page_with_reason

    minimum = get_settings().ingest_ocr_min_chars
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            if document.page_count == 0:
                return True
            page = document[0]
            text = page.get_text() or ""
            if len(text.strip()) >= minimum:
                return False
            ocr, _ = ocr_page_with_reason(page)
            return len(ocr.strip()) < minimum
    except Exception:  # noqa: BLE001 - the open failure has its own answer
        return False


def _read_specification(attachment: dict, tender_dir: Path) -> SpecificationRead:
    """The clauses of a specification, and what happened to each page."""
    path = tender_dir / (attachment.get("path") or "")
    if not path.exists():
        return SpecificationRead(
            error=f"the specification file {path.name} does not exist"
        )
    return read_specification(path)


def _attachment_id(repo, tender_id: str, attachment: dict) -> Optional[str]:
    """The stored row of the attachment the pages came from.

    A page without an attachment cannot be said to have come from anywhere,
    so the ledger names the document it describes (FR-T2).
    """
    url = attachment.get("url")
    for record in repo.list_attachments(tender_id):
        if record.get("url") == url:
            return record.get("id")
    return None


def _ledger_is_empty(repo, tender_id: str) -> bool:
    """Whether no page of this tender has a state recorded yet.

    A repository that cannot answer is taken to have a ledger: not writing is
    the safe answer when the state of the pages is unknown.
    """
    lister = getattr(repo, "list_pages", None)
    if lister is None:
        return False
    return not lister(tender_id)


def _page_rows(pages: list[PageRead], attachment_id: Optional[str]) -> list[dict]:
    """The ledger rows of the pages that were read.

    Every page ends in a state. A page that failed carries the reason it
    failed, so a reviewer can see that it was looked at and why it gave
    nothing.
    """
    return [
        {
            "page": page.page,
            "tags": [page.tag] if page.tag else [],
            "extraction_method": page.method,
            "quality_warnings": [],
            "ledger_status": page.status,
            "skip_reason": page.skip_reason or page.error,
            "attachment_id": attachment_id,
        }
        for page in pages
    ]


def _extract_all(
    requirements: list[RequirementRecord],
    *,
    cache: Optional[LLMCache] = None,
    client=None,
    on_read=None,
) -> tuple[list[RequirementRecord], int, int, bool, Optional[str]]:
    """Read every requirement with the LLM, using the cache where it fits.

    Returns the requirements, the number of calls made, the number of answers
    that came from the cache, whether the provider's limit was hit, and the
    limit's detail. A line the model could not read is kept and flagged, never
    dropped: a failure to ask is not an answer that the line states nothing.

    `on_read` is called with the requirements read so far after each line, so
    the caller can publish a partial read (D-82). It is called on the same
    thread as the extraction, and it is the caller's job to throttle it.
    """
    enriched: list[RequirementRecord] = []
    calls = 0
    hits = 0
    limited = False
    limit_detail: Optional[str] = None

    for index, requirement in enumerate(requirements):
        added: list[RequirementRecord] = []
        readable = english_half(requirement.source_text or requirement.text)

        if requirement.review_required and not requirement.item_id:
            # An ambiguous mapping is flagged, not read.
            added = [requirement]
        elif requirement.page_tag and is_non_technical(requirement.page_tag):
            # The page is not a specification: legal prose, a safety annex or
            # boilerplate. The clause is recorded, and the model is not asked
            # about it (D-87).
            added = [requirement]
        elif is_mangled(requirement.text):
            requirement.unreadable = True
            requirement.review_required = True
            requirement.review_reason = "the line could not be read"
            added = [requirement]
        elif not readable:
            requirement.unreadable = True
            requirement.review_required = True
            requirement.review_reason = "the line has no readable text"
            added = [requirement]
        else:
            try:
                outcome, was_call, was_hit, error = _extract_one(
                    readable, requirement, cache=cache, client=client
                )
            except (LLMRateLimited, LLMBudgetExceeded) as exc:
                # Stop at the first limit. Everything from here on was never
                # asked about, so it stays as it was: the stage is resumable,
                # and dropping the tail would lose requirements the tender
                # stated. What was read before the limit is already written.
                limited = True
                limit_detail = str(exc)
                enriched.extend(requirements[index:])
                break

            calls += int(was_call)
            hits += int(was_hit)
            if outcome is None:
                # The line could not be read at all. It is kept and flagged: a
                # line that was not read is not a requirement that was not
                # there.
                requirement.review_required = True
                requirement.source_text = readable
                requirement.review_reason = (
                    f"the model could not read this line: {error}"
                    if error
                    else "the line could not be read"
                )
                added = [requirement]
            else:
                for extracted in outcome:
                    extracted.item_id = requirement.item_id
                    extracted.page = requirement.page
                    added.append(extracted)

        enriched.extend(added)
        if on_read is not None:
            on_read(enriched)

    return enriched, calls, hits, limited, limit_detail


def _extract_one(
    readable: str, requirement: RequirementRecord, *, cache=None, client=None
):
    """Read one line, from the cache when the same input was read before.

    Returns `(requirements, was_a_call, was_a_cache_hit, error)`. A cached
    answer is not a call. `requirements` is `None` when the model was asked
    and did not answer, and `error` says why: the caller keeps the line and
    flags it rather than reading the failure as an empty answer.
    """
    settings = get_settings()
    model = settings.llm_model or ""
    prompt_version = _prompt_version()
    schema_version = settings.schema_version
    input_hash = content_hash(readable, model, prompt_version, schema_version)

    if cache is not None:
        cached = cache.get(input_hash, model, prompt_version, schema_version)
        if cached is not None:
            return [_as_record(item) for item in cached], False, True, None

    outcome = extract_requirements(
        readable,
        requirement.item_id,
        client=client,
        page=requirement.page,
        section=requirement.source_section,
        # The reader said how the clause was read; the stored requirement has
        # to say the same thing, or a prose clause would be recorded as a
        # table row and never be atomized on a rerun.
        extraction_method=requirement.extraction_method,
    )
    if outcome.rejected:
        return None, True, False, 'source_quote_or_value_validation_failed'
    if outcome.error:
        # The model was asked and did not answer. Nothing is cached: the next
        # run has to ask again.
        return None, True, False, outcome.error
    if cache is not None and outcome.requirements:
        cache.put(
            input_hash,
            model,
            prompt_version,
            schema_version,
            [_as_dict(item) for item in outcome.requirements],
        )
    return list(outcome.requirements), True, False, None


def _as_dict(item: RequirementRecord) -> dict:
    """An extracted requirement, as it is cached."""
    return {
        "text": item.text,
        "source_text": item.source_text,
        "span": list(item.source_span) if item.source_span else None,
        "type": item.requirement_type.value,
        "attribute": item.attribute,
        "operator": item.operator,
        "value": item.value,
        "unit": item.unit,
        "mandatory": item.mandatory,
        "review_required": item.review_required,
        "review_reason": item.review_reason,
        # How the clause was read. A cached prose requirement that came back
        # as a table row would never be atomized again.
        "extraction_method": item.extraction_method,
    }


def _as_record(item) -> RequirementRecord:
    """A stored answer, as a requirement.

    A cached answer is a dict; a fresh one is already a record. Both are read
    the same way.
    """
    if not isinstance(item, dict):
        return item
    span = item.get("span")
    return RequirementRecord(
        text=item.get("text", ""),
        source_text=item.get("source_text"),
        item_id="",
        requirement_type=_type_of(item.get("type", "unknown")),
        attribute=item.get("attribute"),
        operator=item.get("operator"),
        value=item.get("value"),
        unit=item.get("unit"),
        mandatory=bool(item.get("mandatory", True)),
        review_required=bool(item.get("review_required", False)),
        review_reason=item.get("review_reason"),
        source_span=tuple(span) if span else None,
        # An answer cached before this field existed names no method; a table
        # row is the safe reading, because it is the one that is never split.
        extraction_method=item.get("extraction_method") or "table",
    )


def _type_of(value: str) -> RequirementType:
    try:
        return RequirementType(value)
    except ValueError:
        return RequirementType.UNKNOWN


def _stored_records(repo, tender_id: str) -> list[RequirementRecord]:
    """The requirements a tender already has, as records.

    A skipped stage produced nothing new: its output is what the last run
    wrote. Loading the stored rows keeps the model's types and everything the
    reader recorded, so a second run over an unchanged tender is a no-op
    instead of a re-read of the raw clauses (D-93).

    The page tag is not a column. It is recovered from the reason the clause
    carries, which is written from the same constant, so the two cannot drift
    apart.
    """
    records: list[RequirementRecord] = []
    for row in repo.list_requirements(tender_id):
        reason = row.get("review_reason") or None
        tag = None
        if reason and reason.startswith(NON_TECHNICAL_REASON):
            tag = reason[len(NON_TECHNICAL_REASON) :].strip() or None
        records.append(
            RequirementRecord(
                text=row.get("text") or row.get("source_text") or "",
                source_text=row.get("source_text"),
                source_span=tuple(row["source_span"]) if row.get("source_span") is not None else None,
                item_id=row.get("item_id") or "",
                requirement_type=_type_of(row.get("type")),
                attribute=row.get("attribute"),
                operator=row.get("operator"),
                value=row.get("value"),
                unit=row.get("unit"),
                mandatory=bool(row.get("mandatory", True)),
                page=row.get("page"),
                source_section=row.get("source_section"),
                extraction_method=row.get("extraction_method") or "table",
                page_tag=tag,
                confidence=row.get("confidence"),
                review_required=bool(row.get("review_required")),
                review_reason=reason,
            )
        )
    return records


def to_stored(
    requirement: RequirementRecord,
    tender_id: str,
    *,
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    input_hash: Optional[str] = None,
    schema_version: Optional[str] = None,
    extraction_version: Optional[str] = None,
) -> StoredRequirement:
    """A requirement, as it is stored.

    The versions default to what this deployment runs, not to a literal kept
    beside the signature: the stage keys and the cache are built from the
    setting, so a row stamped with a different version is a row nothing reads.
    """
    settings = get_settings()
    if schema_version is None:
        schema_version = settings.schema_version
    if extraction_version is None:
        extraction_version = EXTRACTION_VERSION
    return StoredRequirement(
        tender_id=tender_id,
        item_id=requirement.item_id or None,
        text=requirement.text,
        source_text=requirement.source_text or requirement.text,
        source_span=requirement.source_span,
        page=requirement.page or 1,
        requirement_type=requirement.requirement_type.value,
        attribute=requirement.attribute,
        operator=requirement.operator,
        value=requirement.value,
        unit=requirement.unit,
        mandatory=requirement.mandatory,
        section=requirement.source_section,
        extraction_method=requirement.extraction_method,
        review_required=requirement.review_required,
        review_reason=requirement.review_reason,
        confidence=requirement.confidence,
        model=model,
        prompt_version=prompt_version,
        input_hash=input_hash,
        extraction_version=extraction_version,
        schema_version=schema_version,
    )
