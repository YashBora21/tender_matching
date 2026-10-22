"""The application code names no tender.

The pipeline is generic: it takes a directory of tenders and treats every one
of them the same way. A tender id, a file name or an attachment URL that
appears in the application code would be a tender the system knows about
before it has read it, and every other tender would be second class.

This test reads the corpus and checks that none of it is written into the
code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _source_files() -> list[Path]:
    return sorted(APP_ROOT.rglob("*.py"))


def _corpus_manifests() -> list[Path]:
    """The manifests of the collected corpus, if it is present."""
    from app.core.config import get_settings

    corpus = get_settings().data_dir / "tenders"
    if not corpus.exists():
        return []
    return sorted(corpus.glob("*/manifest.json"))


def _identifiers() -> set[str]:
    """The names and the URLs of the corpus, as they must not appear in code."""
    found: set[str] = set()
    for manifest_path in _corpus_manifests():
        import json

        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tender_id = data.get("tender_id")
        if tender_id:
            found.add(str(tender_id))
        for record in data.get("documents", []):
            url = record.get("url")
            if url:
                found.add(str(url))
            filename = record.get("filename")
            if filename:
                found.add(str(filename))
    return found


def test_the_application_code_names_no_tender():
    """No tender id, file name or attachment URL from the corpus is written
    into the application code."""
    identifiers = _identifiers()
    if not identifiers:
        pytest.skip("the collected corpus is not present")

    # A name is only a name if it is specific: a file called "a.pdf" would
    # match everything.
    specific = {
        identifier
        for identifier in identifiers
        if len(identifier) > 8 and not identifier.endswith(".csv")
    }
    if not specific:
        pytest.skip("the collected corpus has no distinctive names")

    offenders: list[str] = []
    for source in _source_files():
        text = source.read_text(encoding="utf-8")
        for identifier in specific:
            if identifier in text:
                offenders.append(f"{source.name}: {identifier}")

    assert not offenders, "the application code names a tender: " + "; ".join(
        offenders[:10]
    )


def test_the_application_code_holds_no_tender_url():
    """No URL from the corpus is written into the application code."""
    urls = {
        identifier
        for identifier in _identifiers()
        if identifier.startswith("http")
    }
    if not urls:
        pytest.skip("the collected corpus is not present")

    offenders: list[str] = []
    for source in _source_files():
        text = source.read_text(encoding="utf-8")
        for url in urls:
            if url in text:
                offenders.append(f"{source.name}: {url}")

    assert not offenders, "the application code holds a tender URL: " + "; ".join(
        offenders[:10]
    )


def test_the_application_code_holds_no_tender_file_name():
    """No file name from the corpus is written into the application code."""
    names = {
        identifier
        for identifier in _identifiers()
        if identifier.lower().endswith((".pdf", ".csv"))
        and len(identifier) > 12
    }
    if not names:
        pytest.skip("the collected corpus is not present")

    offenders: list[str] = []
    for source in _source_files():
        text = source.read_text(encoding="utf-8")
        for name in names:
            if name in text:
                offenders.append(f"{source.name}: {name}")

    assert not offenders, "the application code holds a tender file name: " + "; ".join(
        offenders[:10]
    )


def test_the_application_code_names_no_tender_id_pattern():
    """No tender id of the shape the corpus uses is written into the code."""
    # The corpus names its tenders GEM_<number>_<Category>.
    pattern = re.compile(r"GEM_\d{5,}")
    offenders: list[str] = []
    for source in _source_files():
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{source.name}:{number}: {line.strip()[:70]}")

    assert not offenders, "the application code holds a tender id: " + "; ".join(
        offenders[:10]
    )
