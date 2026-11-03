"""add product_type, classification_source, confidence to product and doc_role to document

Revision ID: 0015_product_type_doc_role
Revises: 0014_merge_bharat_identity
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_product_type_doc_role"
down_revision = "0014_merge_bharat_identity"
branch_labels = None
depends_on = None

_CLASSIFICATION_SOURCES = ("manifest", "rule", "human")
_DOC_ROLES = (
    "datasheet",
    "catalogue",
    "brochure",
    "white_paper",
    "certificate",
    "policy",
    "other",
)


def upgrade() -> None:
    # 1. Add product columns
    op.add_column("product", sa.Column("product_type", sa.String(length=64), nullable=True))
    op.add_column("product", sa.Column("classification_source", sa.String(length=32), nullable=True))
    op.add_column("product", sa.Column("confidence", sa.Float(), nullable=True))

    op.create_check_constraint(
        "ck_product_classification_source",
        "product",
        f"classification_source IS NULL OR classification_source IN {tuple(_CLASSIFICATION_SOURCES)}",
    )

    # 2. Add document doc_role column
    op.add_column("document", sa.Column("doc_role", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        "ck_document_doc_role",
        "document",
        f"doc_role IS NULL OR doc_role IN {tuple(_DOC_ROLES)}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_document_doc_role", "document", type_="check")
    op.drop_column("document", "doc_role")

    op.drop_constraint("ck_product_classification_source", "product", type_="check")
    op.drop_column("product", "confidence")
    op.drop_column("product", "classification_source")
    op.drop_column("product", "product_type")

