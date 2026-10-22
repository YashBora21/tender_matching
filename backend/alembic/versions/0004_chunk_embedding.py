"""the chunk embedding gets a dimension and an index

Revision ID: 0004_chunk_embedding
Revises: 0003_tender_pipeline
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0004_chunk_embedding"
down_revision = "0003_tender_pipeline"
branch_labels = None
depends_on = None

# The dimension of the embedding model's vectors. It is a property of the
# model, so it is written here rather than read from the environment: a
# migration that depended on the environment would not be the same migration
# on every machine (D-6).
#
# shortcut: the dimension is fixed here. Changing the embedding model means
# a new migration and a re-embedding of the corpus.
EMBEDDING_DIMENSION = 768


def upgrade() -> None:
    op.alter_column(
        "chunk",
        "embedding",
        type_=Vector(EMBEDDING_DIMENSION),
        existing_type=Vector(),
    )
    # The index is written as SQL: pgvector's operator class goes on the
    # column, and the dialect's own form puts it in a WITH clause that
    # PostgreSQL reads as a parameter.
    op.execute(
        "CREATE INDEX ix_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunk_embedding")
    op.alter_column(
        "chunk",
        "embedding",
        type_=Vector(),
        existing_type=Vector(EMBEDDING_DIMENSION),
    )
