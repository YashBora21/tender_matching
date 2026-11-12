"""Reading attempts, and provenance on the shared LLM cache (Track B, step 5).

The history of a tender's readings, each with the limits it ran under, and an
optional tender/reading attribution on cache rows so a reset can tell a
tender-derived entry from a legacy one it must retain.

Depends on the spend-ledger schema so reset can detach billing links without erasing history.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023_reading_attempts"
down_revision = "0022_llm_spend"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reading_attempt",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tender_id",
            sa.Uuid(),
            sa.ForeignKey("tender.id"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("stop_reason", sa.Text()),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("filename", sa.String(512)),
        sa.Column("limits", JSONB(), nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_reading_attempt_tender_id", "reading_attempt", ["tender_id"]
    )
    op.create_unique_constraint(
        "uq_reading_attempt_tender_no",
        "reading_attempt",
        ["tender_id", "attempt_no"],
    )
    op.add_column("llm_cache", sa.Column("provenance", JSONB(), nullable=True))


def downgrade():
    op.drop_column("llm_cache", "provenance")
    op.drop_constraint(
        "uq_reading_attempt_tender_no", "reading_attempt", type_="unique"
    )
    op.drop_index("ix_reading_attempt_tender_id", table_name="reading_attempt")
    op.drop_table("reading_attempt")
