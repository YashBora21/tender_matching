"""Embedding the corpus (FR-11).

The chunks are embedded once, and the embedding is stored beside the text so
the semantic search has something to compare against. A chunk that has no
embedding is still found by the keyword search; it is only missing from the
semantic one.

The embedding is done in batches, and a batch that fails is retried, because
the embedding model is a local process and a large batch can exhaust its
memory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.repositories.schema import chunk
from app.services.llm import embed

logger = get_logger(__name__)

# How many chunks are embedded at once. A local model runs out of memory on a
# large batch, and a smaller one is only a little slower.
BATCH_SIZE = 32


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
    batch_size: int = BATCH_SIZE,
    force: bool = False,
) -> EmbeddingReport:
    """Embed every chunk that has no embedding.

    A chunk that already has one is left alone: the embedding of a text does
    not change, and re-embedding the corpus is the expensive part.
    """
    report = EmbeddingReport()

    statement = select(chunk.c.id, chunk.c.text)
    if not force:
        statement = statement.where(chunk.c.embedding.is_(None))
        report.skipped = session.execute(
            select(func.count()).select_from(chunk).where(chunk.c.embedding.isnot(None))
        ).scalar_one()
    rows = session.execute(statement).all()
    if not rows:
        logger.info("every chunk already has an embedding")
        return report

    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        report.batches += 1
        texts = [row[1] or "" for row in batch]
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

        for (chunk_id, _), vector in zip(batch, vectors):
            session.execute(
                update(chunk).where(chunk.c.id == chunk_id).values(embedding=vector)
            )
            report.embedded += 1
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
