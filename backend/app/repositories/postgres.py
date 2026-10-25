"""PostgreSQL implementation of the repository protocols.

The corpus is written here and nowhere else: the collector produces files and a
manifest, this turns them into `document` and `chunk` rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.repositories.schema import chunk, document, manufacturer


class PostgresDocumentRepo:
    """The corpus, in PostgreSQL."""

    def __init__(self, session: Session):
        self.session = session

    def exists_by_sha256(self, sha256: str) -> bool:
        return (
            self.session.execute(
                select(document.c.id).where(document.c.sha256 == sha256).limit(1)
            ).first()
            is not None
        )

    def get_manufacturer_id(self, name: str) -> str:
        """The manufacturer's id, created on first sight.

        The name is unique, so a manifest written twice does not create two
        manufacturers.
        """
        found = self.session.execute(
            select(manufacturer.c.id).where(manufacturer.c.name == name)
        ).first()
        if found:
            return str(found[0])
        new_id = uuid.uuid4()
        self.session.execute(
            insert(manufacturer).values(id=new_id, name=name)
        )
        return str(new_id)

    def upsert_document(self, record: dict) -> str:
        """Write one document and return its id.

        A document whose content hash is already stored for this manufacturer
        is updated in place rather than duplicated: the corpus is a set of
        files, and the same file collected twice is one document.
        """
        manufacturer_id = self.get_manufacturer_id(record["manufacturer"])
        sha256 = record["sha256"]

        existing = self.session.execute(
            select(document.c.id).where(
                document.c.manufacturer_id == manufacturer_id,
                document.c.sha256 == sha256,
            )
        ).first()

        values = {
            "manufacturer_id": manufacturer_id,
            "url": record.get("url"),
            "sha256": sha256,
            "doc_type": record.get("doc_type"),
            "version_date": _as_datetime(record.get("version_date")),
            "retrieved_at": _as_datetime(record.get("retrieved_at")),
            "path": record.get("path"),
            "is_secondary": bool(record.get("is_secondary", False)),
            "restriction_note": record.get("restriction_note"),
            "model_or_family": record.get("model_or_family"),
            "category": record.get("category"),
        }

        if existing:
            document_id = str(existing[0])
            self.session.execute(
                update(document).where(document.c.id == document_id).values(**values)
            )
            return document_id

        document_id = uuid.uuid4()
        self.session.execute(
            insert(document).values(id=document_id, **values)
        )
        return str(document_id)

    def refresh_metadata(self, record: dict) -> bool:
        """Bring what the manifest says about an unchanged document up to date.

        The text of a document that was already ingested has not changed, so
        it is not read again. What the manifest says *about* it can change:
        a category corrected in the source config, a model named more
        precisely, a document reclassified. Those are the fields retrieval
        filters on, so leaving them stale would keep an old category in front
        of every search.

        Returns True when a row was actually updated.
        """
        manufacturer_id = self.get_manufacturer_id(record["manufacturer"])
        row = self.session.execute(
            select(
                document.c.id,
                document.c.url,
                document.c.doc_type,
                document.c.path,
                document.c.model_or_family,
                document.c.category,
            ).where(
                document.c.manufacturer_id == manufacturer_id,
                document.c.sha256 == record["sha256"],
            )
        ).first()
        if row is None:
            return False

        wanted = {
            "url": record.get("url"),
            "doc_type": record.get("doc_type"),
            "path": record.get("path"),
            "model_or_family": record.get("model_or_family"),
            "category": record.get("category"),
        }
        # A manifest that does not state a field has not said the stored value
        # is wrong. Writing its absence would erase what an earlier, more
        # complete manifest said - a catalogue CSV without a category column
        # would null the category of every document it lists.
        wanted = {
            key: value for key, value in wanted.items() if value not in (None, "")
        }
        stored = {
            "url": row[1],
            "doc_type": row[2],
            "path": row[3],
            "model_or_family": row[4],
            "category": row[5],
        }
        changed = {key: value for key, value in wanted.items() if stored[key] != value}
        if not changed:
            return False
        self.session.execute(
            update(document).where(document.c.id == row[0]).values(**changed)
        )
        return True

    def insert_chunks(self, document_id: str, chunks: list[dict]) -> int:
        """Replace a document's chunks with these ones.

        A document that changed is re-ingested whole: the old chunks describe
        content that is no longer in the file, and leaving them would put
        stale text in front of a reviewer.
        """
        self.session.execute(delete(chunk).where(chunk.c.document_id == document_id))
        if not chunks:
            return 0
        self.session.execute(
            insert(chunk).values(
                [
                    {
                        "id": uuid.uuid4(),
                        "document_id": document_id,
                        "page": item.get("page"),
                        "section": item.get("section"),
                        "is_table": bool(item.get("is_table", False)),
                        "ocr": bool(item.get("ocr", False)),
                        "text": item["text"],
                        "confidence": item.get("confidence", 1.0),
                    }
                    for item in chunks
                ]
            )
        )
        return len(chunks)


def _as_datetime(value) -> Optional[datetime]:
    """Read a date from the manifest, which stores it as text.

    The manifest's own date format is `YYYY-MM-DD`; anything else is left
    alone rather than guessed at.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
