"""Put the corpus back on a machine that only has the manifests (D-111).

The collected PDFs are not in git and are not meant to be: what is in git is
the manifest that names every document by its address, its hash and the page it
was taken from. A fresh clone therefore knows exactly which files the corpus is
made of and has none of them, and ingestion answers "no corpus found" (FR-8).

This reads those manifests and writes the files back where ingestion looks for
them. It chooses nothing:

- a document is fetched only from an address a manifest already records; no
  address is guessed, searched for, or built from a manufacturer's name, because
  an invented source is the failure this project forbids above all others;
- a document whose hash does not match the manifest is **not** written. The
  report cites pages of the file the manifest describes; replacing it with a
  different file would make those citations point at something else;
- the host's robots.txt is asked again, because a policy can change after a
  document was collected; a document that is no longer allowed is reported as
  a gap rather than fetched anyway;
- a document with no recorded address is reported as unrecorded, so the gap is
  visible instead of being filled with a substitute.

The tender-attachment host rule is not applied here. That rule exists because
an address inside a tender is a reader's data, not an operator's request; a
manifest is the operator's own list, and its hash is what checks the answer.
Every other guard still holds: no private address, no redirect, the size cap
and the time cap, and robots.txt is honoured.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.fetching import FetchRefused, fetch, robots_allows
from app.core.logging import get_logger
from app.services.docparser import sha256_of_file

logger = get_logger(__name__)

# A collected document is a PDF. The signature is checked before anything is
# written, so a page that answered with HTML is not filed as a datasheet.
_PDF_MAGIC = b"%PDF-"


@dataclass
class Entry:
    """One document a manifest says the corpus holds."""

    manufacturer: str
    filename: str
    target: Path
    url: str = ""
    sha256: str = ""

    @property
    def name(self) -> str:
        return f"{self.manufacturer}/{self.filename}"


@dataclass
class Outcome:
    """What the restore did, in the four states a document can be in."""

    present: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unrecorded: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        """The documents the corpus does not have and this run could not fix."""
        return len(self.failed) + len(self.unrecorded)

    def as_dict(self) -> dict:
        return {
            "present": self.present,
            "restored": self.restored,
            "failed": [{"document": name, "reason": reason} for name, reason in self.failed],
            "unrecorded": self.unrecorded,
        }


def _json_entries(manifest_path: Path) -> list[Entry]:
    """The collector's own manifest: one manufacturer, a list of documents.

    The path is relative to the manifest's directory, which is where the
    collector wrote it and where ingestion reads it from (FR-8).
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read %s: %s", manifest_path, exc)
        return []

    manufacturer = manifest.get("manufacturer") or manifest_path.parent.name
    found: list[Entry] = []
    for record in manifest.get("documents", []):
        relative = record.get("path")
        if not relative:
            continue
        found.append(
            Entry(
                manufacturer=record.get("manufacturer") or manufacturer,
                filename=Path(relative).name,
                target=manifest_path.parent / relative,
                url=(record.get("url") or "").strip(),
                sha256=(record.get("sha256") or "").strip(),
            )
        )
    return found


def _csv_target(manifest_dir: Path, manufacturer: str, filename: str) -> Path:
    """Where a catalogue row's file lives, or belongs.

    A catalogue keeps one directory per manufacturer and the manifest may sit
    beside those directories or inside one, so an existing file is left where
    it is and a missing one is written into the manufacturer's directory.
    """
    for candidate in (
        [manifest_dir / manufacturer / filename] if manufacturer else []
    ) + [manifest_dir / filename]:
        if candidate.exists():
            return candidate
    if manufacturer:
        return manifest_dir / manufacturer / filename
    return manifest_dir / filename


def _csv_entries(manifest_path: Path) -> list[Entry]:
    """A catalogue manifest: one row per document, addressed by `source_url`."""
    found: list[Entry] = []
    try:
        handle = manifest_path.open(encoding="utf-8", newline="")
    except OSError as exc:
        logger.warning("could not open %s: %s", manifest_path, exc)
        return []
    with handle:
        for row in csv.DictReader(handle):
            filename = (row.get("filename") or "").strip()
            if not filename:
                continue
            manufacturer = (row.get("manufacturer") or "").strip()
            found.append(
                Entry(
                    manufacturer=manufacturer or manifest_path.parent.name,
                    filename=filename,
                    target=_csv_target(manifest_path.parent, manufacturer, filename),
                    url=(row.get("source_url") or "").strip(),
                    sha256=(row.get("sha256") or "").strip(),
                )
            )
    return found


def manifest_entries(data_dir: Path) -> list[Entry]:
    """Every document the manifests name, from both corpus shapes.

    Ingestion reads `manufacturers/` and `catalogue/` separately (FR-8), so a
    document present in both trees has two homes and both are restored. What is
    collapsed here is the same file path named twice - a catalogue that lists
    one document under two categories, which is not two documents.
    """
    data_dir = Path(data_dir)
    found: list[Entry] = []
    for manifest_path in sorted((data_dir / "manufacturers").glob("*/manifest.json")):
        found.extend(_json_entries(manifest_path))
    catalogue = data_dir / "catalogue"
    for manifest_path in sorted(catalogue.glob("*/manifest.json")):
        found.extend(_json_entries(manifest_path))
    for manifest_path in sorted(catalogue.glob("**/manifest.csv")):
        found.extend(_csv_entries(manifest_path))

    seen: set[Path] = set()
    unique: list[Entry] = []
    for entry in found:
        resolved = entry.target.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(entry)
    return unique


def _is_in_place(entry: Entry) -> bool:
    """The file exists and is the file the manifest describes."""
    if not entry.target.exists():
        return False
    if not entry.sha256:
        # A manifest with no hash cannot vouch for the file, but the file is
        # there and ingestion reads it; replacing it from the network would be
        # a change, not a restore.
        return True
    return sha256_of_file(entry.target) == entry.sha256


def _write(target: Path, data: bytes) -> None:
    """Write the file in one step a reader cannot see half of."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def _wanted(entry: Entry, only: Optional[str]) -> bool:
    if not only:
        return True
    needle = only.strip().lower()
    return needle in entry.manufacturer.lower() or needle in entry.target.parent.name.lower()


def _rejection(entry: Entry, data: bytes) -> Optional[str]:
    """Why this document may not be filed, or None when it may.

    The checks are the manifest's own claims: that the address holds a PDF, and
    that the PDF is the one the manifest hashed.
    """
    if not data:
        return "the address returned an empty document"
    if not data.startswith(_PDF_MAGIC):
        return "the address did not return a PDF"
    if entry.sha256 and hashlib.sha256(data).hexdigest() != entry.sha256:
        return (
            "the document at that address no longer matches the manifest hash; "
            "the manifest was not rewritten and the file was not replaced"
        )
    return None


def restore(
    data_dir: Optional[Path] = None,
    *,
    only: Optional[str] = None,
    dry_run: bool = False,
) -> Outcome:
    """Fetch what is missing, verify it, and write it where ingestion looks.

    Nothing already in place is touched, and nothing that fails verification is
    written: the outcome says which documents the corpus still does not have.
    One address is asked once per run even when two manifests name it.
    """
    settings = get_settings()
    root = Path(data_dir) if data_dir else Path(settings.data_dir)
    outcome = Outcome()
    documents: dict[str, bytes] = {}
    refusals: dict[str, str] = {}
    # One robots.txt answer per address: an address named by two manifests is
    # asked once, the same way it is fetched once.
    permissions: dict[str, Optional[str]] = {}

    for entry in manifest_entries(root):
        if not _wanted(entry, only):
            continue
        if _is_in_place(entry):
            outcome.present.append(entry.name)
            continue
        if not entry.url:
            # No address on record: this is a gap in the collection, not a
            # download failure, and it is reported as one.
            outcome.unrecorded.append(entry.name)
            continue
        if dry_run:
            outcome.restored.append(f"{entry.name} (would fetch)")
            continue

        if entry.url not in permissions:
            allowed, note = robots_allows(entry.url)
            permissions[entry.url] = None if allowed else (note or "the path is disallowed")
        if permissions[entry.url]:
            outcome.failed.append(
                (entry.name, f"the host's robots.txt does not allow it: {permissions[entry.url]}")
            )
            continue
        if entry.url in refusals:
            outcome.failed.append((entry.name, refusals[entry.url]))
            continue
        if entry.url in documents:
            data = documents[entry.url]
        else:
            try:
                data = fetch(
                    entry.url,
                    max_bytes=settings.corpus_max_bytes,
                    from_document=False,
                ).data
            except FetchRefused as exc:
                refusals[entry.url] = str(exc)
                outcome.failed.append((entry.name, str(exc)))
                continue
            documents[entry.url] = data

        reason = _rejection(entry, data)
        if reason:
            outcome.failed.append((entry.name, reason))
            continue

        _write(entry.target, data)
        outcome.restored.append(entry.name)
        logger.info("restored %s (%d bytes)", entry.name, len(data))

    return outcome


def summary(outcome: Outcome) -> str:
    """The lines an operator reads: the counts, then what could not be fixed."""
    lines = [
        f"in place: {len(outcome.present)}",
        f"restored: {len(outcome.restored)}",
        f"failed:   {len(outcome.failed)}",
        f"no recorded source: {len(outcome.unrecorded)}",
    ]
    for name, reason in outcome.failed:
        lines.append(f"  FAILED {name}: {reason}")
    if outcome.unrecorded:
        lines.append("  these documents have no address in any manifest:")
        for name in outcome.unrecorded[:20]:
            lines.append(f"    {name}")
        if len(outcome.unrecorded) > 20:
            lines.append(f"    ... and {len(outcome.unrecorded) - 20} more")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.corpusrestore",
        description="Restore the collected corpus from the addresses the manifests record.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="restore one manufacturer only (a substring of its name)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be fetched and fetch nothing",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="the corpus directory (default: DATA_DIR from the environment)",
    )
    args = parser.parse_args(argv)

    outcome = restore(
        Path(args.data_dir) if args.data_dir else None,
        only=args.only,
        dry_run=args.dry_run,
    )
    print(summary(outcome))
    if outcome.missing:
        print(
            "\nThe corpus is incomplete. Ingestion will read what is here and "
            "the report will say which manufacturers it could not answer for.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI entry point
    raise SystemExit(main())
