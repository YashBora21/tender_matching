"""Receiving an uploaded tender: validation, freezing, and the background read.

No database and no network: the tender row is a fake, and the collection and
the audit are replaced by functions that record what they were asked for. What
is tested is what happens to the file and what the tender's state becomes.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.schemas.enums import TenderStatus
from app.services import tenderupload
from app.services.tendercollect import TenderManifest, read_manifest


def _pdf(path: Path, text: str) -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), text, fontsize=10)
    document.save(path)
    document.close()
    return path.read_bytes()


class FakeRepo:
    """The tender row, without a database.

    The row and the log live on the class: one ingest constructs a repo for
    itself and another for the audit, and both must see the same row, as two
    sessions against one database would.
    """

    instances: list["FakeRepo"] = []
    created: list[dict] = []
    statuses: list[tuple[str, str | None]] = []
    row: dict = {}

    def __init__(self, session) -> None:
        self.session = session
        FakeRepo.instances.append(self)

    def upsert_tender(self, *, status=TenderStatus.DONE, **values):
        FakeRepo.created.append({**values, "status": status})
        return "T1"

    def get_tender(self, tender_id):
        return dict(FakeRepo.row) if tender_id == "T1" else None

    def set_status(self, tender_id, status, *, error=None):
        FakeRepo.statuses.append((status.value, error))
        FakeRepo.row["status"] = status.value
        if error is not None:
            FakeRepo.row["error"] = error


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def _fakes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    FakeRepo.instances.clear()
    FakeRepo.created.clear()
    FakeRepo.statuses.clear()
    FakeRepo.row.clear()
    FakeRepo.row.update({"id": "T1", "status": "queued", "error": None})
    monkeypatch.setattr(tenderupload, "TenderRepo", FakeRepo)
    return tmp_path


# --- the name and the bytes -----------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("tender.pdf", "tender.pdf"),
        ("Tender 1.PDF", "Tender 1.PDF"),
        ("nested/dir/tender.pdf", "tender.pdf"),
        ("..\\..\\escape.pdf", "escape.pdf"),
    ],
)
def test_the_uploaded_name_is_reduced_to_its_final_component(given, expected):
    assert tenderupload.safe_name(given) == expected


@pytest.mark.parametrize("given", ["", None, ".hidden.pdf", "tender.exe", "notes"])
def test_a_file_that_is_not_a_named_pdf_is_refused(given):
    with pytest.raises(tenderupload.ValidationError):
        tenderupload.safe_name(given)


def test_the_content_decides_whether_it_is_a_pdf():
    assert tenderupload.is_pdf(b"%PDF-1.7\n...")
    # A name says nothing about what the bytes are.
    assert not tenderupload.is_pdf(b"<html>not a pdf</html>")


def test_bytes_that_are_not_a_pdf_are_refused(_fakes):
    with pytest.raises(tenderupload.ValidationError):
        tenderupload.store_upload(
            FakeSession(), filename="tender.pdf", data=b"<html>not a pdf</html>"
        )


def test_an_empty_file_is_refused(_fakes):
    with pytest.raises(tenderupload.ValidationError):
        tenderupload.store_upload(FakeSession(), filename="tender.pdf", data=b"")


# --- freezing --------------------------------------------------------------


def test_an_uploaded_tender_is_frozen_with_its_hash_before_it_is_read(_fakes):
    data = b"%PDF-1.7\n% synthetic\n"

    uploaded = tenderupload.store_upload(
        FakeSession(), filename="tender.pdf", data=data
    )

    assert uploaded.tender_id == "T1"
    assert uploaded.status == TenderStatus.QUEUED.value
    assert not uploaded.duplicate

    directory = _fakes / "tenders" / "T1"
    # The file is on disk, under the tender's own id, not in a web root.
    assert (directory / "tender.pdf").read_bytes() == data
    manifest = read_manifest(directory / "manifest.json")
    assert manifest["wrapper_sha256"] == tenderupload.sha256_bytes(data)
    assert manifest["filename"] == "tender.pdf"
    # The row was created in the queued state; it is not read yet.
    assert FakeRepo.created[0]["status"] is TenderStatus.QUEUED


def test_a_second_upload_of_the_same_file_is_the_same_tender(_fakes):
    data = b"%PDF-1.7\n% synthetic\n"
    tenderupload.store_upload(FakeSession(), filename="tender.pdf", data=data)
    first = read_manifest(_fakes / "tenders" / "T1" / "manifest.json")

    # A different name, the same bytes: the hash decides it is the same file.
    uploaded = tenderupload.store_upload(
        FakeSession(), filename="renamed.pdf", data=data
    )

    assert uploaded.duplicate is True
    # The frozen copy already there is not replaced or duplicated.
    manifest = read_manifest(_fakes / "tenders" / "T1" / "manifest.json")
    assert manifest == first


def test_a_duplicate_says_what_state_the_tender_reached(_fakes):
    data = b"%PDF-1.7\n% synthetic\n"
    tenderupload.store_upload(FakeSession(), filename="tender.pdf", data=data)
    FakeRepo.row["status"] = TenderStatus.DONE.value

    uploaded = tenderupload.store_upload(
        FakeSession(), filename="tender.pdf", data=data
    )

    assert uploaded.status == TenderStatus.DONE.value


# --- the documents the read uses ------------------------------------------


def test_a_document_uploaded_by_hand_survives_a_re_read():
    """`collect_tender` builds its manifest from the wrapper's links, so a
    manual upload would vanish on the next attempt unless it is carried over."""
    manual = {
        "url": "upload://boq.csv",
        "status": "manual",
        "kind": "boq-csv",
        "filename": "boq.csv",
        "path": "raw/boq.csv",
        "sha256": "c" * 64,
    }
    fetched = {"url": "https://example.test/tender.pdf", "status": "unchanged"}
    previous = {"documents": [manual, fetched]}
    collected = TenderManifest(
        tender_id="T1",
        documents=[tenderupload.TenderDocument(url=fetched["url"], status="unchanged")],
    )

    kept = tenderupload._manual_documents(previous, collected)

    assert [document.url for document in kept] == ["upload://boq.csv"]
    assert kept[0].path == "raw/boq.csv"


def test_a_manual_document_is_not_duplicated_when_it_is_also_a_link():
    manual = {"url": "https://example.test/boq.csv", "status": "manual"}
    collected = TenderManifest(
        tender_id="T1",
        documents=[tenderupload.TenderDocument(url=manual["url"], status="manual")],
    )

    assert tenderupload._manual_documents({"documents": [manual]}, collected) == []


def test_a_tender_that_is_its_own_specification_becomes_an_attachment(tmp_path):
    """Many tenders are one PDF with the specification inside, not a wrapper
    of links. It is still a tender with requirements."""
    path = tmp_path / "tender.pdf"
    _pdf(path, "TECHNICAL SPECIFICATION - the body shall be SS316")

    document = tenderupload._wrapper_document(path, tmp_path)

    assert document is not None
    assert document.kind == "spec"
    assert document.path == "tender.pdf"
    assert document.status == "manual"
    assert len(document.sha256) == 64


def test_a_wrapper_with_nothing_readable_inside_is_not_an_attachment(tmp_path):
    path = tmp_path / "wrapper.pdf"
    _pdf(path, "Click here to download the bill of quantities")

    assert tenderupload._wrapper_document(path, tmp_path) is None


# --- the background read ---------------------------------------------------


def _stored_tender(tmp_path: Path) -> Path:
    directory = tenderupload.tender_dir("T1")
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "tender.pdf"
    _pdf(wrapper, "TECHNICAL SPECIFICATION - the body shall be SS316")
    tenderupload.write_manifest(
        TenderManifest(
            tender_id="T1",
            filename="tender.pdf",
            wrapper_path=str(wrapper),
            wrapper_sha256="a" * 64,
            page_count=1,
            documents=[],
        ),
        directory,
    )
    return directory


def test_reading_a_tender_moves_it_from_processing_to_done(_fakes, monkeypatch):
    _stored_tender(_fakes)

    class Audit:
        failure = None
        rate_limited = False
        rate_limited_detail = None
        pages_read = 1
        items = 1
        requirements = 1

    monkeypatch.setattr(
        tenderupload,
        "collect_tender",
        lambda *a, **k: TenderManifest(tender_id="T1", documents=[]),
    )
    monkeypatch.setattr(tenderupload, "audit_tender", lambda *a, **k: Audit())
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(FakeSession()))

    assert tenderupload.ingest("T1") == TenderStatus.DONE.value
    assert FakeRepo.statuses == [
        (TenderStatus.PROCESSING.value, None),
        (TenderStatus.DONE.value, None),
    ]


def _audit(**values):
    """A stand-in for what the pipeline audit reports."""

    class Audit:
        failure = None
        rate_limited = False
        rate_limited_detail = None
        pages_read = 1
        items = 1
        requirements = 1

    for name, value in values.items():
        setattr(Audit, name, value)
    return Audit()


def _ingest_with(monkeypatch, audit):
    monkeypatch.setattr(
        tenderupload,
        "collect_tender",
        lambda *a, **k: TenderManifest(tender_id="T1", documents=[]),
    )
    monkeypatch.setattr(tenderupload, "audit_tender", lambda *a, **k: audit)
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(FakeSession()))
    return tenderupload.ingest("T1")


def test_a_tender_with_no_page_read_is_not_done(_fakes, monkeypatch):
    """No page read is a tender nobody looked at, not a tender that states
    nothing (PRD 4)."""
    _stored_tender(_fakes)

    status = _ingest_with(monkeypatch, _audit(pages_read=0, requirements=0))

    assert status == TenderStatus.INCOMPLETE.value
    assert FakeRepo.statuses[-1] == (
        TenderStatus.INCOMPLETE.value,
        "no page of the specification was read",
    )


def test_a_tender_with_no_items_is_not_done(_fakes, monkeypatch):
    _stored_tender(_fakes)

    status = _ingest_with(monkeypatch, _audit(items=0, requirements=0))

    assert status == TenderStatus.INCOMPLETE.value
    assert FakeRepo.statuses[-1] == (
        TenderStatus.INCOMPLETE.value,
        "the bill of quantities gave no items",
    )


def test_a_tender_with_no_requirements_is_not_done(_fakes, monkeypatch):
    _stored_tender(_fakes)

    status = _ingest_with(monkeypatch, _audit(requirements=0))

    assert status == TenderStatus.INCOMPLETE.value
    assert FakeRepo.statuses[-1] == (
        TenderStatus.INCOMPLETE.value,
        "the specification gave no requirements",
    )


def test_a_rate_limited_tender_is_neither_done_nor_failed(_fakes, monkeypatch):
    """The extraction stopped at the provider's limit. What was read is kept,
    and the tender says so instead of claiming it was read in full (D8)."""
    _stored_tender(_fakes)

    class Audit:
        failure = None
        rate_limited = True
        rate_limited_detail = "the model was rate limited; retry in 24.9s"

    monkeypatch.setattr(
        tenderupload,
        "collect_tender",
        lambda *a, **k: TenderManifest(tender_id="T1", documents=[]),
    )
    monkeypatch.setattr(tenderupload, "audit_tender", lambda *a, **k: Audit())
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(FakeSession()))

    assert tenderupload.ingest("T1") == TenderStatus.RATE_LIMITED.value
    status, error = FakeRepo.statuses[-1]
    assert status == TenderStatus.RATE_LIMITED.value
    assert "24.9" in error


def test_a_tender_that_cannot_be_read_is_failed_with_its_reason(_fakes, monkeypatch):
    _stored_tender(_fakes)

    class Audit:
        failure = "no bill of quantities was found: the only attachment is a form"
        rate_limited = False
        rate_limited_detail = None

    monkeypatch.setattr(
        tenderupload,
        "collect_tender",
        lambda *a, **k: TenderManifest(tender_id="T1", documents=[]),
    )
    monkeypatch.setattr(tenderupload, "audit_tender", lambda *a, **k: Audit())
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(FakeSession()))

    assert tenderupload.ingest("T1") == TenderStatus.FAILED.value
    status, error = FakeRepo.statuses[-1]
    assert status == TenderStatus.FAILED.value
    assert "no bill of quantities" in error


def test_an_exception_is_recorded_and_never_left_processing(_fakes, monkeypatch):
    _stored_tender(_fakes)

    def explode(*args, **kwargs):
        raise RuntimeError("the wrapper is empty")

    monkeypatch.setattr(tenderupload, "collect_tender", explode)
    session = FakeSession()
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(session))

    assert tenderupload.ingest("T1") == TenderStatus.FAILED.value
    assert session.rollbacks == 1
    status, error = FakeRepo.statuses[-1]
    assert status == TenderStatus.FAILED.value
    assert "RuntimeError" in error and "the wrapper is empty" in error


def test_a_tender_whose_file_is_gone_is_failed_with_what_to_do(_fakes, monkeypatch):
    directory = _fakes / "tenders" / "T1"
    directory.mkdir(parents=True)
    tenderupload.write_manifest(
        TenderManifest(tender_id="T1", filename="tender.pdf", documents=[]),
        directory,
    )
    monkeypatch.setattr("app.repositories.db.session_scope", _scope(FakeSession()))

    assert tenderupload.ingest("T1") == TenderStatus.FAILED.value
    status, error = FakeRepo.statuses[-1]
    assert status == TenderStatus.FAILED.value
    assert "upload it again" in error


def _scope(session):
    @contextmanager
    def scope():
        yield session

    return scope
