"""the same manufacturer written two ways becomes one row

Revision ID: 0014_merge_bharat_identity
Revises: 0013_reason_code_category
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_merge_bharat_identity"
down_revision = "0013_reason_code_category"
branch_labels = None
depends_on = None

# The corpus holds one company under two spellings: the catalogue's directory
# slug and the name its own manifest states. The manifest name is kept, because
# it is what a reader of a report should see and what `data/manufacturers/` uses.
SLUG = "bharat_bijlee"
ATTESTED = "Bharat Bijlee"


def _manufacturer_id(bind, name: str):
    return bind.execute(
        sa.text("select id from manufacturer where name = :name"), {"name": name}
    ).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    slug_id = _manufacturer_id(bind, SLUG)
    kept_id = _manufacturer_id(bind, ATTESTED)
    if slug_id is None or kept_id is None:
        # One spelling, or none. A database that has never split the name has
        # nothing to join, and inventing a row to merge would be the bug this
        # migration exists to remove.
        return

    # Both tables are unique on the manufacturer, so a merge is only a move
    # when the two rows hold different files and different models. Where they
    # hold the same, one document or product has to go, and that choice names
    # the chunks, facts and results hanging off it - a human call, not a
    # migration's. `D-138` made the same refusal in the lookup.
    collisions = {
        "document": bind.execute(
            sa.text(
                "select sha256 from document where manufacturer_id in (:a, :b)"
                " group by sha256 having count(distinct manufacturer_id) > 1"
            ),
            {"a": slug_id, "b": kept_id},
        ).fetchall(),
        "product": bind.execute(
            sa.text(
                "select model from product where manufacturer_id in (:a, :b)"
                " group by model having count(distinct manufacturer_id) > 1"
            ),
            {"a": slug_id, "b": kept_id},
        ).fetchall(),
    }
    blocked = {name: [str(row[0]) for row in rows] for name, rows in collisions.items() if rows}
    if blocked:
        raise RuntimeError(
            f"{SLUG} and {ATTESTED} hold the same rows in {', '.join(blocked)}: "
            f"{blocked}. Decide which document or product survives before merging; "
            "its chunks, facts and results move with it."
        )

    for table in ("document", "product", "result"):
        bind.execute(
            sa.text(f"update {table} set manufacturer_id = :kept where manufacturer_id = :slug"),
            {"kept": kept_id, "slug": slug_id},
        )
    bind.execute(sa.text("delete from manufacturer where id = :slug"), {"slug": slug_id})


def downgrade() -> None:
    # The split is what this removed, and putting it back would mean choosing
    # which documents belonged to which half of one manufacturer. The names are
    # in `data/catalogue/manifest.csv` and `data/manufacturers/` either way.
    pass
