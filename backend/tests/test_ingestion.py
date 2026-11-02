"""Corpus ingestion tests: the manifest becomes documents and chunks.

These use a fake session, so no database is needed. The database-backed test
lives in `test_ingestion_db.py` and is marked `integration`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.corpus.ingest import ingest_corpus


class FakeSession:
    """Records what the repository writes, without a database."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.commits: int = 0

    def commit(self) -> None:
        self.commits += 1


def _pdf(path: Path, text: str) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), text, fontsize=10)
    document.save(path)
    document.close()


def _manifest(root: Path, manufacturer: str, documents: list[dict]) -> None:
    directory = root / "manufacturers" / manufacturer
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps({"manufacturer": manufacturer, "documents": documents}),
        encoding="utf-8",
    )


def test_a_collected_document_is_ingested(tmp_path, monkeypatch):
    """The manifest is the hand-off: it names the file and the metadata."""
    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    _pdf(raw / "datasheet.pdf", "Body of body shall be SS316 with pressure rating 16 bar")

    _manifest(
        tmp_path,
        "acme",
        [
            {
                "url": "https://acme.example/d.pdf",
                "sha256": "a" * 64,
                "doc_type": "datasheet",
                "retrieved_at": "2026-10-21T09:00:00+00:00",
                "path": "raw/datasheet.pdf",
                "model_or_family": "GV series",
                "category": "gate_valves",
            }
        ],
    )

    calls: list[dict] = []

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):
            calls.append(record)
            return "doc-1"

        def insert_chunks(self, document_id, chunks):
            calls.append({"chunks": chunks})
            return len(chunks)

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    assert report.chunks >= 1
    assert calls[0]["manufacturer"] == "acme"
    assert calls[0]["model_or_family"] == "GV series"
    assert calls[0]["category"] == "gate_valves"
    assert calls[0]["sha256"] == "a" * 64


def test_a_document_is_written_and_closed_off_on_its_own(tmp_path, monkeypatch):
    """A big document takes minutes to read, and a connection that sits in a
    transaction that long is killed by the server. The transaction is closed
    before the parse and the document is committed on its own."""
    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    _pdf(raw / "datasheet.pdf", "Body of body shall be SS316")
    _manifest(
        tmp_path,
        "acme",
        [{"url": "u", "sha256": "d" * 64, "path": "raw/datasheet.pdf"}],
    )

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):
            return "doc-1"

        def insert_chunks(self, document_id, chunks):
            return len(chunks)

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    session = FakeSession()
    report = ingest_corpus(tmp_path, session)

    assert report.documents == 1
    # One commit to close the transaction before the parse, one to write the
    # document.
    assert session.commits == 2


def test_a_document_that_did_not_change_is_not_re_ingested(tmp_path, monkeypatch):
    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    _pdf(raw / "datasheet.pdf", "Body of body shall be SS316")
    _manifest(
        tmp_path,
        "acme",
        [{"url": "u", "sha256": "b" * 64, "path": "raw/datasheet.pdf"}],
    )

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return True

        def refresh_metadata(self, record):
            # Nothing changed in the manifest either, so nothing is written.
            return False

        def upsert_document(self, record):  # pragma: no cover - must not run
            raise AssertionError("an unchanged document must not be re-ingested")

        def insert_chunks(self, document_id, chunks):  # pragma: no cover
            raise AssertionError("an unchanged document must not be re-ingested")

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 0
    assert report.skipped_unchanged == 1
    assert report.metadata_refreshed == 0


def test_a_corrected_category_reaches_an_unchanged_document(tmp_path, monkeypatch):
    """The text did not change; what the manifest says about it did.

    The category is what retrieval filters on, so a manifest corrected after
    the first ingestion has to reach the stored document. Re-parsing the file
    would cost minutes and change no chunk, so only the metadata moves.
    """
    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    _pdf(raw / "datasheet.pdf", "Body of body shall be SS316")
    _manifest(
        tmp_path,
        "acme",
        [{"url": "u", "sha256": "d" * 64, "path": "raw/datasheet.pdf", "category": "valves"}],
    )

    refreshed: list[dict] = []

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return True

        def refresh_metadata(self, record):
            refreshed.append(record)
            return True

        def upsert_document(self, record):  # pragma: no cover - must not run
            raise AssertionError("the text did not change, so it must not be re-read")

        def insert_chunks(self, document_id, chunks):  # pragma: no cover
            raise AssertionError("the text did not change, so it must not be re-read")

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.skipped_unchanged == 1
    assert report.metadata_refreshed == 1
    assert refreshed and refreshed[0]["category"] == "valves"


def test_a_document_that_cannot_be_parsed_is_reported_not_raised(tmp_path, monkeypatch):
    """One broken document must not stop the run."""
    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    (raw / "broken.pdf").write_bytes(b"not a pdf at all")
    _manifest(
        tmp_path,
        "acme",
        [{"url": "u", "sha256": "c" * 64, "path": "raw/broken.pdf"}],
    )

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):  # pragma: no cover
            raise AssertionError

        def insert_chunks(self, document_id, chunks):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.failed == 1
    assert report.errors
    assert "broken.pdf" in report.errors[0]


def test_a_csv_catalogue_is_ingested(tmp_path, monkeypatch):
    """A corpus gathered elsewhere can carry a CSV manifest instead."""
    import csv

    catalogue = tmp_path / "catalogue" / "acme"
    catalogue.mkdir(parents=True)
    _pdf(catalogue / "spec.pdf", "Body of body shall be SS316")
    with (tmp_path / "catalogue" / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "manufacturer",
                "filename",
                "source_url",
                "retrieved_at",
                "sha256",
                "doc_type",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "manufacturer": "acme",
                "filename": "spec.pdf",
                "source_url": "https://acme.example/spec.pdf",
                "retrieved_at": "2026-10-21T09:00:00+00:00",
                "sha256": "d" * 64,
                "doc_type": "specification",
                "notes": "product specification",
            }
        )

    calls: list[dict] = []

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):
            calls.append(record)
            return "doc-1"

        def insert_chunks(self, document_id, chunks):
            return len(chunks)

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    assert calls[0]["manufacturer"] == "acme"
    assert calls[0]["doc_type"] == "specification"


def test_one_file_named_by_two_manifests_is_ingested_once(tmp_path, monkeypatch):
    """The collector's manifest and the catalogue's name the same bytes, and
    the corpus holds one document per content hash - so one parse, one row, and
    a report whose count is a count of documents rather than of listings."""
    import csv

    raw = tmp_path / "manufacturers" / "acme" / "raw"
    raw.mkdir(parents=True)
    _pdf(raw / "spec.pdf", "Body of body shall be SS316")
    _manifest(
        tmp_path,
        "acme",
        [
            {
                "url": "https://acme.example/s.pdf",
                "sha256": "b" * 64,
                "doc_type": "datasheet",
                "path": "raw/spec.pdf",
                "category": "gate_valves",
            }
        ],
    )
    catalogue = tmp_path / "catalogue" / "acme"
    catalogue.mkdir(parents=True)
    _pdf(catalogue / "spec-copy.pdf", "Body of body shall be SS316")
    with (tmp_path / "catalogue" / "manifest.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["manufacturer", "filename", "source_url", "sha256", "doc_type"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "manufacturer": "acme",
                "filename": "spec-copy.pdf",
                "source_url": "https://acme.example/s.pdf",
                "sha256": "b" * 64,
                "doc_type": "datasheet",
            }
        )

    calls: list[dict] = []

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):
            calls.append(record)
            return "doc-1"

        def insert_chunks(self, document_id, chunks):
            return len(chunks)

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    assert len(calls) == 1


def test_a_document_on_disk_that_no_manifest_lists_is_still_ingested(tmp_path, monkeypatch):
    """The corpus is the set of files that exist. Leaving the unlisted ones
    out would make the corpus quietly incomplete."""
    catalogue = tmp_path / "catalogue" / "acme"
    catalogue.mkdir(parents=True)
    _pdf(catalogue / "unlisted.pdf", "Body of body shall be SS316")

    calls: list[dict] = []

    class FakeRepo:
        def __init__(self, session):
            pass

        def exists_by_sha256(self, sha256):
            return False

        def upsert_document(self, record):
            calls.append(record)
            return "doc-1"

        def insert_chunks(self, document_id, chunks):
            return len(chunks)

    monkeypatch.setattr("app.services.corpus.ingest.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    # It is hashed here, because a document without a hash cannot be tracked.
    assert len(calls[0]["sha256"]) == 64
    assert calls[0]["manufacturer"] == "acme"


def test_every_doc_type_the_corpus_uses_is_in_the_vocabulary():
    """A doc_type outside the vocabulary is not collected (D-30), so a corpus
    that uses one is a corpus the collector would refuse.

    The catalogue is found where the ingestion looks for it, not at a path
    written into the test: the corpus is not in git, and a path that names one
    machine silently skips the check on every other one.
    """
    import csv

    from app.core.config import get_settings
    from app.services.match.evidence import load_doc_types

    vocabulary = set(load_doc_types().doc_types)
    catalogue = Path(get_settings().data_dir) / "catalogue"
    manifests = sorted(catalogue.glob("**/manifest.csv"))
    if not manifests:
        pytest.skip(f"no catalogue manifest under {catalogue}")

    used: set[str] = set()
    for manifest in manifests:
        with manifest.open(encoding="utf-8", newline="") as handle:
            used |= {row["doc_type"] for row in csv.DictReader(handle) if row.get("doc_type")}

    assert used <= vocabulary, f"doc types not in the vocabulary: {used - vocabulary}"


def test_ingesting_a_corpus_seeds_the_products_its_documents_are_about(monkeypatch):
    """A document is evidence about a product. Ingesting it without seeding
    the product leaves every passage naming none, so the retrieval cannot say
    what the evidence is for and the fact cache can never hit (FR-21)."""
    from contextlib import contextmanager

    from app.services.corpus import ingest

    calls: list[str] = []

    class Report:
        documents = 0
        chunks = 0
        skipped_unchanged = 0
        metadata_refreshed = 0
        failed = 0
        incomplete: list[str] = []
        errors: list[str] = []

    class Seed:
        products = 2
        family_level = 1

    @contextmanager
    def scope():
        yield object()

    monkeypatch.setattr("app.repositories.db.session_scope", scope)
    monkeypatch.setattr(
        "app.repositories.aliases.seed_attributes", lambda s: calls.append("attributes")
    )
    monkeypatch.setattr(
        "app.repositories.aliases.seed_aliases", lambda s: calls.append("aliases")
    )
    monkeypatch.setattr(
        ingest, "ingest_corpus", lambda *a, **k: calls.append("ingest") or Report()
    )
    monkeypatch.setattr(
        "app.services.corpus.seed.seed_corpus",
        lambda s: calls.append("seed") or Seed(),
    )
    monkeypatch.setattr(
        "app.services.corpus.seed.remove_stale_products",
        lambda s: calls.append("stale") or 0,
    )

    assert ingest.main() == 0
    assert calls == ["attributes", "aliases", "ingest", "seed", "stale"]
