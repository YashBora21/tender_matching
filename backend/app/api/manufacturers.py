"""Manufacturer routes (PRD 11): what the corpus covers, per manufacturer.

The answer is read from the documents themselves: a manufacturer covers the
categories its documents declare, and the counts are the documents, products and
chunks actually stored. A manifest that lists a catalogue the ingestion never
read is therefore not coverage - the numbers come from what the system holds.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.repositories.corpus import corpus_hash, list_manufacturers

router = APIRouter(prefix="/api/manufacturers", tags=["manufacturers"])


@router.get("")
def get_manufacturers(session: Session = Depends(get_session)) -> dict:
    """Every manufacturer in the corpus, with what it covers.

    An empty corpus is an empty list, not an error: it says nothing has been
    ingested, which is a state a caller can act on.
    """
    rows = list_manufacturers(session)
    return {
        "count": len(rows),
        "totals": {
            key: sum(row[key] for row in rows)
            for key in ("documents", "products", "chunks")
        },
        "categories": sorted({name for row in rows for name in row["categories"]}),
        # The hash identifies the corpus these numbers describe (FR-34).
        "corpus_hash": corpus_hash(session),
        "manufacturers": rows,
    }
