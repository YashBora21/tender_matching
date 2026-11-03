"""a technical clause that parsed no attribute can say so

Revision ID: 0016_requirement_not_parsed
Revises: 0015_product_type_doc_role
Create Date: 2026-11-03
"""

from __future__ import annotations

from alembic import op

revision = "0016_requirement_not_parsed"
down_revision = "0015_product_type_doc_role"
branch_labels = None
depends_on = None

# The same list `app.schemas.enums.ReasonCode` holds, in the same order, under
# the same constraint name `app.repositories.schema` declares. 0015 is the
# revision that was head when this one was written; if it does not land, change
# `down_revision` to `0014_merge_bharat_identity`.
_CURRENT = (
    "reason_code IN ('attribute_not_mapped', 'value_missing', "
    "'grade_not_recognised', 'unit_not_comparable', "
    "'operator_not_comparable', 'category_mismatch', "
    "'requirement_not_parsed')"
)
_PREVIOUS = (
    "reason_code IN ('attribute_not_mapped', 'value_missing', "
    "'grade_not_recognised', 'unit_not_comparable', "
    "'operator_not_comparable', 'category_mismatch')"
)


def _replace(check: str) -> None:
    op.drop_constraint("reason_code", "result", type_="check")
    op.create_check_constraint("reason_code", "result", check)


def upgrade() -> None:
    # A clause the reader never turned into an attribute has no comparison to
    # run. It keeps the status *no evidence found* - the product was never
    # checked - and the code says the clause, not the corpus, is why.
    _replace(_CURRENT)


def downgrade() -> None:
    _replace(_PREVIOUS)
