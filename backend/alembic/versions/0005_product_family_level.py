"""the product says whether it is a family or a model

Revision ID: 0005_product_family_level
Revises: 0004_chunk_embedding
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_product_family_level"
down_revision = "0004_chunk_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A catalogue covers a family of products rather than one model, and a
    # fact about it is family-level evidence: uncertain applicability, capped
    # and flagged for review (PRD 4). The product has to say which it is.
    op.add_column(
        "product",
        sa.Column(
            "is_family_level",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("product", "is_family_level")
