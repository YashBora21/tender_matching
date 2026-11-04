"""a tender can say why its own reading stopped short

Revision ID: 0017_tender_reason_code
Revises: 0016_requirement_not_parsed
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_tender_reason_code"
down_revision = "0016_requirement_not_parsed"
branch_labels = None
depends_on = None

# The same list `app.schemas.enums.TenderReasonCode` holds, in the same order,
# under the same constraint name `app.repositories.schema` declares. A NULL is
# a read that finished: it states no reason because there is nothing to say.
_CURRENT = (
    "reason_code IN ('no_specification_found', 'specification_unreadable', "
    "'no_requirements_stated')"
)


def upgrade() -> None:
    # Three answers that used to be one: a tender with no specification, a
    # specification nobody could read, and a specification that states nothing
    # are different states, and only the last one is about the tender's
    # content (D-156).
    op.add_column("tender", sa.Column("reason_code", sa.String(length=64), nullable=True))
    op.create_check_constraint("tender_reason_code", "tender", _CURRENT)


def downgrade() -> None:
    op.drop_constraint("tender_reason_code", "tender", type_="check")
    op.drop_column("tender", "reason_code")
