"""Corpus-wide reads: the manufacturers, their coverage, and the corpus hash.

These are the questions the API and the report ask about the corpus as a
whole rather than about one document: which manufacturers are on offer, what
each of them actually covers, and which corpus a run was scored against
(FR-34).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.comparison.vocabulary import normalise_token
from app.repositories.schema import chunk, document, manufacturer, product


def manufacturer_id(session: Session, name: str) -> Optional[str]:
    """The id of a manufacturer, by name. None when the corpus has no such one.

    An exact name is the answer. A name that is not there is read back through
    the identity fold: the same company is `delval_flow` in a catalogue
    directory and "Delval Flow" in a manifest, and a corpus that spells one
    manufacturer two ways hides half of it from every lookup (D-138). A fold
    that lands on more than one row is two names the corpus has not been
    joined on, so it is left unresolved rather than guessed.

    shortcut: compares every manufacturer row in Python; move the fold into
    SQL if the table outgrows a few hundred.
    """
    row = session.execute(
        select(manufacturer.c.id).where(manufacturer.c.name == name)
    ).first()
    if row:
        return str(row[0])
    token = normalise_token(name)
    if not token:
        return None
    matches = [
        str(row[0])
        for row in session.execute(
            select(manufacturer.c.id, manufacturer.c.name)
        ).all()
        if normalise_token(str(row[1])) == token
    ]
    return matches[0] if len(matches) == 1 else None


def list_manufacturers(session: Session) -> list[dict]:
    """Every manufacturer in the corpus, with what it covers.

    The product categories come from the documents: they are what the
    retrieval filters on (FR-17), so a manufacturer that covers nothing in a
    category is a manufacturer that cannot answer an item in it.
    """
    rows = session.execute(
        select(
            manufacturer.c.id,
            manufacturer.c.name,
            manufacturer.c.official_domain,
        ).order_by(manufacturer.c.name)
    ).all()

    documents = dict(
        session.execute(
            select(document.c.manufacturer_id, func.count(document.c.id)).group_by(
                document.c.manufacturer_id
            )
        ).all()
    )
    products = dict(
        session.execute(
            select(product.c.manufacturer_id, func.count(product.c.id)).group_by(
                product.c.manufacturer_id
            )
        ).all()
    )
    chunks = dict(
        session.execute(
            select(document.c.manufacturer_id, func.count(chunk.c.id))
            .select_from(chunk)
            .join(document, document.c.id == chunk.c.document_id)
            .group_by(document.c.manufacturer_id)
        ).all()
    )
    categories: dict = {}
    for manufacturer_id_value, category in session.execute(
        select(document.c.manufacturer_id, document.c.category)
        .where(document.c.category.isnot(None))
        .distinct()
    ).all():
        categories.setdefault(manufacturer_id_value, set()).add(category)

    return [
        {
            "id": str(row[0]),
            "name": row[1],
            "official_domain": row[2],
            "documents": int(documents.get(row[0], 0)),
            "products": int(products.get(row[0], 0)),
            "chunks": int(chunks.get(row[0], 0)),
            "categories": sorted(categories.get(row[0], set())),
        }
        for row in rows
    ]


def corpus_hash(session: Session) -> str:
    """A hash of the corpus a run is scored against (FR-34).

    The documents it actually searched: each manufacturer's name with the
    hash of the file. Two corpora with the same hash hold the same evidence,
    so a report can say what it was scored against without shipping the PDFs.
    The models and the policy are recorded separately, in the run's config
    snapshot: this hash is about the evidence, not about what read it.
    """
    from app.repositories.schema import manufacturer as manufacturer_table

    rows = session.execute(
        select(manufacturer_table.c.name, document.c.sha256)
        .select_from(document)
        .join(manufacturer_table, manufacturer_table.c.id == document.c.manufacturer_id)
        .order_by(manufacturer_table.c.name, document.c.sha256)
    ).all()
    if not rows:
        return ""

    import hashlib

    digest = hashlib.sha256()
    for name, sha256 in rows:
        digest.update(f"{name}\x1f{sha256}\x1e".encode("utf-8"))
    return digest.hexdigest()
