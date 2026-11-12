"""Persist model spend so a day and month cap survives a restart (D-164)."""
from alembic import op
import sqlalchemy as sa

revision = "0022_llm_spend"
down_revision = "0021_requirement_history"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "llm_spend",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False, server_default="text"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_inr", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("run.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_llm_spend_occurred_at", "llm_spend", ["occurred_at"])


def downgrade():
    op.drop_index("ix_llm_spend_occurred_at", table_name="llm_spend")
    op.drop_table("llm_spend")
