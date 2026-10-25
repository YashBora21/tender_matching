"""the tender says what state its own ingestion is in

Revision ID: 0008_tender_status
Revises: 0007_run_error
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_tender_status"
down_revision = "0007_run_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A tender is read when it is uploaded, not when a run asks for it, so a
    # caller needs a state to poll and a failure needs somewhere to say why
    # (PRD 11). The default is `done` only for the rows that already exist:
    # every tender in an existing database was ingested by the corpus runner
    # before this column existed. New rows state their status explicitly, so
    # the server default is dropped immediately after the backfill.
    op.add_column(
        "tender",
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "processing",
                "done",
                "failed",
                name="tender_status",
                native_enum=False,
                length=64,
            ),
            nullable=False,
            server_default="done",
        ),
    )
    op.alter_column("tender", "status", server_default=None)
    op.add_column("tender", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tender", "error")
    op.drop_column("tender", "status")
