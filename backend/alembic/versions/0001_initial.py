"""initial schema: PRD 7 tables + PRD 5.3.1 additions + attribute

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pgvector must exist before any vector column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "manufacturer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("official_domain", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "product",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("family", sa.String(length=255), nullable=True),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["manufacturer_id"], ["manufacturer.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manufacturer_id", "model", name="uq_product_manufacturer_model"),
    )
    op.create_index("ix_product_manufacturer_id", "product", ["manufacturer_id"])

    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("doc_type", sa.String(length=64), nullable=True),
        sa.Column("version_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("is_secondary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("restriction_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["manufacturer_id"], ["manufacturer.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_sha256", "document", ["sha256"])
    op.create_index("ix_document_manufacturer_id", "document", ["manufacturer_id"])

    op.create_table(
        "chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(length=512), nullable=True),
        sa.Column("is_table", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ocr", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chunk_document_id", "chunk", ["document_id"])

    op.create_table(
        "attribute",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_term", sa.String(length=255), nullable=False),
        sa.Column(
            "value_type",
            sa.Enum(
                "numeric", "range", "categorical", "boolean", "certification", "material",
                name="value_type", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column("unit_default", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_term"),
    )

    op.create_table(
        "fact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("attribute_id", sa.Uuid(), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column(
            "value_type",
            sa.Enum(
                "numeric", "range", "categorical", "boolean", "certification", "material",
                name="fact_value_type", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column(
            "operator",
            sa.Enum(
                "eq", "ne", "gt", "gte", "lt", "lte", "between", "approx", "unknown",
                name="fact_operator", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column("conditions", sa.Text(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column(
            "verification_status",
            sa.Enum(
                "verified", "needs_review", "rejected",
                name="fact_verification_status", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column("extractor_version", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["attribute_id"], ["attribute.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["product.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fact_product_id", "fact", ["product_id"])
    op.create_index("ix_fact_attribute_id", "fact", ["attribute_id"])

    op.create_table(
        "alias",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("canonical_term", sa.String(length=255), nullable=False),
        sa.Column("alias", sa.String(length=255), nullable=False),
        sa.Column(
            "source",
            sa.Enum("manual", "llm", name="alias_source", native_enum=False, length=64),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alias_canonical_term", "alias", ["canonical_term"])

    op.create_table(
        "tender",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("processing_version", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
    )

    op.create_table(
        "tender_item",
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("boq_ref", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("item_id"),
    )
    op.create_index("ix_tender_item_tender_id", "tender_item", ["tender_id"])

    op.create_table(
        "tender_page",
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column(
            "extraction_method",
            sa.Enum("table", "prose", "ocr", name="page_extraction_method", native_enum=False, length=64),
            nullable=True,
        ),
        sa.Column("quality_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column(
            "ledger_status",
            sa.Enum("parsed", "skipped", "ocr", "failed", name="ledger_status", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("tender_id", "page"),
    )

    op.create_table(
        "tender_table",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("page_range", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rows", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("continued_from", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["continued_from"], ["tender_table.id"]),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tender_table_tender_id", "tender_table", ["tender_id"])

    op.create_table(
        "requirement",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=True),
        sa.Column("parent_requirement_id", sa.Uuid(), nullable=True),
        sa.Column("source_table_id", sa.Uuid(), nullable=True),
        sa.Column("source_row_id", sa.String(length=128), nullable=True),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("source_section", sa.String(length=512), nullable=True),
        sa.Column("parent_clause", sa.String(length=512), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_span", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "extraction_method",
            sa.Enum("table", "prose", "ocr", name="requirement_extraction_method", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "technical", "certification", "commercial", "eligibility", "informational", "unknown",
                name="requirement_type", native_enum=False, length=64,
            ),
            nullable=False,
        ),
        sa.Column("attribute", sa.String(length=255), nullable=True),
        sa.Column(
            "operator",
            sa.Enum(
                "eq", "ne", "gt", "gte", "lt", "lte", "between", "approx", "unknown",
                name="requirement_operator", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("unit", sa.String(length=64), nullable=True),
        sa.Column(
            "value_type",
            sa.Enum(
                "numeric", "range", "categorical", "boolean", "certification", "material",
                name="requirement_value_type", native_enum=False, length=64,
            ),
            nullable=True,
        ),
        sa.Column("mandatory", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("extraction_confidence", sa.Float(), nullable=True),
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("extraction_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["tender_item.item_id"]),
        sa.ForeignKeyConstraint(["parent_requirement_id"], ["requirement.id"]),
        sa.ForeignKeyConstraint(["source_table_id"], ["tender_table.id"]),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requirement_tender_id", "requirement", ["tender_id"])
    op.create_index("ix_requirement_item_id", "requirement", ["item_id"])

    op.create_table(
        "extraction_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("section", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extractor_version", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extraction_batch_tender_id", "extraction_batch", ["tender_id"])

    op.create_table(
        "run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "queued", "parsing", "retrieving", "verifying", "scoring", "done", "failed",
                name="run_status", native_enum=False, length=64,
            ),
            nullable=False,
        ),
        sa.Column("config_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("corpus_hash", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_tender_id", "run", ["tender_id"])

    op.create_table(
        "result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("manufacturer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "full_match", "partial_match", "missing_capability",
                "unsupported_requirement", "no_evidence_found",
                name="result_status", native_enum=False, length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "verification_outcome",
            sa.Enum(
                "passed", "failed", "family_level", "conflicting", "not_checked",
                name="verification_outcome", native_enum=False, length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "applicability",
            sa.Enum("model_level", "family_level", "unknown", name="applicability", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("capability_text", sa.Text(), nullable=True),
        sa.Column("evidence_ref", sa.Uuid(), nullable=True),
        sa.Column(
            "confidence",
            sa.Enum("high", "medium", "low", name="confidence_level", native_enum=False, length=64),
            nullable=True,
        ),
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["evidence_ref"], ["fact.id"]),
        sa.ForeignKeyConstraint(["manufacturer_id"], ["manufacturer.id"]),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirement.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_result_run_id", "result", ["run_id"])
    op.create_index("ix_result_requirement_id", "result", ["requirement_id"])


def downgrade() -> None:
    op.drop_table("result")
    op.drop_table("run")
    op.drop_table("extraction_batch")
    op.drop_table("requirement")
    op.drop_table("tender_table")
    op.drop_table("tender_page")
    op.drop_table("tender_item")
    op.drop_table("tender")
    op.drop_table("alias")
    op.drop_table("fact")
    op.drop_table("attribute")
    op.drop_table("chunk")
    op.drop_table("document")
    op.drop_table("product")
    op.drop_table("manufacturer")
