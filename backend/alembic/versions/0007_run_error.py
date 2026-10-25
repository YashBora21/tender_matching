"""the run says why it failed

Revision ID: 0007_run_error
Revises: 0006_document_product
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_run_error"
down_revision = "0006_document_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A run that stops has to say what stopped it. The status is `failed`
    # either way; the message is what the caller can act on (PRD 11).
    op.add_column("run", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "error")
