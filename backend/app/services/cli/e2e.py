"""Tender to report in one command (D-112).

    make e2e TENDER=path/to/tender.pdf        # upload it, read it, match it
    make e2e TENDER=<tender_id>               # match a tender already read
    make e2e TENDER=<id> ARGS=--check         # only say what is missing
    make e2e TENDER=<id> ARGS="--manufacturers delval_flow,Flowserve"

The command is the chain the API runs, in the foreground and in one process:
ingest the corpus if the database has not, embed the chunks that have no
vector, read the tender, run the matching, write the report. Every step checks
its own precondition first and the command stops at the first one it cannot
meet, with the reason and the command that would fix it - so a machine with no
model configured says that, rather than creating a run that sits at `queued`
forever (D-108) or hanging on a download nobody asked for.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.llm import config_error
from app.services.output.runs import needs_model

logger = get_logger(__name__)

# The steps, in the order they are taken. A step already done is `skipped`,
# which is a different thing from `blocked`.
STEP_CORPUS = "corpus"
STEP_EMBEDDING = "embedding"
STEP_TENDER = "tender"
STEP_MATCHING = "matching"
STEP_REPORT = "report"


@dataclass
class Step:
    """One step of the chain: what it did, or why it did not."""

    name: str
    state: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.state == "failed"


@dataclass
class Facts:
    """What is on this machine. Read without a model call or a download."""

    documents: int = 0
    chunks: int = 0
    manufacturers: int = 0
    files_on_disk: int = 0
    model_reason: Optional[str] = None
    # Which model, at which host, a run would call. Named because a key for
    # one provider and a base URL for another is a refusal, not a match.
    model_label: str = ""
    ocr_reason: Optional[str] = None
    tender_status: Optional[str] = None
    requirements: int = 0
    needs_model: bool = False


@dataclass
class E2EResult:
    """The outcome of one end-to-end attempt."""

    ok: bool = False
    steps: list[Step] = field(default_factory=list)
    tender_id: Optional[str] = None
    run_id: Optional[str] = None
    files: dict[str, str] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    facts: Optional[Facts] = None
    check_only: bool = False

    @property
    def exit_code(self) -> int:
        """0 completed or nothing to object to, 1 a step failed, 2 blocked first.

        A `--check` that finds the machine ready is a success: the point of the
        code is to tell a script whether a run would go.
        """
        if self.ok:
            return 0
        if self.check_only:
            return 2 if self.blockers else 0
        if self.blockers and not any(step.failed for step in self.steps):
            return 2
        return 1

    def as_text(self) -> str:
        marks = {"done": "done", "skipped": "already", "failed": "FAILED", "blocked": "blocked"}
        lines: list[str] = []
        if self.facts is not None:
            facts = self.facts
            lines.append(
                f"  corpus  {facts.documents} document(s), {facts.chunks} chunk(s) "
                f"from {facts.manufacturers} maker(s); {facts.files_on_disk} file(s) on disk"
            )
            model_line = facts.model_reason or "configured"
            if not facts.model_reason and facts.model_label:
                model_line = f"configured: {facts.model_label}"
            lines.append(f"   model  {model_line}")
            if facts.ocr_reason:
                lines.append("     ocr  not available here; see the reason below")
        lines += [
            f"{marks.get(step.state, step.state):>8}  {step.name}: {step.detail}".rstrip(": ")
            for step in self.steps
        ]
        lines += [f" blocked  {reason}" for reason in self.blockers]
        lines += [f"     next  {hint}" for hint in self.next_steps]
        if not self.blockers and not self.steps and self.facts is not None:
            lines.append("     next  everything is in place: run the command without --check")
        if self.tender_id:
            lines.append(f"  tender  {self.tender_id}")
        if self.run_id:
            lines.append(f"     run  {self.run_id}")
        lines += [f"  report  {fmt}: {path}" for fmt, path in self.files.items()]
        return "\n".join(lines)


def assess(facts: Facts) -> tuple[list[str], list[str]]:
    """Why a run cannot start here, and what would change that.

    Pure: it reads what was measured and decides nothing about the world.
    """
    blockers: list[str] = []
    next_steps: list[str] = []

    if facts.documents == 0:
        if facts.files_on_disk == 0:
            blockers.append(
                "the corpus is empty: the database holds no document and neither does the data directory"
            )
            next_steps.append("make corpus-restore    # refetch the addresses the manifests record")
            next_steps.append("make collect           # gather the corpus afresh")
        else:
            next_steps.append(f"this command ingests the {facts.files_on_disk} document(s) on disk")

    if facts.documents and facts.chunks == 0:
        next_steps.append("this command embeds the chunks of the ingested corpus")

    if facts.model_reason:
        if facts.requirements == 0 or facts.needs_model:
            blockers.append(f"no model is configured: {facts.model_reason}")
            next_steps.append(
                "set LLM_API_KEY, LLM_MODEL and LLM_BASE_URL in .env - any OpenAI-compatible endpoint"
            )
        else:
            next_steps.append(
                f"no model is configured, and none is needed: all {facts.requirements} "
                "clause(s) of this tender are non-technical"
            )

    if facts.tender_status in ("queued", "processing"):
        next_steps.append("this command reads the tender now")
    elif facts.tender_status == "failed":
        next_steps.append("the stored tender failed to read; this command reads it again")

    if facts.ocr_reason:
        # Not a blocker: most tenders arrive with a text layer. It is said
        # anyway, because a scanned tender reads as a pile of failed pages
        # here, and a failed page is not a silent one (AGENTS #3).
        next_steps.append(f"OCR is unavailable: {facts.ocr_reason}")

    return blockers, next_steps


def measure(session: Session, *, tender_id: Optional[str] = None) -> Facts:
    """Read the facts `assess` needs. No download, no model call."""
    from app.repositories.corpus import list_manufacturers
    from app.repositories.tender import TenderRepo
    from app.services.tender.parse import iter_csv_corpus, iter_manifest_documents, tesseract_missing

    makers = list_manufacturers(session)
    settings = get_settings()
    facts = Facts(
        documents=sum(m["documents"] for m in makers),
        chunks=sum(m["chunks"] for m in makers),
        manufacturers=len(makers),
        model_reason=config_error(),
        model_label=f"{settings.llm_model} at {settings.llm_base_url}",
        ocr_reason=tesseract_missing(),
    )

    data_dir = Path(settings.data_dir)
    # Distinct files: more than one manifest lists the same bytes, and a
    # preflight that counted listings would promise work that is already done
    # (D-118).
    facts.files_on_disk = len(
        {
            path
            for path, _entry in list(iter_manifest_documents(data_dir / "manufacturers"))
            + list(iter_csv_corpus(data_dir / "catalogue"))
        }
    )

    if tender_id:
        repo = TenderRepo(session)
        tender = repo.get_tender(tender_id)
        if tender is not None:
            facts.tender_status = tender.get("status")
            requirements = repo.list_requirements(tender_id)
            facts.requirements = len(requirements)
            facts.needs_model = needs_model(requirements)

    return facts


def _stored_id(session: Session, reference: str) -> Optional[str]:
    """The id of the tender the reference names, when it is already stored.

    Read-only, so `--check` can answer about a tender without writing anything.
    """
    from app.repositories.tender import TenderRepo

    repo = TenderRepo(session)
    if repo.get_tender(reference) is not None:
        return reference
    directory = Path(reference)
    if directory.is_dir() and repo.get_tender(directory.name) is not None:
        return directory.name
    return None


def _corpus(session: Session, facts: Facts) -> list[Step]:
    """Ingest and seed the corpus, but only what is not already there."""
    from app.repositories.aliases import seed_aliases, seed_attributes
    from app.services.corpus.seed import remove_stale_products, seed_corpus
    from app.services.corpus.ingest import ingest_corpus

    if facts.documents:
        return [
            Step(
                STEP_CORPUS,
                "skipped",
                f"{facts.documents} document(s) from {facts.manufacturers} maker(s) are already in",
            )
        ]

    seed_attributes(session)
    seed_aliases(session)
    report = ingest_corpus(Path(get_settings().data_dir), session)
    seeded = seed_corpus(session)
    stale = remove_stale_products(session)
    session.commit()

    if report.documents == 0 and report.skipped_unchanged == 0:
        return [Step(STEP_CORPUS, "failed", f"nothing was read from {get_settings().data_dir}")]
    detail = f"{report.documents} document(s), {report.chunks} chunk(s), {seeded.products} product(s)"
    if report.incomplete:
        detail += f", {len(report.incomplete)} unreadable"
    if report.failed:
        detail += f", {report.failed} failed"
    return [Step(STEP_CORPUS, "done", detail)]


def _embedding(session: Session, facts: Facts) -> Step:
    """Embed the chunks that have no vector. The embedding model is local."""
    from app.services.embeddings import embed_corpus

    if facts.documents == 0:
        return Step(STEP_EMBEDDING, "blocked", "there is no corpus to embed")
    if facts.chunks == 0 and facts.documents:
        # The documents are in but produced no text: a corpus of scanned pages
        # with no OCR is exactly this, and it is not silently "embedded".
        return Step(STEP_EMBEDDING, "failed", "the corpus has no chunk to embed")

    report = embed_corpus(session)
    detail = f"{report.embedded} embedded, {report.skipped} already done"
    if report.failed:
        detail += f", {report.failed} failed"
    return Step(STEP_EMBEDDING, "failed" if report.failed else "done", detail)


def _tender(session: Session, reference: str) -> tuple[Step, str]:
    """Have the tender read, or say that it already is."""
    from app.repositories.tender import TenderRepo
    from app.schemas.enums import TenderStatus
    from app.services.tender.upload import ingest, store_upload

    stored = _stored_id(session, reference)
    if stored is None:
        path = Path(reference)
        if not (path.is_file() and path.suffix.lower() == ".pdf"):
            raise ValueError(
                f"'{reference}' is neither a stored tender, a tender directory nor a PDF"
            )
        uploaded = store_upload(session, filename=path.name, data=path.read_bytes())
        session.commit()
        stored = uploaded.tender_id

    row = TenderRepo(session).get_tender(stored) or {}
    if row.get("status") == TenderStatus.DONE.value:
        return Step(STEP_TENDER, "skipped", "already read"), stored

    status = ingest(stored)
    state = "done" if status == TenderStatus.DONE.value else "failed"
    return Step(STEP_TENDER, state, f"status {status}"), stored


def _matching(
    session: Session, tender_id: str, manufacturers: Optional[list[str]] = None
) -> tuple[Step, str]:
    """Create the run and execute it in this process."""
    from app.services.output.runs import create_run, execute_run

    run_id = create_run(session, tender_id, manufacturers)
    session.commit()
    progress = execute_run(run_id, session=session)
    detail = (
        f"{progress.results} result(s) for {len(progress.manufacturers)} maker(s), "
        f"{progress.llm_calls} model call(s)"
    )
    if progress.status == "rate_limited":
        # A stopped run is not a failed one: what it found is committed, and
        # the same command picks up the pairs still missing (D-110). The step
        # still fails, because a half-read tender is not a report.
        return Step(
            STEP_MATCHING,
            "failed",
            f"stopped: {progress.error}; {progress.results} result(s) kept. "
            "Run the same command again to continue where it stopped.",
        ), run_id
    if progress.status != "done":
        return Step(STEP_MATCHING, "failed", f"{progress.status}: {progress.error or detail}"), run_id
    return Step(STEP_MATCHING, "done", detail), run_id


def _report(session: Session, run_id: str, out_dir: Path) -> tuple[Step, dict]:
    """Write the three report formats beside one another."""
    from app.services.output.report import build_report, render_csv, render_html

    report = build_report(session, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for fmt, text in (
        ("json", json.dumps(report, indent=2)),
        ("html", render_html(report)),
        ("csv", render_csv(report)),
    ):
        path = out_dir / f"report-{run_id}.{fmt}"
        path.write_text(text, encoding="utf-8")
        written[fmt] = str(path)
    return Step(STEP_REPORT, "done", f"3 file(s) in {out_dir}"), written


def _needs_a_model(session: Session, tender_id: str) -> Optional[str]:
    """Why this tender cannot be matched without a model, or None.

    A tender whose clauses are all commercial is answered from their types
    alone, so it is not refused for want of a key (D-108).
    """
    from app.repositories.tender import TenderRepo

    if needs_model(TenderRepo(session).list_requirements(tender_id)):
        return config_error()
    return None


def _run(
    session: Session,
    reference: str,
    *,
    out_dir: Optional[Path],
    check_only: bool,
    manufacturers: Optional[list[str]] = None,
) -> E2EResult:
    result = E2EResult(check_only=check_only)
    try:
        stored = _stored_id(session, reference)
    except Exception as exc:  # noqa: BLE001 - a database that will not answer is a blocker
        result.blockers.append(f"the database could not be read: {type(exc).__name__}: {exc}")
        result.next_steps.append("is PostgreSQL up? `docker compose up -d`")
        return result

    result.tender_id = stored
    facts = measure(session, tender_id=stored)
    result.facts = facts
    result.blockers, result.next_steps = assess(facts)
    if check_only:
        return result
    if result.blockers:
        return result

    try:
        result.steps += _corpus(session, facts)
        if any(step.failed for step in result.steps):
            return result
        embedding = _embedding(session, facts)
        result.steps.append(embedding)
        if embedding.failed:
            return result

        tender_step, tender_id = _tender(session, reference)
        result.steps.append(tender_step)
        result.tender_id = tender_id
        if tender_step.failed:
            return result

        # The tender is read now, so the model is asked about the clauses that
        # were actually found rather than about the file in general.
        reason = _needs_a_model(session, tender_id)
        if reason:
            result.blockers.append(f"this tender's clauses are technical and no model is configured: {reason}")
            result.next_steps.append("set LLM_API_KEY, LLM_MODEL and LLM_BASE_URL in .env")
            return result

        matching_step, run_id = _matching(session, tender_id, manufacturers)
        result.steps.append(matching_step)
        result.run_id = run_id
        if matching_step.failed:
            return result

        directory = Path(out_dir) if out_dir else Path(get_settings().data_dir) / "e2e"
        report_step, written = _report(session, run_id, directory)
        result.steps.append(report_step)
        result.files = written
        result.ok = not report_step.failed
    except Exception as exc:  # noqa: BLE001 - the step is reported, not raised past the command
        logger.exception("the end-to-end run stopped")
        result.steps.append(Step("error", "failed", f"{type(exc).__name__}: {exc}"))
    return result


def run_end_to_end(
    reference: str,
    *,
    session: Optional[Session] = None,
    out_dir: Optional[Path] = None,
    check_only: bool = False,
    manufacturers: Optional[list[str]] = None,
) -> E2EResult:
    """Take the whole chain, or stop at the first step that cannot be taken."""
    if session is not None:
        return _run(
            session,
            reference,
            out_dir=out_dir,
            check_only=check_only,
            manufacturers=manufacturers,
        )

    from app.repositories.db import session_scope

    with session_scope() as own:
        return _run(
            own,
            reference,
            out_dir=out_dir,
            check_only=check_only,
            manufacturers=manufacturers,
        )


def preflight(session: Session, *, tender_ref: Optional[str] = None) -> dict:
    """What a caller needs to know before it asks for a run (PRD 11's status).

    The same measurement the command makes, as data: the corpus, the model,
    and what a run would need first.
    """
    stored = _stored_id(session, tender_ref) if tender_ref else None
    facts = measure(session, tender_id=stored)
    blockers, next_steps = assess(facts)
    return {
        "corpus": {
            "documents": facts.documents,
            "chunks": facts.chunks,
            "manufacturers": facts.manufacturers,
            "files_on_disk": facts.files_on_disk,
        },
        "model": {
            "configured": facts.model_reason is None,
            "reason": facts.model_reason,
            "target": facts.model_label,
        },
        "ocr": {"available": facts.ocr_reason is None, "reason": facts.ocr_reason},
        "tender": {
            "reference": tender_ref,
            "stored": stored is not None,
            "tender_id": stored,
            "status": facts.tender_status,
            "requirements": facts.requirements,
            "needs_model": facts.needs_model,
        },
        "can_run": not blockers,
        "why_not": blockers,
        "next_steps": next_steps,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.cli.e2e",
        description="Take one tender from file to report in one command.",
    )
    parser.add_argument("tender", help="a tender PDF, a tender directory, or a stored tender id")
    parser.add_argument(
        "--check",
        action="store_true",
        help="say what is missing, and change nothing",
    )
    parser.add_argument("--out", default=None, help="write the report here (default: DATA_DIR/e2e)")
    parser.add_argument(
        "--manufacturers",
        default=None,
        help="comma-separated names to compare against (default: the whole corpus)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    from app.core.logging import configure_logging

    configure_logging(settings.log_level)

    manufacturers = None
    if args.manufacturers:
        manufacturers = [name.strip() for name in args.manufacturers.split(",") if name.strip()]

    result = run_end_to_end(
        args.tender,
        out_dir=Path(args.out) if args.out else None,
        check_only=args.check,
        manufacturers=manufacturers,
    )
    print(result.as_text())
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - the CLI entry point
    raise SystemExit(main())
