"""Restoring the corpus from the addresses the manifests already record (D-111).

No test here reaches the network: the fetch is a stand-in. What is being
tested is the rule that matters - a document is fetched only from an address a
manifest records, and a document that cannot be verified is never written.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Optional

import pytest

from app.core.config import get_settings
from app.core.fetching import Fetched, FetchRefused
from app.services.corpus import restore as restore_module
from app.services.corpus.restore import Entry, Outcome, main, manifest_entries, restore, summary

PDF = b"%PDF-1.7\na datasheet\n"
PDF_SHA = hashlib.sha256(PDF).hexdigest()


def _manifest(data_dir: Path, manufacturer: str, documents: list[dict]) -> Path:
    """Write one collector manifest, the shape `collect.py` produces."""
    directory = data_dir / "manufacturers" / manufacturer
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.json"
    path.write_text(
        json.dumps({"manufacturer": manufacturer, "documents": documents}), encoding="utf-8"
    )
    return path


def _catalogue_csv(data_dir: Path, rows: list[dict]) -> Path:
    directory = data_dir / "catalogue"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["filename", "manufacturer", "source_url", "sha256"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _record(path: str = "raw/doc.pdf", url: str = "", sha256: str = PDF_SHA) -> dict:
    return {"path": path, "url": url, "sha256": sha256}


def _file(manifest_path: Path, relative: str, data: bytes) -> Path:
    target = manifest_path.parent / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


class _Fetcher:
    """The network, replaced. Records what was asked and answers as told."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.kwargs: list[dict] = []
        self.data = PDF
        self.refuses = False

    def __call__(self, url: str, **kwargs) -> Fetched:
        self.urls.append(url)
        self.kwargs.append(kwargs)
        if self.refuses:
            raise FetchRefused("'crane.com' resolves to a private address")
        return Fetched(url=url, data=self.data)


class _Robots:
    """The hosts' robots.txt, replaced. Nothing here reads a live site."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.allowed = True
        self.note: Optional[str] = None

    def __call__(self, url: str) -> tuple[bool, Optional[str]]:
        self.urls.append(url)
        return self.allowed, self.note if self.note else None


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def robots(monkeypatch) -> _Robots:
    fake = _Robots()
    monkeypatch.setattr(restore_module, "robots_allows", fake)
    return fake


@pytest.fixture
def fetcher(monkeypatch, robots) -> _Fetcher:
    fake = _Fetcher()
    monkeypatch.setattr(restore_module, "fetch", fake)
    return fake


def test_a_missing_document_is_written_where_the_manifest_puts_it(data_dir, fetcher):
    _manifest(
        data_dir, "flowserve", [_record("raw/series-75.pdf", "https://flowserve.com/75.pdf")]
    )

    outcome = restore(data_dir)

    assert outcome.restored == ["flowserve/series-75.pdf"]
    assert (data_dir / "manufacturers" / "flowserve" / "raw" / "series-75.pdf").read_bytes() == PDF


def test_a_file_already_in_place_is_not_fetched_again(data_dir, fetcher):
    manifest_path = _manifest(
        data_dir, "xylem", [_record("raw/leadon.pdf", "https://xylem.com/leadon.pdf")]
    )
    _file(manifest_path, "raw/leadon.pdf", PDF)

    outcome = restore(data_dir)

    assert outcome.present == ["xylem/leadon.pdf"]
    assert fetcher.urls == []


def test_a_document_with_no_recorded_address_is_reported_and_never_guessed(
    data_dir, fetcher
):
    """The gap is shown as a gap. A manufacturer's name is not a URL."""
    manifest_path = _manifest(data_dir, "ksb", [_record("raw/etaseco.pdf")])

    outcome = restore(data_dir)

    assert outcome.unrecorded == ["ksb/etaseco.pdf"]
    assert fetcher.urls == []
    assert not (manifest_path.parent / "raw" / "etaseco.pdf").exists()


def test_a_document_whose_hash_differs_is_not_written(data_dir, fetcher):
    """Filing a different file would make the report's page citations lie."""
    manifest_path = _manifest(
        data_dir,
        "nidec",
        [_record("raw/motor.pdf", "https://nidec.com/motor.pdf", "a" * 64)],
    )

    outcome = restore(data_dir)

    assert [name for name, _ in outcome.failed] == ["nidec/motor.pdf"]
    assert "hash" in outcome.failed[0][1]
    assert not (manifest_path.parent / "raw" / "motor.pdf").exists()


def test_an_address_that_answers_with_html_fails_and_writes_nothing(data_dir, fetcher):
    fetcher.data = b"<html><body>Cookie consent</body></html>"
    manifest_path = _manifest(
        data_dir, "emerson", [_record("raw/valve.pdf", "https://emerson.com/valve.pdf")]
    )

    outcome = restore(data_dir)

    assert outcome.restored == []
    assert outcome.failed == [("emerson/valve.pdf", "the address did not return a PDF")]
    assert not (manifest_path.parent / "raw" / "valve.pdf").exists()


def test_an_address_that_answers_with_nothing_is_not_an_empty_datasheet(data_dir, fetcher):
    fetcher.data = b""
    _manifest(data_dir, "abb", [_record("raw/motor.pdf", "https://abb.com/motor.pdf")])

    outcome = restore(data_dir)

    assert outcome.failed == [("abb/motor.pdf", "the address returned an empty document")]


def test_a_refused_fetch_is_a_failure_and_not_a_missing_capability(data_dir, fetcher):
    fetcher.refuses = True
    _manifest(data_dir, "crane", [_record("raw/coe.pdf", "https://crane.com/coe.pdf")])

    outcome = restore(data_dir)

    assert outcome.failed == [
        ("crane/coe.pdf", "'crane.com' resolves to a private address")
    ]


def test_a_document_a_host_has_stopped_allowing_is_not_fetched(data_dir, fetcher, robots):
    """Robots is asked again: a policy can change after a document was taken."""
    robots.allowed = False
    robots.note = "robots.txt disallows this path for the collector's user agent"
    manifest_path = _manifest(
        data_dir, "ksb", [_record("raw/etaseco.pdf", "https://ksb.de/etaseco.pdf")]
    )

    outcome = restore(data_dir)

    assert fetcher.urls == []
    assert robots.urls == ["https://ksb.de/etaseco.pdf"]
    assert outcome.failed == [
        (
            "ksb/etaseco.pdf",
            "the host's robots.txt does not allow it: "
            "robots.txt disallows this path for the collector's user agent",
        )
    ]
    assert not (manifest_path.parent / "raw" / "etaseco.pdf").exists()


def test_the_size_the_corpus_is_allowed_is_the_one_the_settings_set(data_dir, fetcher):
    _manifest(data_dir, "pentair", [_record("raw/pump.pdf", "https://pentair.com/pump.pdf")])

    restore(data_dir)

    assert fetcher.kwargs == [
        {"max_bytes": get_settings().corpus_max_bytes, "from_document": False}
    ]


def test_a_manifest_address_is_not_gated_by_the_rule_for_a_tenders_links(
    data_dir, fetcher
):
    """The host allowlist answers "may a document found inside a tender ask for
    this?"; a manifest is the operator's own list, and its hash is the check.
    Every other guard is still asked."""
    _manifest(
        data_dir, "lenntech", [_record("raw/etanorm.pdf", "https://lenntech.example/etanorm.pdf")]
    )

    outcome = restore(data_dir)

    assert outcome.restored == ["lenntech/etanorm.pdf"]
    assert fetcher.kwargs[0]["from_document"] is False
    assert (data_dir / "manufacturers" / "lenntech" / "raw" / "etanorm.pdf").exists()


def test_a_manifest_row_that_carries_no_hash_leaves_the_file_it_has(data_dir, fetcher):
    """A hash-less manifest cannot vouch for the file, so it is not overwritten."""
    manifest_path = _manifest(
        data_dir, "apollo", [_record("raw/butterfly.pdf", "https://apollovalves.com/b.pdf", "")]
    )
    _file(manifest_path, "raw/butterfly.pdf", b"%PDF-1.4 an earlier edition")

    outcome = restore(data_dir)

    assert outcome.present == ["apollo/butterfly.pdf"]
    assert fetcher.urls == []


def test_a_catalogue_csv_is_read_as_well_as_a_collector_json(data_dir, fetcher):
    _catalogue_csv(
        data_dir,
        [
            {
                "filename": "etline.pdf",
                "manufacturer": "flowserve",
                "source_url": "https://flowserve.com/etline.pdf",
                "sha256": PDF_SHA,
            }
        ],
    )

    outcome = restore(data_dir)

    assert outcome.restored == ["flowserve/etline.pdf"]
    assert fetcher.urls == ["https://flowserve.com/etline.pdf"]


def test_a_document_named_twice_in_one_catalogue_is_restored_once(data_dir, fetcher):
    """The same file listed under two categories is one file, not two fetches."""
    row = {
        "filename": "shared.pdf",
        "manufacturer": "abb",
        "source_url": "https://abb.com/shared.pdf",
        "sha256": PDF_SHA,
    }
    _catalogue_csv(data_dir, [row, dict(row)])

    entries = manifest_entries(data_dir)
    outcome = restore(data_dir)

    assert len(entries) == 1
    assert fetcher.urls == ["https://abb.com/shared.pdf"]
    assert outcome.restored == ["abb/shared.pdf"]


def test_an_address_named_by_two_trees_is_asked_for_once(data_dir, fetcher, robots):
    """Ingestion reads both trees, so both get the file - from one fetch."""
    _manifest(data_dir, "wilo", [_record("raw/pump.pdf", "https://example.com/w.pdf")])
    catalogue = data_dir / "catalogue" / "wilo"
    catalogue.mkdir(parents=True)
    (catalogue / "manifest.json").write_text(
        json.dumps(
            {"manufacturer": "wilo", "documents": [_record("raw/pump.pdf", "https://example.com/w.pdf")]}
        ),
        encoding="utf-8",
    )

    outcome = restore(data_dir)

    assert fetcher.urls == ["https://example.com/w.pdf"]
    assert robots.urls == ["https://example.com/w.pdf"]
    assert outcome.restored == ["wilo/pump.pdf", "wilo/pump.pdf"]
    assert (data_dir / "manufacturers" / "wilo" / "raw" / "pump.pdf").exists()
    assert (catalogue / "raw" / "pump.pdf").exists()


def test_a_dry_run_says_what_it_would_fetch_and_fetches_nothing(data_dir, fetcher):
    _manifest(data_dir, "sulzer", [_record("raw/pump.pdf", "https://nidec.com/pump.pdf")])

    outcome = restore(data_dir, dry_run=True)

    assert fetcher.urls == []
    assert outcome.restored == ["sulzer/pump.pdf (would fetch)"]


def test_one_manufacturer_can_be_restored_on_its_own(data_dir, fetcher):
    for maker in ("valmatic", "bharat_bijlee"):
        _manifest(
            data_dir, maker, [_record("raw/doc.pdf", f"https://example.com/{maker}.pdf")]
        )

    outcome = restore(data_dir, only="valmatic")

    assert outcome.restored == ["valmatic/doc.pdf"]
    assert fetcher.urls == ["https://example.com/valmatic.pdf"]


def test_an_entry_is_named_by_its_manufacturer_and_its_file():
    entry = Entry(
        manufacturer="flowserve",
        filename="75.pdf",
        target=Path("data/manufacturers/flowserve/raw/75.pdf"),
    )

    assert entry.name == "flowserve/75.pdf"


def test_the_summary_shows_the_counts_and_every_gap():
    outcome = Outcome(
        present=["a/one.pdf"],
        restored=["a/two.pdf"],
        failed=[("b/three.pdf", "the address did not return a PDF")],
        unrecorded=["c/four.pdf", "c/five.pdf"],
    )

    text = summary(outcome)

    assert "in place: 1" in text
    assert "restored: 1" in text
    assert "failed:   1" in text
    assert "no recorded source: 2" in text
    assert "b/three.pdf: the address did not return a PDF" in text
    assert "c/four.pdf" in text


def test_the_missing_count_adds_what_could_not_be_fixed():
    outcome = Outcome(failed=[("a/b.pdf", "gone")], unrecorded=["c/d.pdf"])

    assert outcome.missing == 2


def test_a_corpus_that_is_already_there_exits_zero(data_dir, fetcher):
    assert main(["--data-dir", str(data_dir)]) == 0


def test_a_corpus_with_a_gap_exits_one_and_fetches_nothing_it_cannot_verify(
    data_dir, fetcher
):
    _manifest(data_dir, "krishna", [_record("raw/pump.pdf")])

    assert main(["--data-dir", str(data_dir)]) == 1
    assert fetcher.urls == []
