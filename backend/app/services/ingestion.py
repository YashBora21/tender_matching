"""Corpus ingestion (FR-8 to FR-11).

Reads every document the collector gathered and writes it into the database:
one `document` row and its `chunk` rows.

The manifest is the hand-off. A document whose hash has not changed is not
re-ingested: its chunks are already there, and parsing it again would cost
time and change nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.repositories.postgres import PostgresDocumentRepo
from app.services.docparser import iter_csv_corpus, iter_manifest_documents, parse_document

logger = get_logger(__name__)


@dataclass
class IngestionReport:
    """What one ingestion run did."""

    documents: int = 0
    skipped_unchanged: int = 0
    failed: int = 0
    chunks: int = 0
    # Documents whose text did not change but whose metadata did: a category
    # corrected in the manifest reaches the row without a re-parse.
    metadata_refreshed: int = 0
    # Documents whose hash changed, so their chunks were replaced.
    changed: int = 0
    # Pages that could not be read. A document with a failed page is reported
    # as incomplete, never as fully ingested.
    incomplete: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def ingest_corpus(
    data_dir: Path,
    session,
    *,
    force: bool = False,
) -> IngestionReport:
    """Ingest every collected document into the database.

    Two corpus layouts are read: the collector's own manifest
    (`manufacturers/<name>/manifest.json`) and a catalogue manifest
    (`catalogue/<name>/manifest.csv`). A document whose hash matches the
    stored one is left alone, unless `force` is set. A document whose content
    changed is re-ingested and its chunks are replaced.
    """
    report = IngestionReport()
    repo = PostgresDocumentRepo(session)
    data_dir = Path(data_dir)

    documents = list(iter_manifest_documents(data_dir / "manufacturers"))
    documents += list(iter_csv_corpus(data_dir / "catalogue"))

    if not documents:
        logger.info("no corpus found under %s", data_dir)
        return report

    for file_path, entry in documents:
        sha256 = entry.get("sha256")
        if not sha256:
            report.failed += 1
            report.errors.append(f"no hash for {file_path}")
            continue

        already_there = repo.exists_by_sha256(sha256)
        # The transaction is closed before the parse. A big document takes
        # minutes to read, and a connection that sits in a transaction that
        # long is killed by the server.
        session.commit()
        if not force and already_there:
            # The text has not changed, but what the manifest says about it
            # may have. The metadata is the part retrieval filters on, so it
            # is refreshed without reading the file again.
            if repo.refresh_metadata(entry):
                report.metadata_refreshed += 1
                session.commit()
            report.skipped_unchanged += 1
            continue

        try:
            parsed = parse_document(file_path)
        except Exception as exc:  # noqa: BLE001 - one bad document must not stop the run
            report.failed += 1
            report.errors.append(f"{file_path}: {type(exc).__name__}: {exc}")
            logger.warning("could not parse %s: %s", file_path, exc)
            continue

        record = dict(entry)
        record["sha256"] = sha256
        document_id = repo.upsert_document(record)
        written = repo.insert_chunks(
            document_id,
            [
                {
                    "page": item.page,
                    "section": item.section,
                    "is_table": item.is_table,
                    "ocr": item.ocr,
                    "text": item.text,
                    "confidence": item.confidence,
                }
                for item in parsed.chunks
            ],
        )
        # The document is written and closed off: one document that fails
        # does not take the corpus with it.
        session.commit()

        report.documents += 1
        report.chunks += written
        if parsed.failed_pages:
            report.incomplete.append(str(file_path))
            logger.warning(
                "%s has %d unreadable page(s)", file_path, len(parsed.failed_pages)
            )

    return report


def main() -> int:
    """Ingest the corpus and report what happened."""
    from app.core.config import get_settings
    from app.repositories.aliases import seed_aliases, seed_attributes
    from app.repositories.db import session_scope
    from app.services.corpusseed import remove_stale_products, seed_corpus

    data_dir = get_settings().data_dir
    with session_scope() as session:
        # The canonical terms and their synonyms have to be in the database
        # before anything can resolve a name against them.
        seed_attributes(session)
        seed_aliases(session)
        report = ingest_corpus(data_dir, session)
        # The documents are in. The product each one is about is what a fact
        # hangs off and what the retrieval joins on (FR-21), so it is seeded
        # in the same run rather than in a step someone has to remember.
        seeded = seed_corpus(session)
        stale = remove_stale_products(session)

    print(f"documents ingested: {report.documents}")
    print(f"chunks written:     {report.chunks}")
    print(f"unchanged, skipped: {report.skipped_unchanged}")
    print(f"metadata refreshed: {report.metadata_refreshed}")
    print(f"products seeded:    {seeded.products}")
    print(f"family-level:       {seeded.family_level}")
    if stale:
        print(f"stale products:     {stale}")
    print(f"failed:             {report.failed}")
    if report.incomplete:
        print(f"incomplete:         {len(report.incomplete)}")
        for path in report.incomplete:
            print(f"  {path}")
    for error in report.errors:
        print(f"  error: {error}")
    # A document that could not be read is not a document that is missing: the
    # run reports it, and the corpus is incomplete until it is resolved.
    return 1 if report.failed or report.incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
