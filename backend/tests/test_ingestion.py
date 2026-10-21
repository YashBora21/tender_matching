"""Corpus ingestion tests: the manifest becomes documents and chunks.

These use a fake session, so no database is needed. The database-backed test
lives in `test_ingestion_db.py` and is marked `integration`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ingestion import ingest_corpus


class FakeSession:
    """Records what the repository writes, without a database."""

    def __init__(self) -> None:
        self.executed: list[str] = []


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

    monkeypatch.setattr("app.services.ingestion.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    assert report.chunks >= 1
    assert calls[0]["manufacturer"] == "acme"
    assert calls[0]["model_or_family"] == "GV series"
    assert calls[0]["category"] == "gate_valves"
    assert calls[0]["sha256"] == "a" * 64


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

        def upsert_document(self, record):  # pragma: no cover - must not run
            raise AssertionError("an unchanged document must not be re-ingested")

        def insert_chunks(self, document_id, chunks):  # pragma: no cover
            raise AssertionError("an unchanged document must not be re-ingested")

    monkeypatch.setattr("app.services.ingestion.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 0
    assert report.skipped_unchanged == 1


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

    monkeypatch.setattr("app.services.ingestion.PostgresDocumentRepo", FakeRepo)

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

    monkeypatch.setattr("app.services.ingestion.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    assert calls[0]["manufacturer"] == "acme"
    assert calls[0]["doc_type"] == "specification"


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

    monkeypatch.setattr("app.services.ingestion.PostgresDocumentRepo", FakeRepo)

    report = ingest_corpus(tmp_path, FakeSession())

    assert report.documents == 1
    # It is hashed here, because a document without a hash cannot be tracked.
    assert len(calls[0]["sha256"]) == 64
    assert calls[0]["manufacturer"] == "acme"


def test_every_doc_type_the_corpus_uses_is_in_the_vocabulary():
    """A doc_type outside the vocabulary is not collected (D-30), so a corpus
    that uses one is a corpus the collector would refuse."""
    import csv

    from app.services.evidence import load_doc_types

    vocabulary = set(load_doc_types().doc_types)
    manifest = Path(
        "C:/Users/Lenovo/OneDrive/Desktop/tender_matching/backend/data/catalogue/manifest.csv"
    )
    if not manifest.exists():
        pytest.skip("the collected corpus is not present")

    with manifest.open(encoding="utf-8", newline="") as handle:
        used = {row["doc_type"] for row in csv.DictReader(handle) if row.get("doc_type")}

    assert used <= vocabulary, f"doc types not in the vocabulary: {used - vocabulary}"
