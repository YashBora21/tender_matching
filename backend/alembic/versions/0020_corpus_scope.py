"""Name early abstentions without declaring missing manufacturer capability."""
from alembic import op

revision = '0020_corpus_scope'
down_revision = '0019_document_page_scope'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('reason_code', 'result', type_='check')
    op.create_check_constraint('reason_code', 'result',
        "reason_code IN ('attribute_not_mapped', 'value_missing', 'grade_not_recognised', "
        "'unit_not_comparable', 'operator_not_comparable', 'category_mismatch', "
        "'requirement_not_parsed', 'corpus_gap', 'scope_unresolved')")


def downgrade():
    raise RuntimeError('Resolve new abstention records explicitly before removing their reason codes.')
