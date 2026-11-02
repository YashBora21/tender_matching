"""a run records what its stages cost

Revision ID: 0012_run_metrics
Revises: 0011_result_reason_code
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_run_metrics"
down_revision = "0011_result_reason_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Where the wall clock went, per stage, and what the run's client spent.
    # Not in `config_snapshot`: that is compared to decide whether a stopped
    # run can be continued, and a measurement would make every attempt look
    # like a different configuration (D-130, D-131).
    op.add_column(
        "run",
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        "COMMENT ON COLUMN run.metrics IS "
        "'Seconds per stage and the LLM counts this run spent (D-131)'"
    )


def downgrade() -> None:
    op.drop_column("run", "metrics")
