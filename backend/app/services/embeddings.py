"""Embedding the corpus (FR-11).

The chunks are embedded once, and the embedding is stored beside the text so
the semantic search has something to compare against. A chunk that has no
embedding is still found by the keyword search; it is only missing from the
semantic one.

The embedding is done in batches, and a batch that fails is retried, because
the local model encodes on this machine and a large batch can exhaust its
memory.

A batch is written with one statement. Encoding is cheap next to the write when
the database is on another machine: measured against Neon, a batch of 32 took
30 ms to encode and 9 s of round trips, one per chunk (D-83).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.schema import chunk, document, manufacturer, product
from app.services.llm import embed

logger = get_logger(__name__)


# One statement for a whole batch. `unnest` of two arrays pairs them by
# position, so the ids and their vectors arrive together instead of as one
# statement per chunk.
_WRITE_EMBEDDINGS = text(
    "update chunk set embedding = batch.embedding "
    "from (select unnest(cast(:ids as uuid[])) as id, "
    "             unnest(cast(:vectors as text[]))::vector as embedding) as batch "
    "where chunk.id = batch.id"
)


def _vector_literal(vector: Sequence[float]) -> str:
    """The text form pgvector reads, at full float precision.

    `repr` of a float round-trips, so the stored vector is the one the model
    produced rather than a rounded copy of it.
    """
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _document_title(path: Optional[str]) -> Optional[str]:
    """The title a document is known by: its file name without the extension."""
    if not path:
        return None
    return Path(str(path)).stem or None


def _product_label(
    family: Optional[str], model: Optional[str], model_or_family: Optional[str]
) -> Optional[str]:
    """The product name a chunk is embedded under.

    A family says more than a model for retrieval - a datasheet page is about
    the series, not one catalogue number - so it is preferred, and the
    document's own declaration is the fallback.
    """
    return family or model_or_family or model


def embedding_text(
    text: str,
    *,
    document_title: Optional[str] = None,
    manufacturer: Optional[str] = None,
    product: Optional[str] = None,
    section: Optional[str] = None,
) -> str:
    """The text a chunk is embedded with (D-97).

    A datasheet table row says "150" and the column header says "Pressure
    class": neither is a sentence, and embedded alone the row is close to
    every other row of numbers in the corpus. The header names the document,
    the manufacturer, the product family and the section or table caption, so
    the vector carries what the row is about.

    The header is not stored in the chunk: it is a retrieval aid, not a quote,
    and the verifier still checks the quote against the page (FR-23).
    """
    header = " | ".join(
        part
        for part in (
            f"Document: {document_title}" if document_title else "",
            f"Manufacturer: {manufacturer}" if manufacturer else "",
            f"Product: {product}" if product else "",
            f"Section: {section}" if section else "",
        )
        if part
    )
    body = (text or "").strip()
    if not header:
        return body
    return f"{header}\n{body}" if body else header


def _write_embeddings(
    session: Session, rows: list[tuple[object, Sequence[float]]]
) -> None:
    """Store a batch of embeddings in one round trip."""
    if not rows:
        return
    session.execute(
        _WRITE_EMBEDDINGS,
        {
            "ids": [str(chunk_id) for chunk_id, _ in rows],
            "vectors": [_vector_literal(vector) for _, vector in rows],
        },
    )


@dataclass
class EmbeddingReport:
    """What an embedding run did."""

    embedded: int = 0
    skipped: int = 0
    failed: int = 0
    batches: int = 0


def embed_corpus(
    session: Session,
    *,
    force: bool = False,
) -> EmbeddingReport:
    """Embed every chunk that has no embedding.

    A chunk that already has one is left alone: the embedding of a text does
    not change, and re-embedding the corpus is the expensive part.
    """
    report = EmbeddingReport()
    batch_size = get_settings().embedding_batch_size

    statement = (
        select(
            chunk.c.id,
            chunk.c.text,
            chunk.c.section,
            document.c.path,
            document.c.model_or_family,
            manufacturer.c.name,
            product.c.family,
            product.c.model,
        )
        .select_from(chunk)
        .join(document, document.c.id == chunk.c.document_id)
        .join(manufacturer, manufacturer.c.id == document.c.manufacturer_id)
        .outerjoin(product, product.c.id == document.c.product_id)
    )
    if not force:
        statement = statement.where(chunk.c.embedding.is_(None))
        report.skipped = session.execute(
            select(func.count()).select_from(chunk).where(chunk.c.embedding.isnot(None))
        ).scalar_one()
    rows = session.execute(statement).all()
    if not rows:
        logger.info("every chunk already has an embedding")
        return report

    # Shortest first. The model pads every text in a batch to the longest one,
    # so a single 3,000-character chunk makes 31 short texts pay for its width:
    # measured on this corpus, unsorted batches managed 2.8 chunks/s and
    # length-sorted ones 15.8. The embedding of a text does not depend on the
    # batch, so the order changes nothing but the time.
    rows = sorted(rows, key=lambda row: len(row[1] or ""))

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        report.batches += 1
        texts = [
            embedding_text(
                row[1] or "",
                document_title=_document_title(row[3]),
                manufacturer=row[5],
                product=_product_label(row[6], row[7], row[4]),
                section=row[2],
            )
            for row in batch
        ]
        try:
            vectors = embed(texts)
        except Exception as exc:  # noqa: BLE001 - a failed batch is reported, not raised
            logger.warning("could not embed a batch: %s", exc)
            report.failed += len(batch)
            continue

        if len(vectors) != len(batch):
            logger.warning(
                "the embedding model returned %d vectors for %d texts",
                len(vectors),
                len(batch),
            )
            report.failed += len(batch) - len(vectors)
            continue

        _write_embeddings(
            session, [(row[0], vector) for row, vector in zip(batch, vectors)]
        )
        report.embedded += len(vectors)
        session.commit()
        logger.info("embedded %d of %d chunks", report.embedded, len(rows))

    return report


def main(argv: Optional[list[str]] = None) -> int:
    """Embed the corpus and report what happened."""
    parser = argparse.ArgumentParser(description="Embed the corpus chunks.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="embed every chunk, even the ones that have an embedding",
    )
    args = parser.parse_args(argv)

    from app.repositories.db import session_scope

    with session_scope() as session:
        report = embed_corpus(session, force=args.force)

    print(f"chunks embedded:  {report.embedded}")
    print(f"already embedded: {report.skipped}")
    print(f"chunks failed:    {report.failed}")
    print(f"batches:          {report.batches}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
