"""a dropped pair can say its category was the reason

Revision ID: 0013_reason_code_category
Revises: 0012_run_metrics
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op

revision = "0013_reason_code_category"
down_revision = "0012_run_metrics"
branch_labels = None
depends_on = None

# The same list `app.schemas.enums.ReasonCode` holds, in the same order, under
# the same constraint name `app.repositories.schema` declares (0011).
_CURRENT = (
    "reason_code IN ('attribute_not_mapped', 'value_missing', "
    "'grade_not_recognised', 'unit_not_comparable', "
    "'operator_not_comparable', 'category_mismatch')"
)
_PREVIOUS = (
    "reason_code IN ('attribute_not_mapped', 'value_missing', "
    "'grade_not_recognised', 'unit_not_comparable', "
    "'operator_not_comparable')"
)


def _replace(check: str) -> None:
    op.drop_constraint("reason_code", "result", type_="check")
    op.create_check_constraint("reason_code", "result", check)


def upgrade() -> None:
    # A result with no candidate because the category rule excluded this
    # manufacturer's documents keeps the status *no evidence found* - the
    # product was never checked - and the code says the exclusion was ours.
    _replace(_CURRENT)


def downgrade() -> None:
    _replace(_PREVIOUS)
