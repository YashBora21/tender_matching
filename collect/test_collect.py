"""Collector tests: the rules that decide what the manifest says."""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402
from collect import ManifestEntry  # noqa: E402

DOC_A = b"%PDF-1.4 document A"
DOC_B = b"%PDF-1.4 document B, different content"

ROBOTS = {"mode": "allow"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/robots.txt":
            if ROBOTS["mode"] == "server_error":
                self._send(500, b"boom")
            elif ROBOTS["mode"] == "absent":
                self._send(404, b"nope")
            elif ROBOTS["mode"] == "forbidden":
                self._send(403, b"nope")
            elif ROBOTS["mode"] == "disallow":
                # A real rule, not an error: the path is off limits.
                self._send(200, b"User-agent: *\nDisallow: /a/\n")
            else:
                self._send(200, b"User-agent: *\nAllow: /\n")
        elif self.path == "/a/datasheet.pdf":
            if ROBOTS["mode"] == "empty":
                self._send(200, b"", {"Content-Type": "application/pdf"})
            else:
                self._send(200, DOC_A, {"Content-Type": "application/pdf"})
        elif self.path == "/b/datasheet.pdf":
            self._send(200, DOC_B, {"Content-Type": "application/pdf"})
        else:
            self._send(404, b"missing")

    def do_HEAD(self):
        self.do_GET()


@pytest.fixture(scope="module")
def server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def reset_robots():
    ROBOTS["mode"] = "allow"


def _entry(url: str, **kwargs) -> collect.SourceEntry:
    return collect.SourceEntry(
        manufacturer="Acme Valves",
        url=url,
        doc_type="datasheet",
        model_or_family="GV",
        **kwargs,
    )


def test_robots_disallow_stops_the_fetch_and_is_recorded(server, tmp_path):
    ROBOTS["mode"] = "forbidden"
    entry = _entry(f"{server}/a/datasheet.pdf")
    # A 403 on robots.txt is a policy, not a broken site.
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.status == "restricted"
    assert record.sha256 is None
    assert record.restriction_note


def test_a_robots_disallow_rule_stops_the_fetch(server, tmp_path):
    """The rule itself, not just an unreadable robots.txt."""
    ROBOTS["mode"] = "disallow"
    entry = _entry(f"{server}/a/datasheet.pdf")
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.status == "restricted"
    assert record.sha256 is None
    # A path the rule does not cover is still fetched.
    other = collect.collect_entry(
        _entry(f"{server}/b/datasheet.pdf"), "cat", tmp_path, None, {}, False
    )
    assert other.status == "new"


def test_a_broken_robots_txt_is_not_treated_as_a_restriction(server, tmp_path):
    """A server error is not a policy. Refusing to fetch on a 500 would mean a
    site outage silently empties the corpus."""
    ROBOTS["mode"] = "server_error"
    entry = _entry(f"{server}/a/datasheet.pdf")
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.status == "new"
    assert record.sha256 == collect.sha256_bytes(DOC_A)
    assert "robots.txt" in (record.notes or "")


def test_two_urls_with_the_same_file_name_do_not_overwrite_each_other(server, tmp_path):
    first = _entry(f"{server}/a/datasheet.pdf")
    second = _entry(f"{server}/b/datasheet.pdf")
    taken: dict[str, str] = {}

    one = collect.collect_entry(first, "cat", tmp_path, None, taken, False)
    taken[one.filename] = one.url
    two = collect.collect_entry(second, "cat", tmp_path, None, taken, False)

    assert one.filename != two.filename
    raw = tmp_path / "manufacturers" / "acme-valves" / "raw"
    assert (raw / one.filename).read_bytes() == DOC_A
    assert (raw / two.filename).read_bytes() == DOC_B


def test_a_deleted_file_is_fetched_again_not_recorded_as_present(server, tmp_path):
    """The manifest says a file exists. If it was deleted, the manifest has to
    stop saying so."""
    entry = _entry(f"{server}/a/datasheet.pdf")
    first = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    raw = tmp_path / "manufacturers" / "acme-valves" / "raw"
    (raw / first.filename).unlink()

    again = collect.collect_entry(entry, "cat", tmp_path, first.to_dict(), {}, False)
    assert (raw / again.filename).exists()
    assert again.sha256 == first.sha256


def test_the_same_url_in_two_categories_keeps_both_records(server, tmp_path):
    entry = _entry(f"{server}/a/datasheet.pdf")
    source_one = collect.SourceFile(category="one", entries=[entry])
    source_two = collect.SourceFile(category="two", entries=[entry])

    collect.write_manifests(collect.collect_category(source_one, tmp_path, False), tmp_path, False)
    collect.write_manifests(collect.collect_category(source_two, tmp_path, False), tmp_path, False)

    manifest = json.loads(
        (tmp_path / "manufacturers" / "acme-valves" / "manifest.json").read_text()
    )
    categories = {doc["category"] for doc in manifest["documents"]}
    assert categories == {"one", "two"}


def test_an_unreadable_manifest_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "manufacturers" / "acme-valves" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(collect.ManifestError):
        collect._read_manifest(path)
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_a_manual_document_is_recorded_and_never_downloaded(tmp_path):
    entry = _entry(
        "https://example.com/catalogue/index.html",
        manual=True,
        filename="acme-catalogue.pdf",
        note="gated catalogue, obtained by hand",
    )
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.status == "manual"
    assert record.filename == "acme-catalogue.pdf"
    assert record.sha256 is None
    assert "gated" in (record.notes or "")


def test_a_secondary_document_is_marked(server, tmp_path):
    """A distributor document is recorded, and marked so it never overrides a
    manufacturer document (FR-7)."""
    entry = _entry(f"{server}/b/datasheet.pdf", secondary=True)
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.is_secondary is True


def test_the_manifest_lines_up_with_the_document_table(server, tmp_path):
    entry = _entry(f"{server}/a/datasheet.pdf", models=["GV-100", "GV-200"])
    record = collect.collect_entry(entry, "gate_valves", tmp_path, None, {}, False)

    # The columns of the `document` table the manifest has to supply. The
    # record keeps the field even when it is None, so the ingestion phase can
    # read the manifest straight into the table.
    for column in (
        "url",
        "sha256",
        "doc_type",
        "version_date",
        "retrieved_at",
        "path",
        "is_secondary",
        "restriction_note",
    ):
        assert column in ManifestEntry.__dataclass_fields__

    assert record.models == ["GV-100", "GV-200"]
    # version_date is the document's own date; retrieved_at is when we fetched
    # it. They are not the same field.
    assert record.version_date is None
    assert record.retrieved_at


def test_an_empty_response_is_not_recorded_as_a_document(server, tmp_path):
    """A 200 with no body is not a document. Recording the hash of an empty
    string would put an unreadable file in the corpus."""
    ROBOTS["mode"] = "empty"
    entry = _entry(f"{server}/a/datasheet.pdf")
    record = collect.collect_entry(entry, "cat", tmp_path, None, {}, False)
    assert record.status == "failed"
    assert record.sha256 is None


def test_the_manifest_keeps_the_slug_as_the_stable_manufacturer_key(tmp_path):
    entry = _entry("https://example.com/a.pdf")
    collect.write_manifests(collect.collect_category(
        collect.SourceFile(category="cat", entries=[entry]), tmp_path, False
    ), tmp_path, False)

    manifest = json.loads(
        (tmp_path / "manufacturers" / "acme-valves" / "manifest.json").read_text()
    )
    assert manifest["manufacturer_slug"] == "acme-valves"
    # The slug is the key, but the name is the name.
    assert manifest["manufacturer"] == "Acme Valves"


def test_an_entry_with_an_unknown_doc_type_is_not_collected(tmp_path, monkeypatch):
    """The ingestion phase maps a doc_type to a document class. An unmapped
    one would land in the corpus as a document nobody can classify."""
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "cat.json").write_text(
        json.dumps(
            {
                "category": "cat",
                "entries": [
                    {
                        "manufacturer": "Acme Valves",
                        "url": "https://example.com/a.pdf",
                        "doc_type": "brochure",
                        "model_or_family": "GV",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect, "SOURCES_DIR", sources)

    assert collect.main(["--data-dir", str(tmp_path / "data")]) == 1
    assert not (tmp_path / "data" / "manufacturers").exists()


def test_a_known_doc_type_is_collected(server, tmp_path, monkeypatch):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "cat.json").write_text(
        json.dumps(
            {
                "category": "cat",
                "entries": [
                    {
                        "manufacturer": "Acme Valves",
                        "url": f"{server}/a/datasheet.pdf",
                        "doc_type": "datasheet",
                        "model_or_family": "GV",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect, "SOURCES_DIR", sources)

    assert collect.main(["--data-dir", str(tmp_path / "data")]) == 0
    manifest = tmp_path / "data" / "manufacturers" / "acme-valves" / "manifest.json"
    assert json.loads(manifest.read_text())["documents"][0]["doc_type"] == "datasheet"
