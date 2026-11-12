"""Database schema, expressed once as SQLAlchemy Core tables.

This module is the single source of truth for the DDL: the Alembic migration
imports `metadata` and creates the tables from it, so a column cannot drift between
the app and the database.

Table set = PRD 7 (manufacturer, product, document, chunk, fact, alias, tender,
requirement, run, result) + PRD 5.3.1 additions (tender_page, tender_table,
tender_item, extraction_batch) + `attribute`, which closes the `fact.attribute_id`
reference the PRD leaves dangling.
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from app.schemas.enums import (
    AliasSource,
    Applicability,
    ClassificationSource,
    ConfidenceLevel,
    DocRole,
    ExtractionMethod,
    FactVerificationStatus,
    LedgerStatus,
    Operator,
    ReasonCode,
    RequirementStatus,
    RequirementType,
    RunStatus,
    TenderReasonCode,
    TenderStatus,
    ValueType,
    VerificationOutcome,
)

metadata = MetaData()


def _enum(enum_cls, name: str):
    """VARCHAR + CHECK constraint rather than a native PG enum.

    Native enums need ALTER TYPE to add a value; a CHECK constraint needs a
    migration that drops and recreates it, which is simpler to review.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=64,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
    )


def _uuid_pk():
    return Column("id", Uuid(), primary_key=True, default=uuid.uuid4)


# --- corpus -----------------------------------------------------------------

manufacturer = Table(
    "manufacturer",
    metadata,
    _uuid_pk(),
    Column("name", String(255), nullable=False, unique=True),
    Column("official_domain", String(255)),
    Column("notes", Text),
)

product = Table(
    "product",
    metadata,
    _uuid_pk(),
    Column("manufacturer_id", Uuid(), ForeignKey("manufacturer.id"), nullable=False),
    Column("model", String(255), nullable=False),
    Column("family", String(255)),
    Column("category", String(255)),
    # True when the product stands for a family of products rather than one
    # model. A fact about a family is family-level evidence: uncertain
    # applicability, capped and flagged for review (PRD 4).
    Column("is_family_level", Boolean, nullable=False, server_default="false"),
    Column("product_type", String(64)),
    Column("classification_source", _enum(ClassificationSource, "classification_source")),
    Column("confidence", Float),
    UniqueConstraint("manufacturer_id", "model", name="uq_product_manufacturer_model"),
    # The table cannot be referenced by name while it is being defined.
    Index("ix_product_manufacturer_id", "manufacturer_id"),
)

document = Table(
    "document",
    metadata,
    _uuid_pk(),
    Column("manufacturer_id", Uuid(), ForeignKey("manufacturer.id"), nullable=False),
    Column("url", Text),
    Column("sha256", String(64), nullable=False),
    Column("doc_type", String(64)),
    Column("doc_role", _enum(DocRole, "doc_role")),
    Column("version_date", DateTime(timezone=True)),
    Column("retrieved_at", DateTime(timezone=True)),
    Column("path", Text),
    # Distributor / reseller documents are marked secondary and never override
    # manufacturer documents (FR-7).
    Column("is_secondary", Boolean, nullable=False, server_default="false"),
    Column("restriction_note", Text),
    # The model or family the document covers, and the category it sits in.
    # FR-10: a chunk carries the model/family, and FR-17 filters retrieval by
    # category and model, so both live on the document the chunks belong to.
    Column("model_or_family", String(255)),
    Column("category", String(255)),
    # The product the document is about. The corpus seeding writes it: a
    # document is evidence about one product, and the retrieval joins the
    # document to that product rather than to every product of the
    # manufacturer.
    Column("product_id", Uuid(), ForeignKey("product.id")),
    Index("ix_document_sha256", "sha256"),
    Index("ix_document_manufacturer_id", "manufacturer_id"),
    Index("ix_document_product_id", "product_id"),
    # One document per manufacturer per content: a distributor reselling the
    # manufacturer's own file is the same document, not a second one.
    UniqueConstraint(
        "manufacturer_id", "sha256", name="uq_document_manufacturer_sha256"
    ),
)

chunk = Table(
    "chunk",
    metadata,
    _uuid_pk(),
    Column("document_id", Uuid(), ForeignKey("document.id"), nullable=False),
    Column("page", Integer),
    Column("section", String(512)),
    Column("is_table", Boolean, nullable=False, server_default="false"),
    Column("ocr", Boolean, nullable=False, server_default="false"),
    Column("text", Text, nullable=False),
    # FR-9: OCR text can be wrong, so the chunk carries the lower confidence
    # it was read with.
    Column("confidence", Float, nullable=False, server_default="1.0"),
    # FR-11: keyword search. The column is generated from the text, so it can
    # never drift from it. The vector column has the dimension of the chosen
    # model; the ANN index is created by migration 0009 (D-6).
    Column(
        "search_vector",
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    ),
    # The dimension is the one the migrated column has: an embedding from a
    # different model is a different vector space, not a wider one (D-6, 0009).
    Column("embedding", Vector(384)),
    Index("ix_chunk_document_id", "document_id"),
    Index("ix_chunk_search_vector", "search_vector", postgresql_using="gin"),
)

attribute = Table(
    "attribute",
    metadata,
    _uuid_pk(),
    Column("canonical_term", String(255), nullable=False, unique=True),
    Column("value_type", _enum(ValueType, "value_type")),
    Column("unit_default", String(64)),
    Column("notes", Text),
)

fact = Table(
    "fact",
    metadata,
    _uuid_pk(),
    Column("product_id", Uuid(), ForeignKey("product.id"), nullable=False),
    Column("attribute_id", Uuid(), ForeignKey("attribute.id")),
    Column("original_name", String(255)),
    Column("value", Text),
    Column("unit", String(64)),
    Column("value_type", _enum(ValueType, "fact_value_type")),
    Column("operator", _enum(Operator, "fact_operator")),
    Column("conditions", Text),
    Column("source_context", JSONB),
    Column("document_id", Uuid(), ForeignKey("document.id"), nullable=False),
    Column("page", Integer),
    Column("quote", Text),
    Column("verification_status", _enum(FactVerificationStatus, "fact_verification_status")),
    Column("extractor_version", String(64)),
    Index("ix_fact_product_id", "product_id"),
    Index("ix_fact_attribute_id", "attribute_id"),
)

alias = Table(
    "alias",
    metadata,
    _uuid_pk(),
    Column("canonical_term", String(255), nullable=False),
    Column("alias", String(255), nullable=False),
    Column("source", _enum(AliasSource, "alias_source")),
    Column("confidence", Float),
    Column("approved", Boolean, nullable=False, server_default="false"),
    Index("ix_alias_canonical_term", "canonical_term"),
)

# --- tender -----------------------------------------------------------------

tender = Table(
    "tender",
    metadata,
    _uuid_pk(),
    Column("filename", String(512), nullable=False),
    # Category is inferred per BOQ item, not once per tender (FR-16), so this is
    # a coarse hint only and stays nullable.
    Column("category", String(255)),
    Column("uploaded_at", DateTime(timezone=True), server_default=func.now()),
    # FR-12: idempotent ingestion keys on hash + extractor/prompt/schema versions.
    Column("sha256", String(64), nullable=False, unique=True),
    Column("page_count", Integer),
    Column("processing_version", String(64)),
    # The state of the tender's own ingestion. A tender is read when it is
    # uploaded, not when a run asks for it, so the caller needs a state to
    # poll, and a failure needs somewhere to say why (PRD 11).
    Column("status", _enum(TenderStatus, "tender_status"), nullable=False),
    Column("error", Text),
    # Why this attempt stopped short, when it did (D-156). NULL is "the read
    # finished": an empty requirement list is not the same answer as a
    # specification nobody could find or read.
    Column("reason_code", _enum(TenderReasonCode, "tender_reason_code")),
)

tender_item = Table(
    "tender_item",
    metadata,
    # The composite key: an item is numbered within its tender, and two
    # tenders both have an item "1".
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False, primary_key=True),
    Column("item_id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("category", String(255)),
    Column("boq_ref", String(64)),
    Column("active", Boolean, nullable=False, server_default="true"),
    Index("ix_tender_item_tender_id", "tender_id"),
)

# The documents a tender links to. A tender PDF is a wrapper: it names its
# bill of quantities and its specification and embeds neither.
tender_attachment = Table(
    "tender_attachment",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    Column("url", Text, nullable=False),
    Column("filename", String(512)),
    Column("path", Text),
    Column("sha256", String(64)),
    # The role the document plays: boq-csv, boq-pdf, spec, form, scanned.
    Column("kind", String(64)),
    # The outcome: new, unchanged, changed, failed, restricted, manual.
    Column("status", String(64), nullable=False),
    Column("note", Text),
    Column("bytes", Integer),
    Column("pages", Integer),
    Column("retrieved_at", DateTime(timezone=True)),
    Index("ix_tender_attachment_tender_id", "tender_id"),
    UniqueConstraint("tender_id", "url", name="uq_tender_attachment_tender_url"),
)

tender_page = Table(
    "tender_page",
    metadata,
    Column("tender_id", Uuid(), ForeignKey("tender.id"), primary_key=True),
    Column("source_key", String(64), primary_key=True, nullable=False, server_default="legacy"),
    Column("page", Integer, primary_key=True),
    Column("tags", JSONB, nullable=False, server_default="[]"),
    Column("extraction_method", _enum(ExtractionMethod, "page_extraction_method")),
    Column("quality_warnings", JSONB, nullable=False, server_default="[]"),
    Column("ledger_status", _enum(LedgerStatus, "ledger_status"), nullable=False),
    Column("skip_reason", Text),
    # The attachment the page came from. A tender is a wrapper: its pages
    # live in the documents it links to, and a page without one cannot be
    # said to have come from anywhere.
    Column("attachment_id", Uuid(), ForeignKey("tender_attachment.id")),
    Index("ix_tender_page_tender_id", "tender_id"),
)

tender_table = Table(
    "tender_table",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    Column("page_range", JSONB),
    Column("headers", JSONB),
    Column("rows", JSONB),
    Column("continued_from", Uuid(), ForeignKey("tender_table.id")),
    Index("ix_tender_table_tender_id", "tender_id"),
)

requirement = Table(
    "requirement",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    # The item is identified by the tender it belongs to and its number
    # within that tender.
    Column("item_id", String(64)),
    Column("parent_requirement_id", Uuid(), ForeignKey("requirement.id")),
    Column("source_table_id", Uuid(), ForeignKey("tender_table.id")),
    Column("source_row_id", String(128)),
    Column("source_page", Integer, nullable=False),
    Column("source_section", String(512)),
    Column("parent_clause", String(512)),
    Column("source_text", Text, nullable=False),
    Column("source_span", JSONB),
    Column("normalization", JSONB),
    Column("superseded_at", DateTime(timezone=True)),
    Column("extraction_method", _enum(ExtractionMethod, "requirement_extraction_method"), nullable=False),
    Column("text", Text),
    Column("type", _enum(RequirementType, "requirement_type"), nullable=False),
    Column("attribute", String(255)),
    Column("operator", _enum(Operator, "requirement_operator")),
    Column("value", Text),
    Column("unit", String(64)),
    Column("value_type", _enum(ValueType, "requirement_value_type")),
    Column("mandatory", Boolean, nullable=False, server_default="true"),
    Column("extraction_confidence", Float),
    Column("review_required", Boolean, nullable=False, server_default="false"),
    Column("review_reason", Text),
    Column("extraction_version", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    # What read this requirement, and what it read. The hash is of the input
    # the model was given, so a cached answer is reused only when the input
    # and everything that read it are the same.
    Column("model", String(128)),
    Column("prompt_version", String(64)),
    Column("input_hash", String(64)),
    Index("ix_requirement_tender_id", "tender_id"),
    Index("ix_requirement_item_id", "item_id"),
    # The item is identified by its tender and its number within it.
    ForeignKeyConstraint(
        ["tender_id", "item_id"], ["tender_item.tender_id", "tender_item.item_id"]
    ),
)

extraction_batch = Table(
    "extraction_batch",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    Column("section", String(255)),
    Column("status", String(64), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    # Why the stage stopped, when it did not finish: the provider's limit
    # message and retry hint for `rate_limited` (D9).
    Column("detail", Text),
    Column("extractor_version", String(64)),
    Column("prompt_version", String(64)),
    Column("output_hash", String(64)),
    Index("ix_extraction_batch_tender_id", "tender_id"),
    # One row per stage per tender: the checkpoint a resumed run reads.
    UniqueConstraint("tender_id", "section", name="uq_extraction_batch_tender_section"),
)

# A cached LLM answer. The key is the content of the input plus the model,
# the prompt and the schema that read it, so a change to any of them is a
# different answer and not a stale one.
llm_cache = Table(
    "llm_cache",
    metadata,
    _uuid_pk(),
    Column("input_hash", String(64), nullable=False),
    Column("model", String(128), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("output", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_llm_cache_input_hash", "input_hash"),
    # Which tender and reading wrote the entry, when that is known. Written by
    # `LLMCache(tender_id=..., reading_id=...)`; legacy rows carry no provenance
    # and are retained and reported, never guessed to be tender-only (PLAN 4).
    # The key is unchanged: provenance is recorded, it does not select.
    Column("provenance", JSONB),
    UniqueConstraint(
        "input_hash",
        "model",
        "prompt_version",
        "schema_version",
        name="uq_llm_cache_key",
    ),
)

# --- run and results --------------------------------------------------------

run = Table(
    "run",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    Column("started_at", DateTime(timezone=True), server_default=func.now()),
    Column("status", _enum(RunStatus, "run_status"), nullable=False),
    Column("config_snapshot", JSONB),
    # What the run cost: seconds per stage and the counts its LLM client
    # reported. Kept out of `config_snapshot`, which is compared to decide
    # whether a stopped run may be continued (D-130) - a measurement in there
    # would make every second attempt look like a different configuration.
    Column("metrics", JSONB),
    Column("corpus_hash", String(64)),
    # Why the run failed, in the caller's words. A run that stopped has to be
    # able to say what stopped it without the caller reading the server log.
    Column("error", Text),
    Index("ix_run_tender_id", "tender_id"),
)

result = Table(
    "result",
    metadata,
    _uuid_pk(),
    Column("run_id", Uuid(), ForeignKey("run.id"), nullable=False),
    Column("requirement_id", Uuid(), ForeignKey("requirement.id"), nullable=False),
    Column("manufacturer_id", Uuid(), ForeignKey("manufacturer.id"), nullable=False),
    Column("status", _enum(RequirementStatus, "result_status"), nullable=False),
    Column("verification_outcome", _enum(VerificationOutcome, "verification_outcome"), nullable=False),
    Column("applicability", _enum(Applicability, "applicability"), nullable=False),
    Column("score", Float, nullable=False),
    Column("capability_text", Text),
    # Traceability: every result points at the stored fact that backs it (PRD 13).
    Column("evidence_ref", Uuid(), ForeignKey("fact.id")),
    Column("confidence", _enum(ConfidenceLevel, "confidence_level")),
    Column("review_required", Boolean, nullable=False, server_default="false"),
    Column("review_reason", Text),
    # Why no verdict was reached, when the reason is a limit of this system
    # rather than a finding about the product (D-86, D-101). NULL means the
    # status is a finding: the catalogue answered, or it was silent.
    Column("reason_code", _enum(ReasonCode, "reason_code")),
    Index("ix_result_run_id", "run_id"),
    Index("ix_result_requirement_id", "requirement_id"),
)

# --- reading attempts --------------------------------------------------------

# One row per attempt to read a tender, opened before the attempt starts and
# closed when it ends. It is the record behind "resume with the current
# limits": each row freezes the numeric limits its read ran under, so the next
# attempt is governed by the *current* configuration, never by a previous
# attempt's stop reason or old limits (user step 5). Append-only: a superseded
# reading is history, not something to overwrite.
reading_attempt = Table(
    "reading_attempt",
    metadata,
    _uuid_pk(),
    Column("tender_id", Uuid(), ForeignKey("tender.id"), nullable=False),
    # 1, 2, 3... within the tender, in the order the attempts were made.
    Column("attempt_no", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    # running | done | failed | rate_limited | incomplete — the tender's own
    # state vocabulary, plus `running` for an attempt still in flight.
    Column("status", String(32), nullable=False, server_default="running"),
    Column("stop_reason", Text),
    Column("reason_code", String(64)),
    # The source identity the attempt was given, frozen at its start.
    Column("sha256", String(64)),
    Column("filename", String(512)),
    # The effective numeric limits and versions this attempt ran under.
    Column("limits", JSONB, nullable=False, server_default="{}"),
    # What the attempt produced: calls, tokens, requirements, pages.
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Index("ix_reading_attempt_tender_id", "tender_id"),
    UniqueConstraint("tender_id", "attempt_no", name="uq_reading_attempt_tender_no"),
)

# --- spend ledger -----------------------------------------------------------

# One row per billable model call, written as the call returns. It exists so a
# cap survives a process restart: the client's own counters bound one run, and
# a shell that restarts between runs would otherwise reset the day's spend to
# zero (D-164). Cost is stored, not recomputed, because a price change must not
# rewrite history.
llm_spend = Table(
    "llm_spend",
    metadata,
    _uuid_pk(),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("model", String(128), nullable=False),
    # What the call was for: `text` extraction, `classify`, or a vision probe.
    Column("purpose", String(64), nullable=False, server_default="text"),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_inr", Numeric(12, 4), nullable=False, server_default="0"),
    Column("run_id", Uuid(), ForeignKey("run.id")),
    Index("ix_llm_spend_occurred_at", "occurred_at"),
)
