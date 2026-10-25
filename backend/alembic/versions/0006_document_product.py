"""the document points at the product it is about

Revision ID: 0006_document_product
Revises: 0005_product_family_level
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_document_product"
down_revision = "0005_product_family_level"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The corpus seeding fills this in: a document names a model, or it does
    # not and a family-level product is made for it. The column is nullable
    # because a document that names neither has no product at all.
    op.add_column(
        "document",
        sa.Column("product_id", sa.Uuid(), sa.ForeignKey("product.id"), nullable=True),
    )
    op.create_index("ix_document_product_id", "document", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_document_product_id", table_name="document")
    op.drop_column("document", "product_id")
