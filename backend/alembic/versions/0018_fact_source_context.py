"""Preserve source tables, model scope and extraction context with facts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0018_fact_source_context'
down_revision = '0017_tender_reason_code'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('fact', sa.Column('source_context', JSONB(), nullable=True))


def downgrade():
    op.drop_column('fact', 'source_context')
