"""tender pipeline: attachments, item keys, the LLM cache, the page ledger

Revision ID: 0003_tender_pipeline
Revises: 0002_document_ingestion
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_tender_pipeline"
down_revision = "0002_document_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The documents a tender links to.
    op.create_table(
        "tender_attachment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tender_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tender_id"], ["tender.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tender_id", "url", name="uq_tender_attachment_tender_url"),
    )
    op.create_index(
        "ix_tender_attachment_tender_id", "tender_attachment", ["tender_id"]
    )

    # A cached LLM answer, keyed by the input and everything that read it.
    op.create_table(
        "llm_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "input_hash",
            "model",
            "prompt_version",
            "schema_version",
            name="uq_llm_cache_key",
        ),
    )
    op.create_index("ix_llm_cache_input_hash", "llm_cache", ["input_hash"])

    # An item is numbered within its tender, so the key is the pair.
    op.drop_constraint("requirement_item_id_fkey", "requirement", type_="foreignkey")
    op.drop_constraint("tender_item_pkey", "tender_item", type_="primary")
    op.create_primary_key("tender_item_pkey", "tender_item", ["tender_id", "item_id"])
    op.create_foreign_key(
        "requirement_item_id_fkey",
        "requirement",
        "tender_item",
        ["tender_id", "item_id"],
        ["tender_id", "item_id"],
    )

    # What read a requirement, and what it read.
    op.add_column("requirement", sa.Column("model", sa.String(length=128), nullable=True))
    op.add_column(
        "requirement", sa.Column("prompt_version", sa.String(length=64), nullable=True)
    )
    op.add_column("requirement", sa.Column("input_hash", sa.String(length=64), nullable=True))

    # A page names the attachment it came from.
    op.add_column(
        "tender_page", sa.Column("attachment_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "tender_page_attachment_id_fkey",
        "tender_page",
        "tender_attachment",
        ["attachment_id"],
        ["id"],
    )
    op.create_index("ix_tender_page_tender_id", "tender_page", ["tender_id"])

    # One checkpoint per stage per tender: the state a resumed run reads.
    op.create_unique_constraint(
        "uq_extraction_batch_tender_section", "extraction_batch", ["tender_id", "section"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_extraction_batch_tender_section", "extraction_batch", type_="unique"
    )
    op.drop_index("ix_tender_page_tender_id", table_name="tender_page")
    op.drop_constraint("tender_page_attachment_id_fkey", "tender_page", type_="foreignkey")
    op.drop_column("tender_page", "attachment_id")
    op.drop_column("requirement", "input_hash")
    op.drop_column("requirement", "prompt_version")
    op.drop_column("requirement", "model")
    op.drop_constraint("requirement_item_id_fkey", "requirement", type_="foreignkey")
    op.drop_constraint("tender_item_pkey", "tender_item", type_="primary")
    op.create_primary_key("tender_item_pkey", "tender_item", ["item_id"])
    op.create_foreign_key(
        "requirement_item_id_fkey", "requirement", "tender_item", ["item_id"], ["item_id"]
    )
    op.drop_index("ix_llm_cache_input_hash", table_name="llm_cache")
    op.drop_table("llm_cache")
    op.drop_index("ix_tender_attachment_tender_id", table_name="tender_attachment")
    op.drop_table("tender_attachment")
