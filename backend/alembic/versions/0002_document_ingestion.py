"""document ingestion: chunk confidence, model/family, keyword search

Revision ID: 0002_document_ingestion
Revises: 0001_initial
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_document_ingestion"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # FR-10: a chunk carries the model/family, and FR-17 filters retrieval by
    # category and model, so both live on the document the chunks belong to.
    op.add_column("document", sa.Column("model_or_family", sa.String(length=255)))
    op.add_column("document", sa.Column("category", sa.String(length=255)))

    # One document per manufacturer per content: a distributor reselling the
    # manufacturer's own file is the same document, not a second one.
    op.create_unique_constraint(
        "uq_document_manufacturer_sha256", "document", ["manufacturer_id", "sha256"]
    )

    # FR-9: OCR text can be wrong, so the chunk carries the confidence it was
    # read with.
    op.add_column(
        "chunk",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
    )

    # FR-11: keyword search. The vector is generated from the text, so it can
    # never drift from it.
    op.add_column(
        "chunk",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('english', text)", persisted=True),
        ),
    )
    op.create_index(
        "ix_chunk_search_vector", "chunk", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_search_vector", table_name="chunk")
    op.drop_column("chunk", "search_vector")
    op.drop_column("chunk", "confidence")
    op.drop_constraint("uq_document_manufacturer_sha256", "document", type_="unique")
    op.drop_column("document", "category")
    op.drop_column("document", "model_or_family")
