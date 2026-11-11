"""The corpus runner: process every tender, and report what happened.

Run it over the tenders that have been collected:

    python -m app.services.corpus.run --audit

A second run over an unchanged corpus makes no downloads and no LLM calls:
every stage is keyed by the inputs it read, and nothing changed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.repositories.tender import LLMCache, TenderRepo
from app.services.corpus.audit import AuditReport, audit_corpus
from app.services.tender.collect import collect_tender, read_manifest, write_manifest

logger = get_logger(__name__)


def _corpus_dir() -> Path:
    return get_settings().data_dir / "tenders"


def _wrappers(corpus_dir: Path) -> list[Path]:
    """The tender wrapper PDFs in a corpus."""
    if not corpus_dir.exists():
        return []
    return [
        path
        for path in sorted(corpus_dir.glob("*.pdf"))
        if not path.is_dir()
    ]


def collect_missing(corpus_dir: Path) -> int:
    """Fetch the attachments of the tenders that have no manifest yet.

    A tender whose attachments are already frozen is not fetched again: the
    files on disk are the cache. The manifest is rewritten either way, so a
    tender that was collected before the manifest grew a field is brought up
    to date without a download.
    """
    collected = 0
    for wrapper in _wrappers(corpus_dir):
        directory = corpus_dir / wrapper.stem
        previous = read_manifest(directory / "manifest.json")
        manifest = collect_tender(
            wrapper.stem, wrapper, directory, previous=previous
        )
        write_manifest(manifest, directory)
        collected += 1
    return collected


def process_corpus(
    corpus_dir: Path,
    *,
    session,
    force: bool = False,
    audit_only: bool = False,
) -> AuditReport:
    """Process every tender in a corpus and audit the result.

    The session is not wrapped in a transaction here: each tender runs in
    one of its own, so a tender that fails is rolled back alone and the
    rest of the corpus is still measured.
    """
    repo = TenderRepo(session)
    cache = LLMCache(session)

    if not audit_only:
        collect_missing(corpus_dir)

    report = audit_corpus(
        corpus_dir, repo=repo, cache=cache, client=None, force=force
    )
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Process the tender corpus.")
    parser.add_argument(
        "--corpus-dir",
        default=None,
        help="the tenders to process; the default is DATA_DIR/tenders",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="only report what the corpus holds, without processing it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="process every stage, even the ones that have not changed",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the report as JSON",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level)
    corpus_dir = Path(args.corpus_dir) if args.corpus_dir else _corpus_dir()

    # The session is not wrapped in a transaction: each tender runs in one
    # of its own, so a tender that fails does not take the corpus with it.
    from sqlalchemy.orm import Session

    from app.repositories.db import build_engine

    engine = build_engine()
    session = Session(engine, future=True)
    try:
        report = process_corpus(
            corpus_dir, session=session, force=args.force, audit_only=args.audit
        )
    finally:
        session.close()
        engine.dispose()

    if args.json:
        print(
            json.dumps(
                {
                    "totals": report.totals,
                    "llm_calls": report.llm_calls,
                    "llm_cache_hits": report.llm_cache_hits,
                    "prompt_tokens": report.prompt_tokens,
                    "completion_tokens": report.completion_tokens,
                    "llm_retries": report.llm_retries,
                    "failure_causes": report.failure_causes,
                    "tenders": [
                        {
                            "tender": tender.tender_id,
                            "category": tender.category,
                            "spec_found": tender.spec_found,
                            "boq_found": tender.boq_found,
                            "attachments": tender.attachments,
                            "items": tender.items,
                            "requirements": tender.requirements,
                            "llm_calls": tender.llm_calls,
                            "prompt_tokens": tender.prompt_tokens,
                            "completion_tokens": tender.completion_tokens,
                            "llm_retries": tender.llm_retries,
                            "rate_limited": tender.rate_limited,
                            "rate_limited_detail": tender.rate_limited_detail,
                            "stages": tender.stages,
                            "failure": tender.failure,
                        }
                        for tender in report.tenders
                    ],
                },
                indent=2,
            )
        )
    else:
        print(report.as_table())
        print(report.as_summary())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
