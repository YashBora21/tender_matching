"""an extraction stage records why it stopped

Revision ID: 0010_stage_detail
Revises: 0009_embedding_384
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_stage_detail"
down_revision = "0009_embedding_384"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extraction_batch", sa.Column("detail", sa.Text(), nullable=True))
    # The stage that stopped at the provider's limit is the one whose reason
    # matters, and its row already exists; the column is filled by the next
    # run. Nothing is rewritten here: an old row has no reason to have one.
    op.execute(
        "COMMENT ON COLUMN extraction_batch.detail IS "
        "'Why the stage stopped, when it did not finish (D9)'"
    )


def downgrade() -> None:
    op.drop_column("extraction_batch", "detail")
