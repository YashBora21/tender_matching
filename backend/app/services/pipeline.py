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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.tender import LLMCache, StoredRequirement, TenderRepo
from app.schemas.enums import RequirementType
from app.services.extractor import (
    PROMPT_VERSION,
    ExtractedRequirement,
    extract_requirements,
)
from app.services.tenderingest import (
    RequirementRecord,
    TenderItem,
    assign_to_items,
    english_half,
    is_mangled,
    read_boq_csv,
    read_boq_table,
    read_specification,
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
    attachments: list[dict] = field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    # The reason the tender could not be read, when it could not.
    failure: Optional[str] = None

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
    attachments_key = content_hash("attachments", fetch_config, attachment_parts)

    # The items: the bill of quantities and how it is read.
    boq = _attachment_by_kind(attachments, "boq-csv", "boq-pdf")
    items_key = content_hash("items", attachments_key, boq.get("sha256") if boq else "")

    # The requirements: the specification and the items they hang from.
    spec = _attachment_by_kind(attachments, "boq-pdf", "spec")
    requirements_key = content_hash(
        "requirements", items_key, spec.get("sha256") if spec else ""
    )

    # The extraction: the requirements, and what read them.
    extraction_key = content_hash(
        "extraction", requirements_key, model, prompt_version, schema_version
    )

    # The resolution: what was extracted, and the vocabulary that reads it.
    resolution_key = content_hash("resolution", extraction_key, vocabulary_hash)

    return {
        STAGE_ATTACHMENTS: attachments_key,
        STAGE_ITEMS: items_key,
        STAGE_REQUIREMENTS: requirements_key,
        STAGE_EXTRACTION: extraction_key,
        STAGE_RESOLUTION: resolution_key,
    }


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
        prompt_version=PROMPT_VERSION,
        schema_version=settings.schema_version,
        model=settings.llm_model or "",
        vocabulary_hash=_vocabulary_hash(),
        fetch_config=settings.fetch_allowed_domains,
    )
    previous = repo.stage_keys(tender_id) if repo is not None else {}

    def changed(stage: str) -> bool:
        return force or keys[stage] != previous.get(stage)

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
    boq = _attachment_by_kind(attachments, "boq-csv", "boq-pdf")
    if boq is None:
        items_state.status = STATUS_FAILED
        items_state.detail = _no_bill_reason(attachments)
        result.stages[STAGE_ITEMS] = items_state
        result.failure = items_state.detail
        if repo is not None:
            _save_failed(repo, result, settings)
        return result
    result.items = _read_items(boq, tender_id, tender_dir)
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
    spec = _attachment_by_kind(attachments, "boq-pdf", "spec")
    if spec is None:
        requirements_state.status = STATUS_FAILED
        requirements_state.detail = "no specification was found"
        result.stages[STAGE_REQUIREMENTS] = requirements_state
        result.failure = requirements_state.detail
        if repo is not None:
            _save_failed(repo, result, settings)
        return result
    rows = _read_specification(spec, tender_dir)
    result.requirements, unassigned = assign_to_items(rows, result.items)
    for row in unassigned:
        # A requirement that names no item is kept and flagged: an ambiguous
        # mapping is never guessed at (FR-T9).
        result.requirements.append(
            RequirementRecord(
                text=row.get("description", ""),
                item_id="",
                requirement_type=RequirementType.UNKNOWN,
                page=row.get("page"),
                source_section=row.get("title"),
                review_required=True,
                review_reason=(
                    "the specification row names no item in the bill of quantities"
                ),
            )
        )
    requirements_state.status = STATUS_DONE if changed(STAGE_REQUIREMENTS) else STATUS_SKIPPED
    requirements_state.rows = len(result.requirements)
    result.stages[STAGE_REQUIREMENTS] = requirements_state

    # --- the extraction (the only stage that calls the LLM) ---
    extraction_state = StageState(
        name=STAGE_EXTRACTION,
        key=keys[STAGE_EXTRACTION],
        depends_on=keys[STAGE_REQUIREMENTS],
    )
    if changed(STAGE_EXTRACTION):
        extracted, calls, hits, limited = _extract_all(
            result.requirements, cache=cache, client=client
        )
        result.requirements = extracted
        extraction_state.llm_calls = calls
        result.llm_calls += calls
        result.llm_cache_hits += hits
        if limited:
            # The provider's limit was hit. The stage is resumable: the next
            # run picks it up where it stopped.
            extraction_state.status = STATUS_RATE_LIMITED
            extraction_state.detail = "the model was rate limited"
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
            prompt_version=PROMPT_VERSION,
        )


def _persist(repo, result: PipelineResult, settings) -> None:
    """Write what the run produced, and the checkpoints that describe it.

    The requirements are written once, at the end: they are the output of
    the stages, and writing them per stage would leave a half-read tender in
    the database.
    """
    repo.replace_requirements(
        result.tender_id,
        [
            to_stored(
                requirement,
                result.tender_id,
                model=settings.llm_model,
                prompt_version=PROMPT_VERSION,
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
            prompt_version=PROMPT_VERSION,
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
    if path.suffix.lower() == ".csv":
        return read_boq_csv(path, tender_id, attachment.get("category") or "unknown")
    import pdfplumber

    items: list[TenderItem] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    items.extend(read_boq_table(table, tender_id, "unknown"))
    except Exception as exc:  # noqa: BLE001 - a file that cannot be read gives no items
        logger.warning("could not read the items of %s: %s", path, exc)
        return []
    return items


def _read_specification(attachment: dict, tender_dir: Path) -> list[dict]:
    """The rows of a specification."""
    path = tender_dir / (attachment.get("path") or "")
    if not path.exists():
        return []
    return read_specification(path)


def _extract_all(
    requirements: list[RequirementRecord],
    *,
    cache: Optional[LLMCache] = None,
    client=None,
) -> tuple[list[RequirementRecord], int, int, bool]:
    """Read every requirement with the LLM, using the cache where it fits.

    Returns the requirements, the number of calls made, the number of answers
    that came from the cache, and whether the provider's limit was hit.
    """
    enriched: list[RequirementRecord] = []
    calls = 0
    hits = 0
    limited = False

    for requirement in requirements:
        if requirement.review_required and not requirement.item_id:
            # An ambiguous mapping is flagged, not read.
            enriched.append(requirement)
            continue
        if is_mangled(requirement.text):
            requirement.unreadable = True
            requirement.review_required = True
            requirement.review_reason = "the line could not be read"
            enriched.append(requirement)
            continue

        readable = english_half(requirement.text)
        if not readable:
            requirement.unreadable = True
            requirement.review_required = True
            requirement.review_reason = "the line has no readable text"
            enriched.append(requirement)
            continue

        outcome, was_call, was_hit = _extract_one(
            readable, requirement, cache=cache, client=client
        )
        calls += int(was_call)
        hits += int(was_hit)
        if outcome is None:
            # The line could not be read at all. It is kept and flagged: a
            # line that was not read is not a requirement that was not there.
            requirement.review_required = True
            requirement.review_reason = "the line could not be read"
            enriched.append(requirement)
            continue

        for extracted in outcome:
            extracted.item_id = requirement.item_id
            extracted.page = requirement.page
            enriched.append(extracted)

    return enriched, calls, hits, limited


def _extract_one(
    readable: str, requirement: RequirementRecord, *, cache=None, client=None
):
    """Read one line, from the cache when the same input was read before.

    Returns `(requirements, was_a_call, was_a_cache_hit)`. A cached answer is
    not a call.
    """
    settings = get_settings()
    model = settings.llm_model or ""
    prompt_version = PROMPT_VERSION
    schema_version = settings.schema_version
    input_hash = content_hash(readable, model, prompt_version, schema_version)

    if cache is not None:
        cached = cache.get(input_hash, model, prompt_version, schema_version)
        if cached is not None:
            return [_as_record(item) for item in cached], False, True

    outcome = extract_requirements(
        readable,
        requirement.item_id,
        client=client,
        page=requirement.page,
        section=requirement.source_section,
    )
    if cache is not None and outcome.requirements:
        cache.put(
            input_hash,
            model,
            prompt_version,
            schema_version,
            [_as_dict(item) for item in outcome.requirements],
        )
    return (list(outcome.requirements), True, False)


def _as_dict(item: RequirementRecord) -> dict:
    """An extracted requirement, as it is cached."""
    return {
        "text": item.text,
        "span": list(item.source_span) if item.source_span else None,
        "type": item.requirement_type.value,
        "attribute": item.attribute,
        "operator": item.operator,
        "value": item.value,
        "unit": item.unit,
        "mandatory": item.mandatory,
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
        item_id="",
        requirement_type=_type_of(item.get("type", "unknown")),
        attribute=item.get("attribute"),
        operator=item.get("operator"),
        value=item.get("value"),
        unit=item.get("unit"),
        mandatory=bool(item.get("mandatory", True)),
        source_span=tuple(span) if span else None,
    )


def _type_of(value: str) -> RequirementType:
    try:
        return RequirementType(value)
    except ValueError:
        return RequirementType.UNKNOWN


def to_stored(
    requirement: RequirementRecord,
    tender_id: str,
    *,
    model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    input_hash: Optional[str] = None,
    schema_version: str = "1",
    extraction_version: str = "1",
) -> StoredRequirement:
    """A requirement, as it is stored."""
    return StoredRequirement(
        tender_id=tender_id,
        item_id=requirement.item_id or None,
        text=requirement.text,
        source_text=requirement.text,
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
