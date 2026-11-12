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
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.tender import LLMCache, StoredRequirement, TenderRepo
from app.schemas.enums import RequirementType, TenderReasonCode
from app.schemas.models import EXTRACTION_VERSION
from app.services.requirement.atomize import atomize_records
from app.services.requirement.extract import extract_requirements
from app.services.llm import LLMClient, LLMRateLimited, LLMBudgetExceeded
from app.services.tender.classify import DISCOVERY, WRAPPER_METADATA, NON_TECHNICAL_REASON, is_non_technical
from app.services.requirement.retype import retype_technical, apply_clause_type, retype
from app.services.requirement.structured_values import normalize_records, fingerprint as normalization_fingerprint
from app.services.tender.classify import classify_clause, classification_policy_fingerprint
from app.services.tender.source_policy import fingerprint as source_policy_fingerprint
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
    find_columns,
)

logger = get_logger(__name__)

STAGE_ATTACHMENTS = "attachments"
STAGE_ITEMS = "items"
STAGE_REQUIREMENTS = "requirements"
STAGE_EXTRACTION = "extraction"
STAGE_RESOLUTION = "resolution"


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


def _ocr_signature():
    from app.services.tender.ocr import ocr_signature
    return ocr_signature()


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
    validation_attempts: list[dict] = field(default_factory=list)
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
    wrapper_fingerprint: str = "",
    alias_fingerprint: str = "",
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
        _ocr_signature(),
        wrapper_fingerprint,
        classification_policy_fingerprint(),
        source_policy_fingerprint(),
    )

    # The extraction: the requirements, and what read them.
    extraction_parts = ["extraction", requirements_key, model, prompt_version, schema_version,
                        "source-first-2", get_settings().llm_max_tokens]
    from app.services.requirement.coverage import POLICY_VERSION
    extraction_parts.extend((POLICY_VERSION, vocabulary_hash))
    classification = _classification_signature()
    extraction_parts.append(classification)
    extraction_key = content_hash(*extraction_parts)

    # The resolution: what was extracted, and the vocabulary that reads it.
    resolution_key = content_hash(
        "resolution", extraction_key, vocabulary_hash, RESOLVER_VERSION, normalization_fingerprint(), alias_fingerprint
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

    from app.services.tender.collect import read_manifest
    from app.services.tender.upload import _wrapper
    from app.services.tender.integrity import verify_sources
    manifest_path = tender_dir / 'manifest.json'
    manifest = read_manifest(manifest_path)
    wrapper_path = _wrapper(tender_dir, manifest) if manifest_path.exists() else None
    wrapper = ({'path': str((wrapper_path or tender_dir / (manifest.get('filename') or 'missing-wrapper')).resolve()),
                'sha256': manifest.get('wrapper_sha256'), 'pages': manifest.get('page_count'),
                'source_key': 'wrapper'} if manifest_path.exists() else None)
    issues = verify_sources(tender_dir, attachments, wrapper=wrapper)
    if issues:
        result.failure = 'source integrity check failed'
        result.stages[STAGE_ATTACHMENTS] = StageState(
            name=STAGE_ATTACHMENTS, status=STATUS_FAILED,
            key=content_hash('source-integrity', [(r.get('sha256'), r['integrity_error']) for r in issues]),
            detail='; '.join(sorted({r['integrity_error'] for r in issues})))
        existing = repo.list_pages(tender_id) if repo is not None else []
        for record in issues:
            attachment_id = _attachment_id(repo, tender_id, record) if repo is not None and not record.get('source_key') else None
            key = record.get('source_key') or attachment_id or record.get('sha256') or 'legacy'
            aliases = {str(value) for value in (key, attachment_id, record.get('sha256')) if value}
            existing = [page for page in existing if str(page.get('source_key') or page.get('attachment_id') or 'legacy') not in aliases]
            failed = [PageRead(number, '', 'failed', error=record['integrity_error'],
                               source_key=key, attachment_id=attachment_id)
                      for number in range(1, (record.get('pages') or 0) + 1)]
            result.pages.extend(failed)
            existing.extend(_page_rows(failed, attachment_id))
        if repo is not None:
            repo.replace_pages(tender_id, existing)
            _save_failed(repo, result, settings)
        return result

    from app.services.tender.collect import read_manifest
    wrapper_fingerprint = read_manifest(tender_dir / 'manifest.json').get('wrapper_sha256') or ''
    aliases = []
    if repo is not None and getattr(repo, 'session', None) is not None:
        from app.repositories.aliases import AliasRepo
        aliases = AliasRepo(repo.session).list_all()
    keys = stage_keys(
        attachments,
        prompt_version=_prompt_version(),
        schema_version=settings.schema_version,
        model=settings.llm_model or "",
        vocabulary_hash=_vocabulary_hash(),
        fetch_config=settings.fetch_allowed_domains,
        wrapper_fingerprint=wrapper_fingerprint,
        alias_fingerprint=content_hash(sorted(aliases, key=lambda row: row['alias'])),
    )
    previous = repo.stage_keys(tender_id) if repo is not None else {}
    previous_status = (
        repo.stage_statuses(tender_id)
        if repo is not None and hasattr(repo, "stage_statuses")
        else {}
    )

    def changed(stage: str) -> bool:
        if stage == STAGE_REQUIREMENTS and previous_status.get(STAGE_EXTRACTION) == STATUS_FAILED:
            # Rebuild clean source records instead of inheriting failed-attempt flags.
            return True
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
        from app.services.tender.collect import read_manifest
        from app.services.tender.upload import _wrapper
        manifest_path = tender_dir / 'manifest.json'
        manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
        wrapper = _wrapper(tender_dir, manifest) if manifest else None
        if wrapper is not None:
            spec = {'path': str(wrapper), 'sha256': manifest.get('wrapper_sha256'),
                    'source_key': 'wrapper', 'pages': manifest.get('page_count'),
                    'item_titles': [item.title for item in result.items]}
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
        read = _read_package(tender_id, tender_dir, attachments, spec, repo)
        if read.error:
            if repo is not None:
                repo.replace_pages(tender_id, _page_rows(read.pages, _attachment_id(repo, tender_id, spec)))
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
                    source_text=row.get("row_text") or row.get("description", ""),
                    source_neighbors=row.get('source_neighbors'),
                    source_role=row.get('source_role'),
                    context_only=bool(row.get('context_only')),
                    candidate_type=row.get('candidate_type'),
                    table_context=({"headers": row["source_headers"], "cells": row["source_cells"]}
                                   if row.get("source_headers") and row.get("source_cells") else None),
                    item_id="",
                    requirement_type=(RequirementType(row["clause_type"]) if row.get("clause_type") else
                        RequirementType.INFORMATIONAL if tag in {DISCOVERY, WRAPPER_METADATA} else RequirementType.UNKNOWN),
                    page=row.get("page"),
                    source_section=row.get("title"),
                    extraction_method=row.get("extraction_method") or "table",
                    page_tag=tag or None,
                    review_required=not bool(row.get("context_only")),
                    review_reason=(
                        "Context retained: " + row["source_policy_reason"] if row.get("context_only") else
                        f"{NON_TECHNICAL_REASON} {tag}"
                        if classified
                        else "the specification row names no item in the bill of quantities"
                    ),
                      source_table_id=row.get('source_table_id'),
                      source_row_id=row.get('source_row_id'),
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
        from app.services.tender.source_policy import triage
        stated = {
            (requirement.item_id or "", requirement.text.strip().lower())
            for requirement in result.requirements
        }
        for requirement in requirements_from_items(result.items):
            key = (requirement.item_id, requirement.text.lower())
            if key in stated:
                continue
            stated.add(key)
            decision = triage(requirement.text, 'boq', section=requirement.source_section)
            requirement.source_role = 'boq'
            requirement.context_only = decision.context_only
            requirement.candidate_type = decision.candidate_type
            requirement.requirement_type = RequirementType(decision.clause_type)
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
            result.pages = _read_package(tender_id, tender_dir, attachments, spec, repo).pages
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
    if any(page.status == 'failed' for page in result.pages):
        requirements_state.status = STATUS_FAILED
        requirements_state.detail = 'source pages remain unreadable'
        result.failure = requirements_state.detail
        result.failure_code = TenderReasonCode.SPECIFICATION_UNREADABLE
        if repo is not None:
            _persist(repo, result, settings)
        return result
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
        validation_start = len(getattr(client, "validation_attempts", []))
        has_calls = getattr(client, "calls", None) is not None
        # Commit small response-cache entries during extraction; publish requirement
        # revisions only at completion or a resumable stop.
        last_write = 0.0

        def checkpoint_cache(_read_so_far) -> None:
            nonlocal last_write
            if repo is None:
                return
            now = time.monotonic()
            if now - last_write < settings.stream_interval_seconds:
                return
            last_write = now
            repo.session.commit()

        extracted, calls, hits, limited, limit_detail = _extract_all(
            result.requirements, cache=cache, client=client, on_read=checkpoint_cache
        )
        result.requirements = extracted
        # Source position and subject can correct proposed types; numeric recovery
        # only fills absent fields and never decides a match.
        retyped = retype_technical(result.requirements)
        if retyped:
            logger.info(
                "tender %s: %d clause(s) corrected by source typing or technical field recovery", tender_id, retyped
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
        result.validation_attempts = list(getattr(client, "validation_attempts", []))[validation_start:]
        if limited:
            # The provider's limit was hit. The stage is resumable: the next
            # run picks it up where it stopped.
            extraction_state.status = STATUS_RATE_LIMITED
            extraction_state.detail = limit_detail or "the model was rate limited"
            result.rate_limited = True
            result.rate_limited_detail = extraction_state.detail
        else:
            from app.services.requirement.coverage import MARKER
            from app.services.tender.source_policy import blocking_requirement
            gaps = sum(MARKER in (record.review_reason or '') and blocking_requirement(vars(record))
                       for record in result.requirements)
            extraction_state.status = STATUS_FAILED if gaps else STATUS_DONE
            if gaps:
                extraction_state.detail = f'{gaps} source clauses remain unextracted; retry uses valid cached answers'
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
    if changed(STAGE_RESOLUTION):
        normalize_records(result.requirements, aliases)
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
        for name in (STAGE_REQUIREMENTS, STAGE_EXTRACTION, STAGE_RESOLUTION)
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
    return read_specification(path, wrapper=True, item_titles=tuple(attachment.get("item_titles") or ())) if attachment.get("source_key") == "wrapper" else read_specification(path)


def _read_package(tender_id, directory, attachments, specification, repo):
    """Inspect each distinct source PDF locally; wrapper terms are not silently omitted."""
    from app.services.tender.collect import read_manifest
    from app.services.tender.upload import _wrapper
    from app.schemas.enums import LedgerStatus
    sources = [specification] + [a for a in attachments if a is not specification and
                                  (str(a.get('path') or '').casefold().endswith('.pdf') or a.get('pages') is not None)]
    manifest_path = directory / 'manifest.json'
    manifest = read_manifest(manifest_path) if manifest_path.exists() else {}
    wrapper = _wrapper(directory, manifest) if manifest else None
    known_hashes = {source.get('sha256') for source in sources if source.get('sha256')}
    if wrapper is not None:
        digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
        if digest not in known_hashes:
            sources.append({'path': str(wrapper), 'sha256': digest, 'source_key': 'wrapper',
                            'pages': manifest.get('page_count'),
                            'item_titles': [item['name'] for item in repo.list_items(tender_id)] if repo is not None else []})
    combined = SpecificationRead()
    seen = set()
    for source in sources:
        identity = source.get('sha256') or source.get('path')
        if identity in seen:
            continue
        seen.add(identity)
        attachment_id = (_attachment_id(repo, tender_id, source)
                         if repo is not None and source.get('source_key') != 'wrapper' else None)
        source_key = source.get('source_key') or attachment_id or source.get('sha256') or 'legacy'
        from app.services.tender.source_policy import source_role, triage
        role = source_role(source, specification=source is specification)
        read = _read_specification(source, directory)
        if read.error:
            combined.error = read.error
            for number in range(1, (source.get('pages') or 0) + 1):
                read.pages.append(PageRead(number, '', LedgerStatus.FAILED.value, error=read.error))
        for page in read.pages:
            page.source_key, page.attachment_id = source_key, attachment_id
            page.source_role = role
            page_rows = [row for row in read.rows if row['page'] == page.page]
            for row in page_rows:
                decision = triage(row.get('row_text') or row['description'], role,
                                  page_tag=row.get('page_tag'), section=row.get('title'))
                row.update(source_role=role, context_only=decision.context_only,
                           candidate_type=decision.candidate_type, clause_type=decision.clause_type,
                           source_policy_reason=decision.reason)
            for row in page_rows:
                row['source_neighbors'] = [candidate.get('row_text') or candidate['description']
                    for candidate in page_rows
                    if candidate is not row and candidate.get('title') == row.get('title')]
            context = page.source_context
            if context is None:
                context = {'headers': next((row['source_headers'] for row in page_rows if row.get('source_headers')), ['Field', 'Value'] if any(row.get('source_cells') for row in page_rows) else []),
                           'rows': [{'row_id': row['source_row_id'], 'cells': row['source_cells'],
                                     'bbox': row.get('source_bbox'), 'headers': row.get('source_headers'), 'header_page': row.get('source_header_page')} for row in page_rows if row.get('source_cells')], 'reading': {
                    'cache_key': str(source.get('sha256')) + ':' + str(page.page),
                    'source_sha256': source.get('sha256'), 'source_key': source_key,
                    'text_rows': page_rows}}
            if context is not None and repo is not None:
                context['reading']['source_sha256'] = source.get('sha256')
                context['reading']['source_key'] = source_key
                context['reading']['source_role'] = role
                columns = find_columns([label.casefold() for label in context['headers']])
                for row in context['rows']:
                    index = columns.get('part_number')
                    row['part_number'] = row['cells'][index] if index is not None else None
                identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, tender_id + '/' + source_key + '/' +
                                           context['reading']['cache_key'] + '/' + content_hash(context['headers'], context['rows'], _reader_version(), context['reading'].get('text_rows'))))
                repo.store_ocr_table(tender_id, identifier, page.page, attachment_id, context)
                for row in page_rows:
                    row['source_table_id'] = identifier
            combined.pages.append(page)
        combined.rows.extend(read.rows)
    return combined


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
            "tags": ([page.tag] if page.tag else []) + (["source_role:" + page.source_role] if page.source_role else []),
            "extraction_method": page.method,
            "quality_warnings": [],
            "ledger_status": page.status,
            "skip_reason": page.skip_reason or page.error,
            'attachment_id': page.attachment_id if page.source_key else attachment_id,
            'source_key': page.source_key or attachment_id or 'legacy',
        }
        for page in pages
    ]


def _classification_signature():
    from app.services.requirement.classify import signature
    enabled = get_settings().requirement_classification_enabled
    return (enabled, signature(), get_settings().requirement_classification_max_chars,
            "source-scope-6")


def _classification_context(readable, requirement):
    context = {'source_role': requirement.source_role or 'unknown',
               'section': english_half(requirement.source_section or ''), 'nearby_source_clauses': []}
    for source in requirement.source_neighbors or []:
        candidate = dict(context, nearby_source_clauses=context['nearby_source_clauses'] + [english_half(source)])
        if len(json.dumps({'clause': readable, 'context': candidate}, ensure_ascii=False)) <= get_settings().requirement_classification_max_chars:
            context = candidate
    from app.services.requirement.classify import cache_context
    return cache_context(readable, context)


def _classify_unscoped(readable, requirement, *, cache=None, client=None, proposal=None):
    from app.services.requirement.classify import classify_clause
    if proposal is None:
        label, called, hit, error = classify_clause(readable,
            context=_classification_context(readable, requirement), cache=cache, client=client)
    else:
        label, called, hit, error = proposal
    if label is None:
        return None, called, hit, error
    from app.services.tender.classify import NUMBER_WITH_UNIT, classify_clause as source_clause
    from app.services.requirement.retype import _attribute, _value
    from app.comparison.units import parse_unit, REGISTRY
    nontechnical = label in {RequirementType.COMMERCIAL, RequirementType.ELIGIBILITY, RequirementType.INFORMATIONAL}
    source_decision = source_clause(readable, page_tag=requirement.page_tag,
                                    source_section=requirement.source_section)
    physical_quantity = False
    for match in NUMBER_WITH_UNIT.finditer(readable):
        unit = parse_unit(re.sub(r'^[\d.,\s]+', '', match.group()))
        if unit is None or not REGISTRY.Unit(unit).dimensionless:
            physical_quantity = True
            break
    labelled_value = bool(_attribute(readable) and _value(readable)[0] is not None)
    technical_context = source_decision.kind == 'technical' or physical_quantity or labelled_value
    if nontechnical and not technical_context:
        requirement.requirement_type = label
        requirement.source_text = requirement.source_text or readable
        requirement.review_required = True
        requirement.review_reason = 'Source-wide nontechnical classification; retained for review outside capability scoring.'
        return [requirement], called, hit, None
    if label is RequirementType.UNKNOWN and not technical_context:
        outcome, extracted_call, extracted_hit, extraction_error = _extract_one(
            readable, requirement, cache=cache, client=client)
        from app.services.requirement.coverage import uncovered_tokens
        if (outcome and all(row.requirement_type in {
                RequirementType.COMMERCIAL, RequirementType.ELIGIBILITY, RequirementType.INFORMATIONAL}
                for row in outcome) and not uncovered_tokens(readable, outcome)):
            for row in outcome:
                row.review_required = True
                row.review_reason = 'Source-supported nontechnical clauses; retained for review outside capability scoring.'
            return outcome, called or extracted_call, hit and extracted_hit, None
    requirement.review_required = True
    requirement.review_reason = '; '.join(part for part in (
        requirement.review_reason, f'Proposed clause type: {label.value}; classification and BOQ scope require review.'
    ) if part)
    return [requirement], called, hit, None


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

    client_calls_before = getattr(client, "calls", None)
    seen_sources = set()
    for index, requirement in enumerate(requirements):
        if requirement.source_text and requirement.source_table_id and requirement.source_row_id is not None:
            source_key = (requirement.item_id, requirement.page, requirement.source_table_id,
                          requirement.source_row_id, requirement.source_text)
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
        added: list[RequirementRecord] = []
        original_source = requirement.source_text or requirement.text
        readable = english_half(original_source)
        source_type = classify_clause(original_source, page_tag=requirement.page_tag,
                                      source_section=requirement.source_section)
        if requirement.context_only:
            added = [requirement]
        elif source_type.kind in {'commercial', 'eligibility', 'informational'} and not source_type.review_required:
            apply_clause_type(requirement)
            if requirement.source_role:
                requirement.context_only = True
                requirement.candidate_type = None
            requirement.source_text = original_source
            added = [requirement]
        elif requirement.review_required and not requirement.item_id and not requirement.candidate_type and not (
            get_settings().requirement_classification_enabled
            and requirement.requirement_type is RequirementType.UNKNOWN
        ):
            # An ambiguous mapping is flagged, not read.
            added = [requirement]
        elif requirement.page_tag and is_non_technical(requirement.page_tag) and source_type.kind != "technical" and not requirement.candidate_type:
            # Keep unresolved nontechnical context for review. A decisive
            # product obligation in buyer-added terms bypasses this coarse tag.
            added = [requirement]
        elif is_mangled(requirement.text):
            requirement.unreadable = True
            requirement.review_required = True
            requirement.source_text = requirement.source_text or requirement.text
            requirement.review_reason = '; '.join(part for part in (
                requirement.review_reason, 'source_coverage_gap: the line could not be read'
            ) if part)
            added = [requirement]
        elif not readable:
            requirement.unreadable = True
            requirement.review_required = True
            requirement.source_text = requirement.source_text or requirement.text
            requirement.review_reason = '; '.join(part for part in (
                requirement.review_reason, 'source_coverage_gap: the line has no readable text'
            ) if part)
            added = [requirement]
        else:
            try:
                reader = (_classify_unscoped if not requirement.item_id and
                          requirement.requirement_type is RequirementType.UNKNOWN else _extract_one)
                options = {}
                outcome, was_call, was_hit, error = reader(
                    readable, requirement, cache=cache, client=client, **options)
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
                requirement.review_reason = '; '.join(part for part in (
                    requirement.review_reason,
                    f"the model could not read this line: {error}" if error else "the line could not be read",
                    'source_coverage_gap: extraction did not resolve the source clause' if reader is _extract_one else None
                ) if part)
                added = [requirement]
            else:
                from app.services.requirement.coverage import uncovered_tokens, MARKER
                gap = uncovered_tokens(readable, outcome) if reader is _extract_one else []
                if not outcome:
                    requirement.requirement_type = RequirementType.UNKNOWN
                    requirement.review_required = True
                    requirement.source_text = readable
                    requirement.review_reason = '; '.join(part for part in (
                        requirement.review_reason, f'{MARKER}: extractor returned no source facts'
                    ) if part)
                    added.append(requirement)
                for extracted in outcome:
                    from app.services.requirement.structured_values import resolve_name
                    canonical, _ = resolve_name(extracted.attribute)
                    if (requirement.source_role and requirement.candidate_type == 'technical' and canonical
                            and extracted.requirement_type.value in {'commercial', 'eligibility', 'informational'}):
                        extracted.requirement_type = RequirementType.UNKNOWN
                        extracted.review_required = True
                        extracted.review_reason = 'Source candidate has a technical attribute; clause typing needs review'
                    extracted.source_role = requirement.source_role
                    extracted.context_only = requirement.context_only or bool(requirement.source_role and
                        extracted.requirement_type.value in {'commercial', 'eligibility', 'informational'})
                    extracted.candidate_type = requirement.candidate_type
                    extracted.table_context = requirement.table_context
                    extracted.source_neighbors = requirement.source_neighbors
                    extracted.source_text = original_source
                    extracted.page_tag = requirement.page_tag
                    extracted.source_section = requirement.source_section
                    extracted.review_required = extracted.review_required or requirement.review_required or bool(gap)
                    extracted.review_reason = '; '.join(part for part in (
                        requirement.review_reason, extracted.review_reason,
                        f'{MARKER}: unrepresented source tokens: ' + ', '.join(gap) if gap else None
                    ) if part) or None
                    extracted.item_id = requirement.item_id
                    extracted.page = requirement.page
                    extracted.source_table_id = requirement.source_table_id
                    extracted.source_row_id = requirement.source_row_id
                    retype(extracted)
                    added.append(extracted)

        for record in added:
            retype(record)
        enriched.extend(added)
        if on_read is not None:
            on_read(enriched)

    client_calls_after = getattr(client, "calls", None)
    if isinstance(client_calls_before, int) and isinstance(client_calls_after, int):
        calls = client_calls_after - client_calls_before
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
    input_hash = content_hash(readable, model, prompt_version, schema_version,
                              requirement.page_tag, requirement.source_section,
                              classification_policy_fingerprint(),
                              json.dumps(requirement.table_context, sort_keys=True),
                              requirement.source_role, requirement.candidate_type)

    if cache is not None:
        cached = cache.get(input_hash, model, prompt_version, schema_version)
        if cached is not None:
            from app.services.requirement.coverage import uncovered_tokens
            records = [_as_record(item) for item in cached]
            if records and not uncovered_tokens(readable, records):
                return records, False, True, None

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
        table_context=requirement.table_context,
    )
    if outcome.rejected:
        return None, True, False, 'source_coverage_gap: source_quote_or_value_validation_failed'
    if outcome.error:
        # The model was asked and did not answer. Nothing is cached: the next
        # run has to ask again.
        return None, True, False, outcome.error
    for extracted in outcome.requirements:
        extracted.source_role = requirement.source_role
        extracted.context_only = requirement.context_only
        extracted.candidate_type = requirement.candidate_type
        extracted.table_context = requirement.table_context
        extracted.source_text = requirement.source_text or requirement.text
        extracted.page_tag = requirement.page_tag
        extracted.source_section = requirement.source_section
        retype(extracted)
    from app.services.requirement.coverage import uncovered_tokens
    if cache is not None and outcome.requirements and not uncovered_tokens(readable, outcome.requirements):
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
        "page_tag": item.page_tag,
        "source_section": item.source_section,
        "normalization": item.normalization,
        "table_context": item.table_context,
        "source_neighbors": item.source_neighbors,
        "source_role": item.source_role,
        "context_only": item.context_only,
        "candidate_type": item.candidate_type,
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
        page_tag=item.get("page_tag"),
        source_section=item.get("source_section"),
        normalization=item.get("normalization"),
        table_context=item.get("table_context"),
        source_neighbors=item.get('source_neighbors'),
        source_role=item.get("source_role"),
        context_only=bool(item.get("context_only")),
        candidate_type=item.get("candidate_type"),
        item_id="",
        requirement_type=_type_of(item.get("type", "unknown")),
        attribute=item.get("attribute"),
        operator=item.get("operator"),
        value=item.get("value"),
        unit=item.get("unit"),
        mandatory=bool(item.get("mandatory", True)),
        review_required=bool(item.get('review_required', False)),
        review_reason=item.get('review_reason'),
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
        normalization = row.get("normalization") or {}
        tag = normalization.get("source_context", {}).get("page_tag")
        if not tag and reason and reason.startswith(NON_TECHNICAL_REASON):
            tag = reason[len(NON_TECHNICAL_REASON) :].strip() or None
        records.append(
            RequirementRecord(
                text=row.get("text") or row.get("source_text") or "",
                source_text=row.get("source_text"),
                source_span=tuple(row['source_span']) if row.get('source_span') is not None else None,
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
                normalization=row.get("normalization"),
                table_context=normalization.get("source_context", {}).get("table"),
                source_neighbors=normalization.get('source_context', {}).get('nearby_source_clauses'),
                source_role=normalization.get('source_context', {}).get('source_role'),
                context_only=bool(normalization.get('source_context', {}).get('context_only')),
                candidate_type=normalization.get('source_context', {}).get('candidate_type'),
                confidence=row.get("confidence"),
                review_required=bool(row.get("review_required")),
                review_reason=reason,
                  source_table_id=row.get('source_table_id'),
                  source_row_id=row.get('source_row_id'),
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
    if requirement.source_role and requirement.context_only and requirement.candidate_type is None:
        model = None
    return StoredRequirement(
        tender_id=tender_id,
        item_id=requirement.item_id or None,
        text=requirement.text,
        source_text=requirement.source_text or requirement.text,
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
        source_table_id=requirement.source_table_id,
        source_row_id=requirement.source_row_id,
        source_span=requirement.source_span,
        normalization=dict(requirement.normalization or {},
                           source_context={"page_tag": requirement.page_tag, "table": requirement.table_context,
                                           "nearby_source_clauses": requirement.source_neighbors,
                                           **({"source_role": requirement.source_role, "context_only": requirement.context_only,
                                               "candidate_type": requirement.candidate_type} if requirement.source_role else {})}),
    )
