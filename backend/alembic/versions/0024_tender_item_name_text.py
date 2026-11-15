"""Let a BOQ item name be as long as the line it came from."""
from alembic import op
import sqlalchemy as sa

revision = "0024_tender_item_name_text"
down_revision = "0023_reading_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("tender_item", "name", existing_type=sa.String(255), type_=sa.Text())


def downgrade():
    raise RuntimeError("Item names may exceed 255 chars; narrowing would truncate evidence.")