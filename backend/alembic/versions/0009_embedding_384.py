"""the embedding model is a local 384-dimension one

Revision ID: 0009_embedding_384
Revises: 0008_tender_status
Create Date: 2026-10-27
"""

from __future__ import annotations

from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0009_embedding_384"
down_revision = "0008_tender_status"
branch_labels = None
depends_on = None

# The dimension is written here, not read from the environment: a migration
# that depended on the environment would not be the same migration on every
# machine (D-6). The model it describes is BAAI/bge-small-en-v1.5, loaded
# locally through sentence-transformers.
EMBEDDING_DIMENSION = 384
PREVIOUS_DIMENSION = 768


def upgrade() -> None:
    # An HNSW index is built for one dimension, so it is dropped before the
    # column changes and rebuilt after.
    op.execute("DROP INDEX IF EXISTS ix_chunk_embedding")
    # Vectors from the old model are 768-dimensional and cannot be compared
    # with 384-dimensional ones. They are cleared rather than converted:
    # `make embed --force` re-embeds the corpus with the new model.
    op.execute("UPDATE chunk SET embedding = NULL")
    op.alter_column(
        "chunk",
        "embedding",
        type_=Vector(EMBEDDING_DIMENSION),
        existing_type=Vector(PREVIOUS_DIMENSION),
    )
    op.execute(
        "CREATE INDEX ix_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunk_embedding")
    op.execute("UPDATE chunk SET embedding = NULL")
    op.alter_column(
        "chunk",
        "embedding",
        type_=Vector(PREVIOUS_DIMENSION),
        existing_type=Vector(EMBEDDING_DIMENSION),
    )
    op.execute(
        "CREATE INDEX ix_chunk_embedding ON chunk USING hnsw (embedding vector_cosine_ops)"
    )
