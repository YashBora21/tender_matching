#!/usr/bin/env python3
"""Offline corpus collector (PRD 5.1, FR-1 to FR-7).

Downloads the manufacturer documents listed in a source config and records each
one in a per-manufacturer manifest. It fetches only what a config lists, it
respects robots.txt, and it records a restriction rather than working around
one.

A source config is one JSON file per category, in `collect/sources/`:

    {
      "category": "gate_valves",
      "entries": [
        {
          "manufacturer": "Acme Valves",
          "url": "https://acme.example/gv-datasheet.pdf",
          "doc_type": "datasheet",
          "model_or_family": "GV series"
        }
      ]
    }

Adding a category is adding a file. No code changes (FR-6).

The tool never invents a document, a hash or a date: every field in the
manifest comes from a real file or a real response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = Path(__file__).resolve().parent / "sources"
DEFAULT_DATA_DIR = REPO_ROOT / "backend" / "data"
DOC_TYPES_PATH = REPO_ROOT / "backend" / "data" / "doc_types.json"

# The collector identifies itself so a site can see who is asking, and so a
# robots.txt rule can target it deliberately.
USER_AGENT = os.getenv("COLLECT_USER_AGENT", "tender-corpus-collector/1.0")
TIMEOUT_SECONDS = int(os.getenv("COLLECT_TIMEOUT_SECONDS", "30"))


def load_doc_types() -> list[str]:
    """The document-type vocabulary the manifest uses.

    The app side has its own classification (`DocumentClass`) for the priority
    a document carries; the mapping between the two lands with the ingestion
    phase, which is where a manifest becomes a `document` row.
    """
    data = json.loads(DOC_TYPES_PATH.read_text(encoding="utf-8"))
    return list(data["doc_types"])


# --- config ------------------------------------------------------------------


class SourceEntry(BaseModel):
    """One document to collect (FR-1)."""

    model_config = ConfigDict(extra="forbid")

    manufacturer: str
    url: str
    doc_type: str
    model_or_family: str
    # The specific models covered, when the document covers more than the
    # family. Always present in the manifest, empty list when there are none.
    models: list[str] = []
    # A distributor or reseller document. It is recorded, but it never
    # overrides a manufacturer document (FR-7).
    secondary: bool = False
    # A document obtained by hand, behind a JS page or a gated catalogue
    # (FR-3). It is recorded but never downloaded.
    manual: bool = False
    # The local file name for a manual document.
    filename: Optional[str] = None
    # The document's own revision date. Not the retrieval date.
    version_date: Optional[str] = None
    note: Optional[str] = None


class SourceFile(BaseModel):
    """One category's worth of source config."""

    model_config = ConfigDict(extra="forbid")

    category: str
    entries: list[SourceEntry] = []


class ManifestError(Exception):
    """A manifest exists but cannot be read. It is never overwritten."""


# --- manifest ----------------------------------------------------------------


@dataclass
class ManifestEntry:
    """One document as recorded in the manifest (FR-2).

    The field names line up with the `document` table so the ingestion phase
    can read the manifest straight into it.
    """

    url: str
    manufacturer: str
    category: str
    doc_type: str
    model_or_family: str
    status: str
    # new | unchanged | changed | restricted | manual | failed
    models: list[str] = None  # type: ignore[assignment]
    filename: Optional[str] = None
    path: Optional[str] = None
    sha256: Optional[str] = None
    previous_sha256: Optional[str] = None
    retrieved_at: Optional[str] = None
    version_date: Optional[str] = None
    is_secondary: bool = False
    restriction_note: Optional[str] = None
    notes: Optional[str] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None

    def __post_init__(self) -> None:
        if self.models is None:
            self.models = []

    def to_dict(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if v is not None}
        data["models"] = self.models
        return data


# --- helpers -----------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    """A filesystem-safe name for a manufacturer or category."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "unnamed"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _basename(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return name or "document.pdf"


def _unique_filename(name: str, url: str, taken: dict[str, str]) -> str:
    """A name no other document is using.

    Two different URLs can end in the same file name ("datasheet.pdf" on two
    manufacturers' sites). Writing the second one over the first loses a
    document, so the name gets a short tag from the URL.
    """
    owner = taken.get(name)
    if owner is None or owner == url:
        return name
    stem = Path(name).stem
    suffix = Path(name).suffix
    tag = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"{stem}-{tag}{suffix}"


# --- robots ------------------------------------------------------------------


@dataclass
class RobotsVerdict:
    allowed: bool
    note: Optional[str] = None
    restriction: bool = False


def _robots_verdict(url: str) -> RobotsVerdict:
    """Check the site's robots.txt for this URL (FR-4).

    A robots.txt that disallows the path is a restriction and the document is
    not fetched. A robots.txt that cannot be read is not a restriction: a
    server error is a broken site, not a policy, and it is recorded as a note
    so the run can be read afterwards.
    """
    parts = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return RobotsVerdict(
                False, f"robots.txt is not readable (HTTP {exc.code}); treated as disallow-all", True
            )
        if 400 <= exc.code < 500:
            return RobotsVerdict(True)  # no robots.txt, so no rules
        return RobotsVerdict(True, f"robots.txt could not be read (HTTP {exc.code}); no restriction applied")
    except urllib.error.URLError as exc:
        return RobotsVerdict(True, f"robots.txt could not be fetched ({exc.reason}); no restriction applied")

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(text.splitlines())
    if parser.can_fetch(USER_AGENT, url):
        return RobotsVerdict(True)
    return RobotsVerdict(
        False, "robots.txt disallows this path for the collector's user agent", True
    )


# --- fetching ----------------------------------------------------------------


@dataclass
class FetchResult:
    status: str
    data: Optional[bytes] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None
    note: Optional[str] = None


def _http_get(url: str, headers: dict[str, str]) -> FetchResult:
    """GET a URL. Never raises: a failed fetch is a recorded outcome."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return FetchResult(
                "ok",
                data=response.read(),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                content_type=response.headers.get("Content-Type"),
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult("not_modified")
        return FetchResult("error", note=f"HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        return FetchResult("error", note=f"{type(exc.reason).__name__}: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 - the reason is recorded, not raised
        return FetchResult("error", note=f"{type(exc).__name__}: {exc}")


# --- collection --------------------------------------------------------------


def _read_manifest(path: Path) -> dict:
    """Read a manifest. A manifest that cannot be parsed is an error.

    Returning an empty manifest here would silently throw away every document
    already recorded, so this refuses instead.
    """
    if not path.exists():
        return {"documents": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path} is not a manifest object")
    data.setdefault("documents", [])
    return data


def _write_manifest(path: Path, payload: dict) -> None:
    """Write a manifest atomically, so a crash cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def collect_entry(
    entry: SourceEntry,
    category: str,
    data_dir: Path,
    previous: Optional[dict],
    taken: dict[str, str],
    dry_run: bool,
) -> ManifestEntry:
    """Collect one document and describe what happened to it."""
    slug = slugify(entry.manufacturer)
    raw_dir = data_dir / "manufacturers" / slug / "raw"

    record = ManifestEntry(
        url=entry.url,
        manufacturer=entry.manufacturer,
        category=category,
        doc_type=entry.doc_type,
        model_or_family=entry.model_or_family,
        status="failed",
        models=list(entry.models),
        is_secondary=entry.secondary,
        version_date=entry.version_date,
    )
    if entry.note:
        record.notes = entry.note

    # A document obtained by hand is recorded, never downloaded (FR-3).
    if entry.manual:
        name = entry.filename or _basename(entry.url)
        candidate = raw_dir / name
        record.status = "manual"
        record.filename = name
        record.path = f"raw/{name}"
        if candidate.exists():
            record.sha256 = sha256_file(candidate)
            record.retrieved_at = now_iso()
        else:
            record.notes = "; ".join(
                filter(
                    None,
                    [record.notes, f"not downloaded yet; put the file at {candidate}"],
                )
            )
        return record

    verdict = _robots_verdict(entry.url)
    if not verdict.allowed:
        record.status = "restricted"
        record.restriction_note = verdict.note
        return record
    if verdict.note:
        record.notes = "; ".join(filter(None, [record.notes, verdict.note]))

    headers = {"User-Agent": USER_AGENT}
    if previous:
        # A conditional request, so an unchanged document is not re-downloaded.
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]

    result = _http_get(entry.url, headers)

    if result.status == "not_modified":
        # The server says the file is unchanged, but the file has to be on disk
        # for the manifest to be true.
        name = (previous or {}).get("filename") or _basename(entry.url)
        if not (raw_dir / name).exists():
            headers.pop("If-None-Match", None)
            headers.pop("If-Modified-Since", None)
            result = _http_get(entry.url, headers)
        else:
            record.status = "unchanged"
            record.filename = name
            record.path = (previous or {}).get("path") or f"raw/{name}"
            record.sha256 = (previous or {}).get("sha256")
            record.retrieved_at = (previous or {}).get("retrieved_at")
            record.etag = (previous or {}).get("etag")
            record.last_modified = (previous or {}).get("last_modified")
            return record

    if result.status == "error":
        record.status = "failed"
        record.notes = "; ".join(filter(None, [record.notes, result.note]))
        return record

    if not result.data:
        # A 200 with no body is not a document. Recording the hash of an empty
        # string would put a document in the corpus that cannot be read.
        record.status = "failed"
        record.notes = "; ".join(
            filter(None, [record.notes, "the server returned an empty document"])
        )
        return record

    digest = sha256_bytes(result.data)
    old_digest = (previous or {}).get("sha256")

    filename = _unique_filename(_basename(entry.url), entry.url, taken)
    target = raw_dir / filename

    record.filename = filename
    record.path = f"raw/{filename}"
    record.sha256 = digest
    record.retrieved_at = now_iso()
    record.etag = result.etag
    record.last_modified = result.last_modified
    record.content_type = result.content_type

    if old_digest == digest:
        # Same content as last time: nothing to re-ingest. The file is still
        # written, in case it was deleted since the last run.
        record.status = "unchanged"
    else:
        record.status = "changed" if old_digest else "new"
        record.previous_sha256 = old_digest

    if not dry_run:
        raw_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(result.data or b"")
    return record


def collect_category(
    source: SourceFile,
    data_dir: Path,
    dry_run: bool,
) -> dict[str, list[ManifestEntry]]:
    """Collect one category, grouped by manufacturer."""
    grouped: dict[str, list[ManifestEntry]] = {}
    for entry in source.entries:
        slug = slugify(entry.manufacturer)
        manifest_path = data_dir / "manufacturers" / slug / "manifest.json"
        existing = _read_manifest(manifest_path)
        by_key = {(d.get("category"), d.get("url")): d for d in existing.get("documents", [])}
        taken = {
            d.get("filename"): d.get("url")
            for d in existing.get("documents", [])
            if d.get("filename")
        }
        previous = by_key.get((source.category, entry.url))

        record = collect_entry(entry, source.category, data_dir, previous, taken, dry_run)
        grouped.setdefault(slug, []).append(record)
        taken[record.filename] = record.url
    return grouped


def write_manifests(
    grouped: dict[str, list[ManifestEntry]],
    data_dir: Path,
    dry_run: bool,
) -> None:
    """Merge the collected records into each manufacturer's manifest."""
    for slug, records in grouped.items():
        manufacturer_dir = data_dir / "manufacturers" / slug
        manifest_path = manufacturer_dir / "manifest.json"
        existing = _read_manifest(manifest_path)
        by_key = {(d.get("category"), d.get("url")): d for d in existing.get("documents", [])}

        for record in records:
            by_key[(record.category, record.url)] = record.to_dict()

        documents = sorted(by_key.values(), key=lambda doc: (doc.get("category") or "", doc.get("url") or ""))
        # The configured name is the manufacturer's name. The slug is the
        # stable key: the name in a datasheet footer can change, and a renamed
        # manufacturer must not become a new one.
        name = existing.get("manufacturer") or next(
            (doc["manufacturer"] for doc in documents if doc.get("manufacturer")), slug
        )
        payload = {
            "manufacturer": name,
            "manufacturer_slug": slug,
            "updated_at": now_iso(),
            "documents": documents,
        }
        if dry_run:
            continue
        _write_manifest(manifest_path, payload)


# --- config loading ----------------------------------------------------------


def load_sources(sources_dir: Path) -> list[SourceFile]:
    """Load every source config, skipping the `*.example.json` templates."""
    sources: list[SourceFile] = []
    if not sources_dir.exists():
        return sources
    for path in sorted(sources_dir.glob("*.json")):
        if path.name.endswith(".example.json"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  skipped {path.name}: {exc}", file=sys.stderr)
            continue
        try:
            sources.append(SourceFile(**raw))
        except ValidationError as exc:
            print(f"  skipped {path.name}: {exc}", file=sys.stderr)
    return sources


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the manufacturer corpus.")
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="category to collect (repeatable); default is every category",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="where the corpus and manifests are written",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen without writing anything",
    )
    args = parser.parse_args(argv)

    sources = load_sources(SOURCES_DIR)
    if args.category:
        wanted = set(args.category)
        sources = [s for s in sources if s.category in wanted]
        missing = wanted - {s.category for s in sources}
        if missing:
            print(f"no such category: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not sources:
        print(f"no source configs in {SOURCES_DIR}", file=sys.stderr)
        return 2

    doc_types = set(load_doc_types())
    data_dir = Path(args.data_dir)
    failures = 0
    for source in sources:
        print(f"\n[{source.category}]")
        usable: list[SourceEntry] = []
        for entry in source.entries:
            if entry.doc_type not in doc_types:
                # The ingestion phase maps a doc_type to a document class, so
                # an unmapped one is not collected at all.
                print(
                    f"  skipped {entry.url}: doc_type {entry.doc_type!r} is not in the vocabulary",
                    file=sys.stderr,
                )
                failures += 1
                continue
            usable.append(entry)
        try:
            grouped = collect_category(
                SourceFile(category=source.category, entries=usable),
                data_dir,
                args.dry_run,
            )
        except ManifestError as exc:
            print(f"  {exc}", file=sys.stderr)
            print("  fix or move the manifest; it was not overwritten", file=sys.stderr)
            return 2
        for slug, records in grouped.items():
            for record in records:
                line = f"  {record.status:12} {record.doc_type:20} {record.url}"
                if record.sha256:
                    line += f"  sha256={record.sha256[:12]}"
                if record.restriction_note:
                    line += f"  ({record.restriction_note})"
                if record.notes:
                    line += f"  [{record.notes}]"
                print(line)
                if record.status == "failed":
                    failures += 1
        write_manifests(grouped, data_dir, args.dry_run)

    if args.dry_run:
        print("\ndry run: nothing was written")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
