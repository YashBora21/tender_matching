"""Tender attachment collection: the links, the freeze, the failures."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from app.services.tendercollect import (
    classify_document,
    collect_tender,
    discover_links,
    read_manifest,
    write_manifest,
)

DOC_B = b"%PDF-1.4 a bill of quantities"
BOQ_TABLE = b"%PDF-1.4 table"


@pytest.fixture(scope="module")
def server():
    """A server that serves a robots.txt, a BOQ and a document that fails."""
    state = {"robots": b"User-agent: *\nAllow: /\n", "fail": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/robots.txt":
                body = state["robots"]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/boq.csv":
                body = b"Item Number,Item Title,Item Quantity\n1,GATE VALVE,3\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/refused.pdf":
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()


def _allow_localhost(monkeypatch) -> None:
    """Let the tests reach their own local server.

    The fetcher refuses a private address, which is right: a link in a
    document must not reach the machine's own network. A test server is on
    the loopback address, so the host is excepted for the test.
    """
    monkeypatch.setenv("FETCH_ALLOWED_DOMAINS", "127.0.0.1")
    monkeypatch.setenv("FETCH_ALLOWED_PRIVATE_HOSTS", "127.0.0.1")
    from app.core.config import get_settings

    get_settings.cache_clear()


def _wrapper_with_links(path: Path, links: list[str]) -> None:
    """A PDF whose pages carry the given links."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 90), "Bid Details / विवरण", fontsize=10)
    for link in links:
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 100, 200, 120), "uri": link})
    document.save(path)
    document.close()


def test_the_links_of_a_wrapper_are_found(tmp_path):
    path = tmp_path / "tender.pdf"
    _wrapper_with_links(path, ["https://example.com/a.pdf", "https://example.com/b.csv"])

    links = discover_links(path)

    assert links == ["https://example.com/a.pdf", "https://example.com/b.csv"]


def test_a_link_that_is_not_a_document_is_ignored(tmp_path):
    path = tmp_path / "tender.pdf"
    _wrapper_with_links(
        path,
        [
            "https://example.com/a.pdf",
            "mailto:someone@example.com",
            "https://example.com/catalogue",
            "https://example.com/downloadOmppdfile/",
        ],
    )

    assert discover_links(path) == ["https://example.com/a.pdf"]


def test_a_boq_is_told_from_a_form_by_its_content():
    """A link's file name says nothing: the same shape carries the BOQ in one
    tender and a certificate in another."""
    import pymupdf

    def make_pdf(text: str, path: Path):
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 90), text, fontsize=10)
        document.save(path)
        document.close()
        return path.read_bytes()

    tmp = Path(__file__).parent
    boq = make_pdf(
        "BOQ Item number  BOQ Item Title  BOQ Item Description  BOQ Item Quantity",
        tmp / "_boq.pdf",
    )
    form = make_pdf("Declaration regarding conflict of interest", tmp / "_form.pdf")

    assert classify_document(boq, "x.pdf")[0] == "boq-pdf"
    assert classify_document(form, "x.pdf")[0] == "form"
    for name in ("_boq.pdf", "_form.pdf"):
        (tmp / name).unlink(missing_ok=True)


def test_a_scanned_document_is_still_a_document():
    """No text at all: it is a scanned document, and the OCR pass reads it
    later. It is not a document that says nothing."""
    import pymupdf

    tmp = Path(__file__).parent / "_scanned.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(tmp)
    document.close()
    data = tmp.read_bytes()
    tmp.unlink(missing_ok=True)

    kind, pages = classify_document(data, "x.pdf")
    assert kind == "scanned"
    assert pages == 1


def test_a_failed_fetch_is_recorded_as_failed(tmp_path, server, monkeypatch):
    """A document that cannot be fetched is a failed document. It is never
    a tender with no requirements."""
    base, _ = server
    _allow_localhost(monkeypatch)
    path = tmp_path / "tender.pdf"
    _wrapper_with_links(path, [f"{base}/refused.pdf"])

    manifest = collect_tender("T1", path, tmp_path / "out")

    assert len(manifest.documents) == 1
    assert manifest.documents[0].status == "failed"
    assert manifest.documents[0].sha256 is None


def test_a_restricted_path_is_not_fetched(tmp_path, server, monkeypatch):
    base, state = server
    _allow_localhost(monkeypatch)
    state["robots"] = b"User-agent: *\nDisallow: /\n"
    try:
        path = tmp_path / "tender.pdf"
        _wrapper_with_links(path, [f"{base}/boq.csv"])

        manifest = collect_tender("T1", path, tmp_path / "out")

        assert manifest.documents[0].status == "restricted"
        assert manifest.documents[0].note
        assert not (tmp_path / "out" / "raw").exists()
    finally:
        state["robots"] = b"User-agent: *\nAllow: /\n"


def test_a_fetched_document_is_frozen_with_its_hash_and_url(tmp_path, server, monkeypatch):
    base, _ = server
    _allow_localhost(monkeypatch)
    path = tmp_path / "tender.pdf"
    _wrapper_with_links(path, [f"{base}/boq.csv"])

    manifest = collect_tender("T1", path, tmp_path / "out")

    document = manifest.documents[0]
    assert document.status == "new"
    assert document.kind == "boq-csv"
    assert document.sha256 and len(document.sha256) == 64
    assert document.retrieved_at
    frozen = tmp_path / "out" / "raw" / document.filename
    assert frozen.exists()
    assert frozen.read_bytes().startswith(b"Item Number")


def test_the_manifest_records_the_tender_and_its_documents(tmp_path, server, monkeypatch):
    base, _ = server
    _allow_localhost(monkeypatch)
    path = tmp_path / "tender.pdf"
    _wrapper_with_links(path, [f"{base}/boq.csv"])

    manifest = collect_tender("T1", path, tmp_path / "out", category="valve")
    written = write_manifest(manifest, tmp_path / "out")

    data = read_manifest(written)
    assert data["tender_id"] == "T1"
    assert data["category"] == "valve"
    assert data["documents"][0]["kind"] == "boq-csv"
    assert data["documents"][0]["sha256"]
