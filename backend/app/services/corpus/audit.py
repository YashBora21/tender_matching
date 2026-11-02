"""The corpus audit: what each tender gave, and what went wrong.

The audit runs the pipeline over a directory of tenders and reports, for each
one, whether its specification and its bill of quantities were found, how many
items and requirements it produced, and why it failed if it did. The failure
causes are then ranked, so the thing that goes wrong most often is the thing
that is looked at first.

Nothing here is tender-specific: the directory is the input, and every
tender in it is treated the same way.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.logging import get_logger
from app.schemas.enums import TenderStatus
from app.services.tender.collect import read_manifest
from app.services.tender.pipeline import PipelineResult, resolve_roles, run_pipeline

logger = get_logger(__name__)


@dataclass
class TenderAudit:
    """What one tender gave."""

    tender_id: str
    filename: str
    category: str = "unknown"
    spec_found: bool = False
    boq_found: bool = False
    attachments: int = 0
    attachments_failed: int = 0
    items: int = 0
    requirements: int = 0
    # The pages of the specification that were read. A tender with none was
    # not read, whatever its requirements say.
    pages_read: int = 0
    # The reason the tender could not be read, when it could not.
    failure: Optional[str] = None
    llm_calls: int = 0
    llm_cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0
    # True when the provider's limit stopped the extraction. The stage is
    # resumable, so this is not a failure of reading. `rate_limited_detail`
    # carries what the provider said, so the next run knows when to resume
    # (D9).
    rate_limited: bool = False
    rate_limited_detail: Optional[str] = None
    stages: dict = field(default_factory=dict)


@dataclass
class AuditReport:
    """The audit of a whole corpus."""

    tenders: list[TenderAudit] = field(default_factory=list)
    llm_calls: int = 0
    llm_cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_retries: int = 0

    @property
    def failure_causes(self) -> list[tuple[str, int]]:
        """The failure causes, most common first."""
        counts = Counter(
            tender.failure for tender in self.tenders if tender.failure
        )
        return counts.most_common()

    @property
    def totals(self) -> dict:
        return {
            "tenders": len(self.tenders),
            "attachments": sum(t.attachments for t in self.tenders),
            "attachments_failed": sum(t.attachments_failed for t in self.tenders),
            "items": sum(t.items for t in self.tenders),
            "requirements": sum(t.requirements for t in self.tenders),
            "tenders_with_requirements": sum(1 for t in self.tenders if t.requirements),
            "tenders_failed": sum(1 for t in self.tenders if t.failure),
            "tenders_rate_limited": sum(1 for t in self.tenders if t.rate_limited),
        }

    def as_table(self) -> str:
        """The per-tender table."""
        header = (
            f"{'tender':44} {'cat':9} {'spec':5} {'boq':4} {'att':4} "
            f"{'items':6} {'reqs':5} {'calls':6} failure"
        )
        lines = [header, "-" * len(header)]
        for tender in self.tenders:
            lines.append(
                f"{tender.filename[:42]:44} {tender.category[:8]:9} "
                f"{'yes' if tender.spec_found else 'no':5} "
                f"{'yes' if tender.boq_found else 'no':4} "
                f"{tender.attachments:4} {tender.items:6} {tender.requirements:5} "
                f"{tender.llm_calls:6} "
                f"{tender.failure or ('rate_limited' if tender.rate_limited else '')}"
            )
        return "\n".join(lines)

    def as_summary(self) -> str:
        """The totals and the ranked failure causes."""
        totals = self.totals
        lines = ["", "TOTALS"]
        for key, value in totals.items():
            lines.append(f"  {key:28} {value}")
        lines.append(f"  {'llm_calls':28} {self.llm_calls}")
        lines.append(f"  {'llm_cache_hits':28} {self.llm_cache_hits}")
        lines.append(f"  {'prompt_tokens':28} {self.prompt_tokens}")
        lines.append(f"  {'completion_tokens':28} {self.completion_tokens}")
        lines.append(f"  {'llm_retries':28} {self.llm_retries}")

        causes = self.failure_causes
        lines.append("")
        if causes:
            lines.append("FAILURE CAUSES (most common first)")
            for cause, count in causes:
                lines.append(f"  {count:4}  {cause}")
        else:
            lines.append("FAILURE CAUSES: none")
        return "\n".join(lines)


def audit_tender(
    tender_dir: Path,
    *,
    repo=None,
    cache=None,
    client=None,
    force: bool = False,
) -> TenderAudit:
    """Audit one tender: its attachments, its items and its requirements."""
    manifest = read_manifest(tender_dir / "manifest.json")
    name = manifest.get("tender_id") or tender_dir.name
    attachments = list(manifest.get("documents", []))

    # The database identifies a tender by the hash of its file; the manifest
    # names it. The two are reconciled here, so the pipeline works with the
    # database's id and never with a name.
    tender_id = None
    if repo is not None:
        tender_id = repo.resolve_tender(manifest, tender_dir)
    if tender_id is None:
        return TenderAudit(
            tender_id=name,
            filename=manifest.get("filename") or tender_dir.name,
            category=manifest.get("category", "unknown"),
            attachments=len(attachments),
            failure="the tender has no wrapper hash, so it cannot be recorded",
        )

    result: Optional[PipelineResult] = None
    try:
        result = run_pipeline(
            tender_id,
            tender_dir,
            attachments,
            repo=repo,
            cache=cache,
            client=client,
            force=force,
        )
    except Exception as exc:  # noqa: BLE001 - one bad tender must not stop the audit
        logger.warning("could not process %s: %s", tender_id, exc)
        return TenderAudit(
            tender_id=tender_id,
            filename=manifest.get("filename") or tender_dir.name,
            category=manifest.get("category", "unknown"),
            attachments=len(attachments),
            failure=f"{type(exc).__name__}: {str(exc)[:80]}",
        )

    roles = resolve_roles(attachments)
    if result.rate_limited and repo is not None:
        # The stage stopped at the provider's limit: the tender is neither
        # done nor failed. Recording it here keeps the row honest, because
        # the pipeline writes stages, not the tender's own state (D8).
        repo.set_status(
            tender_id,
            TenderStatus.RATE_LIMITED,
            error=result.rate_limited_detail,
        )
    return TenderAudit(
        tender_id=tender_id,
        filename=manifest.get("filename") or tender_dir.name,
        category=manifest.get("category", "unknown"),
        spec_found=roles.specification is not None,
        boq_found=roles.boq is not None,
        attachments=len(attachments),
        attachments_failed=sum(
            1
            for record in attachments
            if record.get("status") in ("failed", "restricted")
        ),
        items=len(result.items),
        requirements=len(result.requirements),
        pages_read=len(result.pages),
        failure=result.failure,
        llm_calls=result.llm_calls,
        llm_cache_hits=result.llm_cache_hits,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        llm_retries=result.llm_retries,
        rate_limited=result.rate_limited,
        rate_limited_detail=result.rate_limited_detail,
        stages={
            name: state.status for name, state in result.stages.items()
        },
    )


def audit_corpus(
    corpus_dir: Path,
    *,
    repo=None,
    cache=None,
    client=None,
    force: bool = False,
) -> AuditReport:
    """Audit every tender in a directory.

    A tender is a directory with a manifest; a directory without one is
    skipped. A tender that fails is reported and the audit carries on: one
    broken tender must not stop the corpus from being measured.
    """
    report = AuditReport()
    corpus_dir = Path(corpus_dir)
    if not corpus_dir.exists():
        return report

    for directory in sorted(corpus_dir.iterdir()):
        if not directory.is_dir():
            continue
        if not (directory / "manifest.json").exists():
            continue
        try:
            # A tender that fails takes its own work down with it and
            # nothing else: the failure is recorded, and the audit carries
            # on. Without this, one broken tender aborts the transaction and
            # every tender after it is reported as broken too.
            with _isolated(repo):
                tender = audit_tender(
                    directory, repo=repo, cache=cache, client=client, force=force
                )
        except Exception as exc:  # noqa: BLE001 - one bad tender must not stop the audit
            logger.warning("could not audit %s: %s", directory, exc)
            tender = TenderAudit(
                tender_id=directory.name,
                filename=directory.name,
                failure=f"{type(exc).__name__}: {str(exc)[:80]}",
            )
        report.tenders.append(tender)
        report.llm_calls += tender.llm_calls
        report.llm_cache_hits += tender.llm_cache_hits
        report.prompt_tokens += tender.prompt_tokens
        report.completion_tokens += tender.completion_tokens
        report.llm_retries += tender.llm_retries
    return report


@contextmanager
def _isolated(repo):
    """A transaction of its own, so one tender's failure does not abort the
    others.

    A savepoint is not enough: the error often arrives when the work is
    written, after the savepoint has closed, and the whole transaction is
    then unusable. A separate transaction is thrown away with the tender
    that caused it.
    """
    if repo is None:
        yield
        return
    session = repo.session
    # The session may already hold a transaction from an earlier read, so it
    # is rolled back first: what follows is the tender's own work.
    if session.in_transaction():
        session.rollback()
    transaction = session.begin()
    try:
        yield
        transaction.commit()
    except Exception:
        transaction.rollback()
        raise
