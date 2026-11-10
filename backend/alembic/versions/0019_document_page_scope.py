"""Keep wrapper and attachment page numbers in separate ledger scopes."""
from alembic import op
import sqlalchemy as sa

revision = '0019_document_page_scope'
down_revision = '0018_fact_source_context'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tender_page', sa.Column('source_key', sa.String(64), nullable=False, server_default='legacy'))
    op.execute("UPDATE tender_page SET source_key=attachment_id::text WHERE attachment_id IS NOT NULL")
    op.drop_constraint('tender_page_pkey', 'tender_page', type_='primary')
    op.create_primary_key('tender_page_pkey', 'tender_page', ['tender_id', 'source_key', 'page'])


def downgrade():
    # Downgrading would collapse distinct documents' page numbers and lose provenance.
    raise RuntimeError('Document-scoped ledgers require an explicit conflict-resolution migration to downgrade.')
