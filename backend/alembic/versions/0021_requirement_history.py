"""Preserve requirement revisions and their normalization beside raw quotes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_requirement_history"
down_revision = "0020_corpus_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("requirement", sa.Column("normalization", JSONB()))
    op.add_column("requirement", sa.Column("superseded_at", sa.DateTime(timezone=True)))
    op.add_column("tender_item", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index("ix_requirement_active_tender", "requirement", ["tender_id"], postgresql_where=sa.text("superseded_at IS NULL"))


def downgrade():
    raise RuntimeError("Requirement history must be retained; downgrade requires an explicit archival plan.")
