"""a result records why no verdict was reached

Revision ID: 0011_result_reason_code
Revises: 0010_stage_detail
Create Date: 2026-10-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_result_reason_code"
down_revision = "0010_stage_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A result whose comparison could not run keeps the status *no evidence
    # found*, because the product was never checked. The code says which limit
    # stopped it, so "we could not evaluate this attribute" is countable apart
    # from "the catalogue is silent" (D-101). NULL is the ordinary case: the
    # status is a finding about the product.
    op.add_column(
        "result",
        sa.Column("reason_code", sa.String(length=64), nullable=True),
    )
    # The same constraint the schema metadata declares for this column, under
    # the same name, so a migrated database and a freshly created one agree.
    op.create_check_constraint(
        "reason_code",
        "result",
        "reason_code IN ('attribute_not_mapped', 'value_missing', "
        "'grade_not_recognised', 'unit_not_comparable', "
        "'operator_not_comparable')",
    )
    op.execute(
        "COMMENT ON COLUMN result.reason_code IS "
        "'Why no verdict was reached, when the limit is ours (D-101)'"
    )


def downgrade() -> None:
    op.drop_constraint("reason_code", "result", type_="check")
    op.drop_column("result", "reason_code")
