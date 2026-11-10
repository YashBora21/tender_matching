"""Persistence for the tender pipeline.

Three things are stored:

- the **corpus of a tender**: the `tender` row, its attachments, its items,
  its page ledger and its requirements;
- the **stage checkpoints**: which stage produced what, and the key of the
  inputs it was given, so a run that changed nothing does nothing;
- the **LLM cache**: an answer, keyed by the input it was read from and
  everything that read it.

The checkpoints live in `extraction_batch`, which the PRD gives for exactly
this: "persist each completed batch so a failed run resumes instead of
restarting".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.schemas.enums import TenderReasonCode, TenderStatus

from app.core.logging import get_logger
from app.repositories.schema import (
    extraction_batch,
    llm_cache,
    requirement,
    result,
    run,
    tender,
    tender_attachment,
    tender_item,
    tender_page,
    tender_table,
)

logger = get_logger(__name__)


@dataclass
class StoredRequirement:
    """A requirement, as it is stored."""

    tender_id: str
    item_id: Optional[str]
    text: str
    source_text: str
    page: int
    requirement_type: str
    # No default: these are the versions the deployment is running, and a row
    # stamped with a literal written here would say nothing about the shape it
    # was written in.
    extraction_version: str
    schema_version: str
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    mandatory: bool = True
    section: Optional[str] = None
    extraction_method: str = "table"
    review_required: bool = False
    review_reason: Optional[str] = None
    confidence: Optional[float] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    input_hash: Optional[str] = None
    source_table_id: Optional[str] = None
    source_row_id: Optional[str] = None
    source_span: Optional[tuple[int, int]] = None
    normalization: Optional[dict] = None


class TenderRepo:
    """The tender corpus, in PostgreSQL."""

    def __init__(self, session: Session):
        self.session = session

    # --- the tender ---

    def get_tender(self, tender_id: str) -> Optional[dict]:
        row = self.session.execute(
            select(
                tender.c.id,
                tender.c.filename,
                tender.c.sha256,
                tender.c.page_count,
                tender.c.status,
                tender.c.error,
                tender.c.reason_code,
            )
            .where(tender.c.id == tender_id)
        ).first()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "filename": row[1],
            "sha256": row[2],
            "page_count": row[3],
            "status": row[4],
            "error": row[5],
            "reason_code": row[6],
        }

    def tenders_in_states(self, statuses: tuple[TenderStatus, ...]) -> list[dict]:
        """Every tender left in one of these states, oldest first.

        Used at startup to find a read that no process is going to finish
        (D-120): the state is written by the background task, so a task that
        died leaves it behind.
        """
        rows = self.session.execute(
            select(tender.c.id, tender.c.filename, tender.c.status)
            .where(
                tender.c.status.in_(
                    [getattr(status, "value", status) for status in statuses]
                )
            )
            .order_by(tender.c.uploaded_at)
        ).all()
        return [
            {"id": str(row[0]), "filename": row[1], "status": row[2]} for row in rows
        ]

    def set_status(
        self,
        tender_id: str,
        status: TenderStatus,
        *,
        error: Optional[str] = None,
        reason_code: Optional[TenderReasonCode] = None,
    ) -> None:
        """Move a tender's ingestion to a state, recording why when it failed.

        The reason is written together with the state, including when there is
        none: the column describes *this* attempt, and a read that starts while
        the previous attempt's message is still on the row reports a cause it
        never had (D-120). `reason_code` is the same fact as a stable key, so a
        screen can say which of the three outcomes it is without parsing a
        sentence (D-156).
        """
        self.session.execute(
            update(tender)
            .where(tender.c.id == tender_id)
            .values(
                status=status.value,
                error=error,
                reason_code=reason_code.value if reason_code else None,
            )
        )

    def upsert_tender(
        self,
        *,
        filename: str,
        sha256: str,
        page_count: Optional[int] = None,
        category: Optional[str] = None,
        processing_version: Optional[str] = None,
        status: TenderStatus = TenderStatus.DONE,
    ) -> str:
        """The tender's row, found by the hash of its file.

        A tender is identified by what it contains, so the same file is the
        same tender however many times it is offered. `status` applies only
        when the row is created: re-offering a file the database already has
        does not reset what its ingestion has already reached.
        """
        existing = self.session.execute(
            select(tender.c.id).where(tender.c.sha256 == sha256)
        ).first()
        if existing:
            # A field the caller does not state has not said the stored value
            # is wrong, so it is left alone.
            values = {
                "filename": filename,
                "page_count": page_count,
                "category": category,
                "processing_version": processing_version,
            }
            values = {key: value for key, value in values.items() if value not in (None, "")}
            if values:
                self.session.execute(
                    update(tender).where(tender.c.id == existing[0]).values(**values)
                )
            return str(existing[0])

        tender_id = uuid.uuid4()
        self.session.execute(
            insert(tender).values(
                id=tender_id,
                filename=filename,
                sha256=sha256,
                page_count=page_count,
                category=category,
                processing_version=processing_version,
                status=status.value,
            )
        )
        return str(tender_id)

    def find_tender_by_filename(self, filename: str) -> Optional[str]:
        row = self.session.execute(
            select(tender.c.id).where(tender.c.filename == filename).limit(1)
        ).first()
        return str(row[0]) if row else None

    def resolve_tender(self, manifest: dict, tender_dir: Path) -> Optional[str]:
        """The tender's id, from the manifest that describes it.

        A manifest names the tender by its file, and the database identifies
        it by the hash of that file. The two are reconciled here, so the rest
        of the pipeline works with the database's id and never with a name.
        """
        from app.services.tender.collect import manifest_identity

        sha256, filename = manifest_identity(manifest, tender_dir)
        if not sha256:
            return None
        return self.upsert_tender(
            filename=filename or "",
            sha256=sha256,
            page_count=manifest.get("page_count"),
            category=manifest.get("category"),
        )

    # --- the attachments ---

    def upsert_attachment(self, tender_id: str, record: dict) -> str:
        """An attachment's row, found by its URL."""
        existing = self.session.execute(
            select(tender_attachment.c.id).where(
                tender_attachment.c.tender_id == tender_id,
                tender_attachment.c.url == record["url"],
            )
        ).first()
        values = {
            "url": record["url"],
            "filename": record.get("filename"),
            "path": record.get("path"),
            "sha256": record.get("sha256"),
            "kind": record.get("kind"),
            "status": record.get("status", "new"),
            "note": record.get("note"),
            "bytes": record.get("bytes"),
            "pages": record.get("pages"),
            "retrieved_at": record.get("retrieved_at"),
        }
        if existing:
            self.session.execute(
                update(tender_attachment)
                .where(tender_attachment.c.id == existing[0])
                .values(**values)
            )
            return str(existing[0])
        attachment_id = uuid.uuid4()
        self.session.execute(
            insert(tender_attachment).values(id=attachment_id, tender_id=tender_id, **values)
        )
        return str(attachment_id)

    def list_attachments(self, tender_id: str) -> list[dict]:
        rows = self.session.execute(
            select(
                tender_attachment.c.id,
                tender_attachment.c.url,
                tender_attachment.c.filename,
                tender_attachment.c.path,
                tender_attachment.c.sha256,
                tender_attachment.c.kind,
                tender_attachment.c.status,
                tender_attachment.c.note,
                tender_attachment.c.bytes,
                tender_attachment.c.pages,
            )
            .where(tender_attachment.c.tender_id == tender_id)
            .order_by(tender_attachment.c.url)
        ).all()
        return [
            {
                "id": str(row[0]),
                "url": row[1],
                "filename": row[2],
                "path": row[3],
                "sha256": row[4],
                "kind": row[5],
                "status": row[6],
                "note": row[7],
                "bytes": row[8],
                "pages": row[9],
            }
            for row in rows
        ]

    # --- the items ---

    def _preserve_run_inputs(self, tender_id: str) -> None:
        """Keep actual result-linked legacy rows before changing current inputs.

        Old runs did not record their complete input set. Do not claim that
        today's unmatched rows or item labels belonged to those runs.
        """
        for run_id, config in self.session.execute(
            select(run.c.id, run.c.config_snapshot).where(run.c.tender_id == tender_id)
        ).all():
            config = dict(config or {})
            if "tender_inputs" in config:
                continue
            linked = self.session.execute(
                select(result.c.requirement_id).where(result.c.run_id == run_id)
            ).scalars().all()
            rows = self.list_requirements(tender_id, requirement_ids=linked)
            config["tender_inputs"] = {
                "requirements": rows, "items": [], "complete": False,
                "origin": "legacy_result_links", "tender_id": str(tender_id),
            }
            self.session.execute(update(run).where(run.c.id == run_id).values(config_snapshot=config))

    def _supersede_requirements(self, tender_id: str) -> None:
        self._preserve_run_inputs(tender_id)
        self.session.execute(update(requirement).where(
            requirement.c.tender_id == tender_id,
            requirement.c.superseded_at.is_(None),
        ).values(superseded_at=datetime.now(timezone.utc)))

    def replace_items(self, tender_id: str, items: list[dict]) -> int:
        """The tender's items, replacing what was there.

        Previous requirements are superseded and item rows are deactivated.
        Recorded run inputs retain their original item labels.

        A number that appears twice is written once: the key is the tender
        and the number, and a bill of quantities that repeats a number is a
        document with a mistake in it, not a reason to lose the tender.
        """
        self._supersede_requirements(tender_id)
        self.session.execute(update(tender_item).where(
            tender_item.c.tender_id == tender_id
        ).values(active=False))
        if not items:
            return 0

        unique: list[dict] = []
        seen: set[str] = set()
        for item in items:
            key = item["item_id"]
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)

        for item in unique:
            values = {
                "tender_id": tender_id, "item_id": item["item_id"],
                "name": item.get("name") or "", "category": item.get("category"),
                "boq_ref": item.get("boq_ref"), "active": True,
            }
            self.session.execute(pg_insert(tender_item).values(**values).on_conflict_do_update(
                index_elements=[tender_item.c.tender_id, tender_item.c.item_id],
                set_={key: value for key, value in values.items() if key not in {"tender_id", "item_id"}},
            ))
        return len(unique)

    def list_items(self, tender_id: str) -> list[dict]:
        rows = self.session.execute(
            select(
                tender_item.c.item_id,
                tender_item.c.name,
                tender_item.c.category,
                tender_item.c.boq_ref,
            ).where(tender_item.c.tender_id == tender_id, tender_item.c.active.is_(True))
        ).all()
        return [
            {"item_id": row[0], "name": row[1], "category": row[2], "boq_ref": row[3]}
            for row in rows
        ]

    # --- the requirements ---

    def replace_requirements(self, tender_id: str, requirements: list[StoredRequirement]) -> int:
        """The tender's requirements, replacing what was there.

        Previous rows and their results remain addressable by their original IDs.
        """
        self._supersede_requirements(tender_id)
        if not requirements:
            return 0
        self.session.execute(
            insert(requirement),
            [
                {
                    "id": uuid.uuid4(),
                    "tender_id": tender_id,
                    "item_id": item.item_id or None,
                    "source_page": item.page,
                    "source_section": item.section,
                    "source_text": item.source_text,
                    "source_span": list(item.source_span) if item.source_span is not None else None,
                    "normalization": item.normalization,
                    "text": item.text,
                    "type": item.requirement_type,
                    "attribute": item.attribute,
                    "operator": item.operator,
                    "value": item.value,
                    "unit": item.unit,
                    "mandatory": item.mandatory,
                    "extraction_method": item.extraction_method,
                    "extraction_confidence": item.confidence,
                    "review_required": item.review_required,
                    "review_reason": item.review_reason,
                    "extraction_version": item.extraction_version,
                    "schema_version": item.schema_version,
                    "model": item.model,
                    "prompt_version": item.prompt_version,
                    "input_hash": item.input_hash,
                    "source_table_id": item.source_table_id,
                    "source_row_id": item.source_row_id,
                }
                for item in requirements
            ],
        )
        return len(requirements)

    def count_requirements(self, tender_id: str) -> int:
        """How many requirements this tender has stored.

        A count, always an int: the previous version returned the empty result
        list when there were none, so a caller comparing it to zero raised
        instead of answering (D-120).
        """
        return self.session.execute(
            select(func.count())
            .select_from(requirement)
            .where(requirement.c.tender_id == tender_id, requirement.c.superseded_at.is_(None))
        ).scalar_one()

    def list_requirements(self, tender_id: str, *, requirement_ids: Optional[list] = None) -> list[dict]:
        rows = self.session.execute(
            select(
                requirement.c.id,
                requirement.c.item_id,
                requirement.c.text,
                requirement.c.source_text,
                requirement.c.source_page,
                requirement.c.source_section,
                requirement.c.extraction_method,
                requirement.c.type,
                requirement.c.attribute,
                requirement.c.review_required,
                requirement.c.review_reason,
                requirement.c.value,
                requirement.c.unit,
                requirement.c.operator,
                requirement.c.extraction_confidence,
                requirement.c.mandatory,
                requirement.c.source_table_id,
                requirement.c.source_row_id,
                requirement.c.prompt_version,
                requirement.c.source_span,
                requirement.c.normalization,
            )
            .where(requirement.c.tender_id == tender_id)
            .where(requirement.c.superseded_at.is_(None) if requirement_ids is None else requirement.c.id.in_(requirement_ids))
        ).all()
        return [
            {
                "id": str(row[0]),
                "item_id": row[1],
                "text": row[2],
                "source_text": row[3],
                "page": row[4],
                # What the row came from and how it was read, so a stage that
                # reuses the stored rows can write them back unchanged (D-93).
                "source_section": row[5],
                "extraction_method": row[6],
                "type": row[7],
                "attribute": row[8],
                "review_required": row[9],
                "review_reason": row[10],
                "value": row[11],
                "unit": row[12],
                "operator": row[13],
                "confidence": row[14],
                "mandatory": bool(row[15]) if row[15] is not None else True,
                "source_table_id": str(row[16]) if row[16] else None,
                "source_row_id": row[17],
                "prompt_version": row[18],
                "source_span": tuple(row[19]) if row[19] is not None else None,
                "normalization": row[20],
            }
            for row in rows
        ]

    # --- the page ledger ---

    def store_ocr_table(self, tender_id, identifier, page, attachment_id, context):
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        self.session.execute(pg_insert(tender_table).values(
            id=identifier, tender_id=tender_id, headers=context['headers'], rows=context['rows'],
            page_range={'page': page, 'attachment_id': attachment_id, 'ocr': context['reading']}
        ).on_conflict_do_nothing(index_elements=['id']))

    def list_source_tables(self, tender_id):
        rows = self.session.execute(select(tender_table.c.id, tender_table.c.page_range,
                                            tender_table.c.headers, tender_table.c.rows)
                                    .where(tender_table.c.tender_id == tender_id)).all()
        return [{'id': str(row[0]), 'source': row[1], 'headers': row[2], 'rows': row[3]} for row in rows]

    def replace_pages(self, tender_id: str, pages: list[dict]) -> int:
        """The tender's page ledger, replacing what was there.

        Every page ends in a state. A page that could not be read is
        `failed`, never counted as processed (FR-T2).
        """
        self.session.execute(delete(tender_page).where(tender_page.c.tender_id == tender_id))
        if not pages:
            return 0
        self.session.execute(
            insert(tender_page),
            [
                {
                    "tender_id": tender_id,
                    "page": page["page"],
                    "source_key": page.get('source_key') or page.get('attachment_id') or 'legacy',
                    "tags": page.get("tags", []),
                    "extraction_method": page.get("extraction_method"),
                    "quality_warnings": page.get("quality_warnings", []),
                    "ledger_status": page["ledger_status"],
                    "skip_reason": page.get("skip_reason"),
                    "attachment_id": page.get("attachment_id"),
                }
                for page in pages
            ],
        )
        return len(pages)

    def list_pages(self, tender_id: str) -> list[dict]:
        rows = self.session.execute(
            select(
                tender_page.c.page,
                tender_page.c.ledger_status,
                tender_page.c.skip_reason,
                tender_page.c.attachment_id,
                tender_page.c.source_key,
            )
            .where(tender_page.c.tender_id == tender_id)
            .order_by(tender_page.c.source_key, tender_page.c.page)
        ).all()
        return [
            {
                "page": row[0],
                "ledger_status": row[1],
                "skip_reason": row[2],
                "attachment_id": str(row[3]) if row[3] else None,
                "source_key": row[4],
            }
            for row in rows
        ]

    # --- the stage checkpoints ---

    def get_stage(self, tender_id: str, section: str) -> Optional[dict]:
        """The checkpoint of a stage, if it has run."""
        row = self.session.execute(
            select(
                extraction_batch.c.status,
                extraction_batch.c.output_hash,
                extraction_batch.c.attempts,
                extraction_batch.c.extractor_version,
                extraction_batch.c.prompt_version,
                extraction_batch.c.detail,
            )
            .where(
                extraction_batch.c.tender_id == tender_id,
                extraction_batch.c.section == section,
            )
        ).first()
        if row is None:
            return None
        return {
            "status": row[0],
            "key": row[1],
            "attempts": row[2],
            "extractor_version": row[3],
            "prompt_version": row[4],
            "detail": row[5],
        }

    def stage_keys(self, tender_id: str) -> dict[str, str]:
        """The key of every stage that has run for this tender."""
        rows = self.session.execute(
            select(extraction_batch.c.section, extraction_batch.c.output_hash).where(
                extraction_batch.c.tender_id == tender_id
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    def stage_statuses(self, tender_id: str) -> dict[str, str]:
        """The state every stage was left in.

        The key says what the stage read; the state says whether it finished.
        A stage that stopped at the provider's limit has the same key next
        time, so the state is what tells a resumed run to try it again (D15).
        """
        rows = self.session.execute(
            select(extraction_batch.c.section, extraction_batch.c.status).where(
                extraction_batch.c.tender_id == tender_id
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    def save_stage(
        self,
        tender_id: str,
        section: str,
        *,
        status: str,
        key: str,
        attempts: int = 0,
        extractor_version: Optional[str] = None,
        prompt_version: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        """Record what a stage did, so the next run knows what to skip."""
        existing = self.session.execute(
            select(extraction_batch.c.id).where(
                extraction_batch.c.tender_id == tender_id,
                extraction_batch.c.section == section,
            )
        ).first()
        if existing:
            self.session.execute(
                update(extraction_batch)
                .where(extraction_batch.c.id == existing[0])
                .values(
                    status=status,
                    output_hash=key,
                    attempts=attempts,
                    extractor_version=extractor_version,
                    prompt_version=prompt_version,
                    detail=detail,
                )
            )
            return
        self.session.execute(
            insert(extraction_batch).values(
                id=uuid.uuid4(),
                tender_id=tender_id,
                section=section,
                status=status,
                output_hash=key,
                attempts=attempts,
                extractor_version=extractor_version,
                prompt_version=prompt_version,
                detail=detail,
            )
        )


class LLMCache:
    """The cached answers of the LLM.

    The key is the hash of the input plus the model, the prompt and the
    schema that read it, so an answer is reused only when every one of those
    is the same.
    """

    def __init__(self, session: Session):
        self.session = session

    def get(
        self, input_hash: str, model: str, prompt_version: str, schema_version: str
    ) -> Optional[Any]:
        row = self.session.execute(
            select(llm_cache.c.output).where(
                llm_cache.c.input_hash == input_hash,
                llm_cache.c.model == model,
                llm_cache.c.prompt_version == prompt_version,
                llm_cache.c.schema_version == schema_version,
            )
        ).first()
        return row[0] if row else None

    def put(
        self,
        input_hash: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        output: Any,
    ) -> None:
        """Store an answer, replacing the one with the same key."""
        existing = self.session.execute(
            select(llm_cache.c.id).where(
                llm_cache.c.input_hash == input_hash,
                llm_cache.c.model == model,
                llm_cache.c.prompt_version == prompt_version,
                llm_cache.c.schema_version == schema_version,
            )
        ).first()
        if existing:
            self.session.execute(
                update(llm_cache)
                .where(llm_cache.c.id == existing[0])
                .values(output=output, created_at=datetime.now(timezone.utc))
            )
            return
        self.session.execute(
            insert(llm_cache).values(
                id=uuid.uuid4(),
                input_hash=input_hash,
                model=model,
                prompt_version=prompt_version,
                schema_version=schema_version,
                output=output,
            )
        )
