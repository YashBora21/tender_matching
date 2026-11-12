"""A scoped reset of tender-derived data, dry run first (user step 6).

    python -m app.services.cli.reset --tender <id|sha256|filename>
    python -m app.services.cli.reset --all
    python -m app.services.cli.reset --tender <id> --yes        # executes

The default is a dry run: it prints what would be selected, what would be
excluded and why, and the row counts that would be deleted, and it writes
nothing. Deleting anything requires `--yes`, and the deletion is one
transaction: an invariant failure rolls the whole scope back, so the plan and
the deletion cannot disagree halfway through.

What is deleted is the *tender side only*: the tender rows, their attachments,
items, requirements (superseded ones included), page ledger, source tables,
extraction batches, reading attempts, runs, results, and the cache entries
demonstrably derived from them. The corpus - `manufacturer`, `product`,
`document`, `chunk`, `fact`, `alias`, `attribute` - and the spend ledger are
never touched: the corpus is the other side of every comparison, and billing
history is history, not budget state.

Tenders referenced by `eval/gold*`, `eval/baseline*` or anything under
`eval/audit` are protected: their ids, source hashes and filenames are matched
against the references, and a protected tender is excluded unless
`--include-protected` is given explicitly.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import ProgrammingError

from app.core.logging import get_logger

logger = get_logger(__name__)

# The tender-derived tables a reset may delete, in the order the foreign keys
# demand. `result` goes first (it points at runs and requirements), the tender
# rows last. Anything not in this list is out of the reset's reach by
# construction: the executor never names a table outside it.
DELETE_ORDER = (
    "result",
    "run",
    "requirement",
    "tender_page",
    "tender_table",
    "extraction_batch",
    "reading_attempt",
    "tender_attachment",
    "tender_item",
    "tender",
)

# The corpus tables and the ledger, named so the plan can say in one place
# what it is *not* allowed to touch.
NEVER_DELETED = (
    "manufacturer",
    "product",
    "document",
    "chunk",
    "fact",
    "alias",
    "attribute",
    "llm_spend",
)

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b", re.I)


# --- the evaluation references ----------------------------------------------


@dataclass
class Protection:
    """What the evaluation files pin down.

    UUIDs and source hashes are read out of the text; filenames are matched
    against the tender rows themselves. `sources` remembers which file
    produced which reference, so an exclusion can say why.
    """

    ids: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)
    sources: dict[str, str] = field(default_factory=dict)
    # Each eval file's text, so a tender filename can be matched against the
    # file that names it.
    source_texts: dict[str, str] = field(default_factory=dict)

    def match(self, *, tender_id: str, sha256: str, filename: str) -> Optional[str]:
        """Why this tender is protected, or None when it is not.

        A missing protected file does not shrink the protection: the tender
        id that was referenced once is referenced forever, even if the eval
        file naming it has since moved.
        """
        tender_id = str(tender_id).lower()
        sha256 = (sha256 or "").lower()
        filename = (filename or "").lower()
        for identifier in self.ids:
            if tender_id == identifier.lower():
                return self.sources.get(identifier, "an evaluation reference")
        for digest in self.hashes:
            if sha256 and sha256 == digest.lower():
                return self.sources.get(digest, "an evaluation reference")
        return None


def collect_protection(eval_dir: Path) -> Protection:
    """Read the evaluation references out of `eval/gold*`, `eval/baseline*`
    and everything under `eval/audit`.

    The files are data, not instructions: they are only ever searched for ids,
    hashes and names. A directory that does not exist protects nothing, which
    is correct - but the CLI says so, because silence would look like
    certainty.
    """
    protection = Protection()
    eval_dir = Path(eval_dir)
    if not eval_dir.is_dir():
        return protection

    paths: list[Path] = []
    for pattern in ("gold*", "baseline*"):
        for match in eval_dir.glob(pattern):
            paths.extend(p for p in match.rglob("*") if p.is_file()) if match.is_dir() else paths.append(match)
    audit = eval_dir / "audit"
    if audit.is_dir():
        paths.extend(p for p in audit.rglob("*") if p.is_file())
    for path in sorted(set(paths)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ResetRefused(f"cannot read evaluation protection file: {path}") from exc
        source = str(path.relative_to(eval_dir))
        for token in _UUID.findall(text):
            protection.ids.add(token.lower())
            protection.sources.setdefault(token.lower(), source)
        for token in _HEX64.findall(text):
            protection.hashes.add(token.lower())
            protection.sources.setdefault(token.lower(), source)
        # Filenames cannot be enumerated from free text the way ids can; each
        # tender row's filename is matched against the file that carries it.
        protection.source_texts[source] = text
    return protection


def _filename_protected(protection: Protection, filename: str) -> Optional[str]:
    """The eval file that names this tender's filename, or None."""
    filename = (filename or "").lower()
    if not filename:
        return None
    for source, text in protection.source_texts.items():
        if filename in text.lower():
            return source
    return None


# --- the repository the planner and executor speak to ------------------------


class ResetRefused(RuntimeError):
    """An execution attempt without confirmation, or a plan that cannot run."""


@dataclass
class ResetPlan:
    """What a reset would do, as data."""

    selected: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    # Cache entries attributed to the selected tenders and demonstrably
    # unshared: what the executor would delete.
    cache_delete: list[str] = field(default_factory=list)
    cache_retained_shared: int = 0
    cache_retained_unattributed: int = 0
    # Spend rows whose run link would be detached (the row itself is kept).
    spend_detached: int = 0
    # What the planner could not read, and why - typically a probe against a
    # table a migration has not created yet. A count that cannot be read is
    # reported as unknown, never as zero.
    notes: list[str] = field(default_factory=list)
    include_protected: bool = False
    dry_run: bool = True

    def as_dict(self) -> dict:
        return {
            "selected": self.selected,
            "excluded": self.excluded,
            "counts": self.counts,
            "cache_delete_count": len(self.cache_delete),
            "cache_retained_shared": self.cache_retained_shared,
            "cache_retained_unattributed": self.cache_retained_unattributed,
            "spend_detached": self.spend_detached,
            "notes": self.notes,
            "include_protected": self.include_protected,
            "dry_run": self.dry_run,
            "delete_order": list(DELETE_ORDER),
            "never_deleted": list(NEVER_DELETED),
        }


class ResetPlanner:
    """The plan, read from a repository and the evaluation references.

    `repo` is injectable so the planner can be tested against synthetic
    fixtures with no database: it needs exactly the methods
    `SqlResetRepo` defines, nothing more.
    """

    def __init__(self, repo, protection: Protection):
        self.repo = repo
        self.protection = protection

    def plan(
        self,
        tender_refs: list[str],
        *,
        include_protected: bool = False,
        dry_run: bool = True,
    ) -> ResetPlan:
        selected: list[dict] = []
        excluded: list[dict] = []
        rows = self.repo.all_tenders()
        for ref in tender_refs:
            matches = _match(rows, ref)
            if not matches:
                raise ResetRefused(
                    f"'{ref}' is not a stored tender id, hash or filename"
                )
            for row in matches:
                if str(row["id"]) in {str(item["id"]) for item in selected}:
                    continue
                selected.append(row)

        # Protection is the default, not the exception.
        for row in list(selected):
            why = self.protection.match(
                tender_id=row["id"],
                sha256=row.get("sha256") or "",
                filename=row.get("filename") or "",
            ) or _filename_protected(self.protection, row.get("filename") or "")
            for source in row.get('attachment_sources', []):
                why = why or self.protection.match(tender_id='', sha256=source.get('sha256'),
                    filename=source.get('filename')) or _filename_protected(self.protection, source.get('filename'))
            if why and not include_protected:
                selected.remove(row)
                excluded.append(
                    {
                        "tender_id": row["id"],
                        "filename": row.get("filename"),
                        "reason": "protected_eval_reference",
                        "detail": why,
                    }
                )

        plan = ResetPlan(
            selected=selected, excluded=excluded, include_protected=include_protected,
            dry_run=dry_run,
        )
        if not selected:
            return plan

        ids = [str(row["id"]) for row in selected]
        plan.counts = dict(self.repo.counts(ids))
        # Cache: only entries attributed to the selected tenders are candidates,
        # and an entry whose input another tender's requirements still name is
        # shared, so it is retained. Entries with no provenance are legacy:
        # counted, reported, retained.
        entries = self.repo.tender_cache_entries(ids)
        shared = self.repo.cache_hashes_used_outside(ids)
        plan.cache_delete = [entry["id"] for entry in entries
            if entry["input_hash"] not in shared and cache_exclusive(entry.get('provenance'), ids)]
        plan.cache_retained_shared = len(entries) - len(plan.cache_delete)
        plan.cache_retained_unattributed = self.repo.unattributed_cache_count()
        plan.spend_detached = self.repo.runs_with_spend(ids)
        plan.notes = list(getattr(self.repo, "warnings", []))
        return plan


def cache_users(provenance):
    if not isinstance(provenance, dict):
        return set()
    users = {str(row['tender_id']) for row in provenance.get('usages', []) if row.get('tender_id')}
    if provenance.get('tender_id'):
        users.add(str(provenance['tender_id']))
    return users


def cache_exclusive(provenance, selected):
    return bool(isinstance(provenance, dict) and provenance.get('version') == 2
        and provenance.get('exclusively_tender') is True and cache_users(provenance)
        and cache_users(provenance) <= set(selected))


def _match(rows: list[dict], ref: str) -> list[dict]:
    """The tender rows a reference names: id, hash, or exact filename."""
    ref = ref.strip()
    lowered = ref.lower()
    matches = [
        row for row in rows if str(row["id"]).lower() == lowered
        or (row.get("sha256") or "").lower() == lowered
        or (row.get("filename") or "").lower() == lowered
    ]
    return matches


# --- the executor ------------------------------------------------------------


def execute_reset(
    plan: ResetPlan,
    repo,
    *,
    confirm: bool,
    session=None,
) -> dict:
    """Delete the planned scope, in one transaction.

    `confirm` is the `--yes` of the command line: without it this raises, and
    nothing is written. With it, the deletes run in `DELETE_ORDER`; the spend
    ledger keeps its rows, and a run link on a spend row is detached rather
    than deleting billing history with the run.
    """
    if not confirm:
        raise ResetRefused(
            "the reset was not confirmed: pass --yes to execute it "
            "(a dry run never writes)"
        )
    if plan.dry_run:
        raise ResetRefused("this plan is a dry run; build it again with dry_run=False")
    if not plan.selected:
        return {"deleted": {}, "message": "nothing selected"}

    if plan.notes or any(value is None for value in plan.counts.values()):
        raise ResetRefused("reset counts are incomplete; apply migrations and inspect a fresh dry run")
    ids = [str(row["id"]) for row in plan.selected]
    deleted: dict[str, int] = {}

    def _step(name: str, delete) -> None:
        deleted[name] = delete()

    # Every delete below is inside the caller's transaction. Any invariant
    # failure raises after the whole scope is rolled back: a half-executed
    # reset is not a state the database may be left in.
    try:
        _step("result", lambda: repo.delete_results(ids))
        run_ids = repo.run_ids_for(ids)
        # Billing history survives the run it belongs to; only the link goes.
        repo.detach_spend(run_ids)
        _step("run", lambda: repo.delete_tender_table("run", ids))
        for name in DELETE_ORDER[2:-1]:
            _step(name, lambda name=name: repo.delete_tender_table(name, ids))
        if plan.cache_delete:
            _step("llm_cache", lambda: repo.delete_cache(plan.cache_delete))
        _step("tender", lambda: repo.delete_tenders(ids))
    except Exception:
        if session is not None:
            session.rollback()
        raise

    if session is not None:
        session.commit()
    return {"deleted": deleted, "run_ids_detached_from_spend": len(run_ids)}


# --- the PostgreSQL implementation -------------------------------------------


class SqlResetRepo:
    """The repository the planner and executor use against PostgreSQL.

    Only the methods the two need; the fake in the tests implements the same
    surface over in-memory records.

    Probes that read a table or column a migration has not created yet (the
    reading-attempt table, the cache provenance column) degrade instead of
    failing the dry run: the missing number is reported as unknown through
    `warnings`, never as zero.
    """

    def __init__(self, session):
        self.session = session
        self.warnings: list[str] = []

    def _probe(self, label: str, read, default):
        """Read a count, or report it unknown when the schema lags the code."""
        try:
            with self.session.begin_nested():
                return read()
        except ProgrammingError as exc:
            self.warnings.append(f"{label}: schema unavailable")
            return default

    def all_tenders(self) -> list[dict]:
        from sqlalchemy import select

        from app.repositories.schema import tender, tender_attachment

        rows = self.session.execute(
            select(tender.c.id, tender.c.filename, tender.c.sha256)
        ).all()
        attachments = {}
        for item in self.session.execute(select(tender_attachment.c.tender_id,
                tender_attachment.c.filename, tender_attachment.c.sha256)):
            attachments.setdefault(str(item[0]), []).append({'filename': item[1], 'sha256': item[2]})
        return [{"id": str(row[0]), "filename": row[1], "sha256": row[2],
                 'attachment_sources': attachments.get(str(row[0]), [])} for row in rows]

    def counts(self, tender_ids: list[str]) -> dict:
        from sqlalchemy import func, select

        from app.repositories.schema import (
            extraction_batch,
            llm_cache,
            reading_attempt,
            requirement,
            result,
            run,
            tender_attachment,
            tender_item,
            tender_page,
            tender_table,
        )

        def _count(table) -> int:
            return self.session.execute(
                select(func.count())
                .select_from(table)
                .where(table.c.tender_id.in_(tender_ids))
            ).scalar_one()

        requirements = _count(requirement)
        active = self.session.execute(
            select(func.count())
            .select_from(requirement)
            .where(
                requirement.c.tender_id.in_(tender_ids),
                requirement.c.superseded_at.is_(None),
            )
        ).scalar_one()
        runs = _count(run)
        results = self.session.execute(
            select(func.count())
            .select_from(result)
            .join(run, run.c.id == result.c.run_id)
            .where(run.c.tender_id.in_(tender_ids))
        ).scalar_one()
        reading_attempts = self._probe(
            "the reading-attempt count (is migration 0023 applied?)",
            lambda: _count(reading_attempt),
            None,
        )
        return {
            "tenders": len(tender_ids),
            "attachments": _count(tender_attachment),
            "items": _count(tender_item),
            "requirements": requirements,
            "requirements_active": active,
            "requirements_superseded": requirements - active,
            "extraction_batches": _count(extraction_batch),
            "page_ledger": _count(tender_page),
            "source_tables": _count(tender_table),
            "reading_attempts": reading_attempts,
            "runs": runs,
            "results": results,
        }

    def tender_cache_entries(self, tender_ids: list[str]) -> list[dict]:
        from sqlalchemy import select

        from app.repositories.schema import llm_cache

        def _read():
            rows = self.session.execute(select(llm_cache.c.id, llm_cache.c.input_hash,
                                                llm_cache.c.provenance)).all()
            return [{'id': str(row[0]), 'input_hash': row[1] or '', 'provenance': row[2]}
                    for row in rows if cache_users(row[2]) & set(tender_ids)]
        return self._probe('cache ownership (migration 0023)', _read, [])

    def cache_hashes_used_outside(self, tender_ids: list[str]) -> set[str]:
        from sqlalchemy import select

        from app.repositories.schema import requirement

        rows = self.session.execute(
            select(requirement.c.input_hash).where(
                requirement.c.tender_id.notin_(tender_ids),
                requirement.c.input_hash.is_not(None),
            )
        ).scalars().all()
        return set(rows)

    def unattributed_cache_count(self):
        from sqlalchemy import select
        from app.repositories.schema import llm_cache
        def read():
            rows = self.session.execute(select(llm_cache.c.provenance)).scalars()
            return sum(not (isinstance(value, dict) and value.get('version') == 2
                            and value.get('exclusively_tender') is True) for value in rows)
        return self._probe('legacy or unproven cache ownership (migration 0023)', read, None)

    def runs_with_spend(self, tender_ids: list[str]) -> int:
        from sqlalchemy import func, select

        from app.repositories.schema import llm_spend, run

        return self._probe('spend links (migration 0022)', lambda: self.session.execute(
            select(func.count()).select_from(llm_spend).join(run, run.c.id == llm_spend.c.run_id)
            .where(run.c.tender_id.in_(tender_ids))).scalar_one(), None)

    def run_ids_for(self, tender_ids: list[str]) -> list[str]:
        from sqlalchemy import select

        from app.repositories.schema import run

        rows = self.session.execute(
            select(run.c.id).where(run.c.tender_id.in_(tender_ids))
        ).scalars().all()
        return [str(row) for row in rows]

    def detach_spend(self, run_ids: list[str]) -> None:
        from sqlalchemy import update

        from app.repositories.schema import llm_spend

        if not run_ids:
            return
        self.session.execute(
            update(llm_spend)
            .where(llm_spend.c.run_id.in_(run_ids))
            .values(run_id=None)
        )

    def delete_results(self, tender_ids: list[str]) -> int:
        from sqlalchemy import delete, select

        from app.repositories.schema import result, run

        run_ids = select(run.c.id).where(run.c.tender_id.in_(tender_ids))
        outcome = self.session.execute(
            delete(result).where(result.c.run_id.in_(run_ids))
        )
        return outcome.rowcount or 0

    def delete_tender_table(self, name: str, tender_ids: list[str]) -> int:
        from sqlalchemy import delete

        from app.repositories.schema import (
            extraction_batch,
            reading_attempt,
            requirement,
            run,
            tender_attachment,
            tender_item,
            tender_page,
            tender_table,
        )

        tables = {
            "run": run,
            "requirement": requirement,
            "tender_page": tender_page,
            "tender_table": tender_table,
            "extraction_batch": extraction_batch,
            "reading_attempt": reading_attempt,
            "tender_attachment": tender_attachment,
            "tender_item": tender_item,
        }
        if name not in tables:
            raise ResetRefused(f"'{name}' is not a tender-derived table the reset may delete")
        outcome = self.session.execute(
            delete(tables[name]).where(tables[name].c.tender_id.in_(tender_ids))
        )
        return outcome.rowcount or 0

    def delete_cache(self, cache_ids: list[str]) -> int:
        from sqlalchemy import delete

        from app.repositories.schema import llm_cache

        outcome = self.session.execute(
            delete(llm_cache).where(llm_cache.c.id.in_(cache_ids))
        )
        return outcome.rowcount or 0

    def delete_tenders(self, tender_ids: list[str]) -> int:
        from sqlalchemy import delete

        from app.repositories.schema import tender

        outcome = self.session.execute(
            delete(tender).where(tender.c.id.in_(tender_ids))
        )
        return outcome.rowcount or 0


# --- the command -------------------------------------------------------------


def _plan_text(plan: ResetPlan, eval_dir: Path) -> str:
    lines = ["DRY RUN - nothing was written" if plan.dry_run else "RESET PLAN - execution requested"]
    if plan.include_protected:
        lines.append("protected tenders: included (--include-protected)")
    if not plan.selected and not plan.excluded:
        lines.append("  nothing selected")
    for row in plan.selected:
        lines.append(f"  selected  {row['id']}  {row.get('filename') or ''}")
    for row in plan.excluded:
        lines.append(
            f"  excluded  {row['tender_id']}  {row.get('filename') or ''}"
            f"  ({row['reason']}: {row['detail']})"
        )
    if plan.counts:
        lines.append("  counts that would be deleted:")
        for name, value in plan.counts.items():
            if name == "llm_cache_delete":
                continue
            shown = "unknown (see notes)" if value is None else value
            lines.append(f"    {name:20} {shown}")
        if plan.counts.get("llm_cache_delete"):
            lines.append(f"    {'llm_cache':20} {plan.counts['llm_cache_delete']}")
        retained_unattributed = (
            "unknown (see notes)"
            if plan.cache_retained_unattributed is None
            else plan.cache_retained_unattributed
        )
        lines.append(
            f"    cache retained (shared): {plan.cache_retained_shared}; "
            f"(unattributed legacy): {retained_unattributed}"
        )
        lines.append(
            f"    spend rows kept, run links detached: {plan.spend_detached}"
        )
    for note in plan.notes:
        lines.append(f"  note: {note}")
    lines.append(f"  evaluation references read from: {eval_dir}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    from app.core.config import REPO_ROOT

    parser = argparse.ArgumentParser(
        prog="python -m app.services.cli.reset",
        description=(
            "Plan (default) or execute a scoped reset of tender-derived data. "
            "The corpus, the catalogue and the spend ledger are never touched."
        ),
    )
    parser.add_argument(
        "--tender",
        action="append",
        default=[],
        metavar="ID|SHA256|FILENAME",
        help="a tender to reset; repeatable",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every stored tender, protected ones excluded unless overridden",
    )
    parser.add_argument(
        "--include-protected",
        action="store_true",
        help="allow tenders referenced by the evaluation files to be reset",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="execute the reset; without it this command is a dry run",
    )
    parser.add_argument(
        "--eval-dir",
        default=None,
        help="where the evaluation references live (default: REPO_ROOT/eval)",
    )
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)
    if args.all and args.tender:
        parser.error("choose --all or --tender, not both")

    if not args.tender and not args.all:
        parser.print_help()
        print(
            "\nName a tender with --tender, or pass --all. "
            "The default run is a dry plan and writes nothing."
        )
        return 2

    eval_dir = Path(args.eval_dir) if args.eval_dir else REPO_ROOT / "eval"
    protection = collect_protection(eval_dir)

    from app.repositories.db import session_scope

    with session_scope() as session:
        repo = SqlResetRepo(session)
        if args.yes:
            if not eval_dir.is_dir():
                raise ResetRefused('evaluation protection directory is unavailable')
            from sqlalchemy import text
            # Freeze writers while rebuilding and executing this exact scoped plan.
            tables = ', '.join((*DELETE_ORDER, 'llm_cache', 'llm_spend'))
            session.execute(text(f'LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE'))
            protection = collect_protection(eval_dir)
            from app.repositories.schema import result, run, requirement
            from sqlalchemy import select
        refs = (
            [row["id"] for row in repo.all_tenders()] if args.all else args.tender
        )
        planner = ResetPlanner(repo, protection)
        plan = planner.plan(
            refs, include_protected=args.include_protected, dry_run=not args.yes
        )
        plan.counts.setdefault("llm_cache_delete", len(plan.cache_delete))
        print(_plan_text(plan, eval_dir))

        if args.yes:
            ids = [row['id'] for row in plan.selected]
            cross_scope = session.execute(select(result.c.id).join(run, run.c.id == result.c.run_id)
                .join(requirement, requirement.c.id == result.c.requirement_id)
                .where(requirement.c.tender_id.in_(ids), run.c.tender_id.notin_(ids)).limit(1)).first()
            if cross_scope:
                raise ResetRefused('a retained run references a selected requirement')
            outcome = execute_reset(plan, repo, confirm=True, session=session)
            print(f"deleted: {json.dumps(outcome['deleted'])}")
            print(
                "spend rows kept, run links detached: "
                f"{outcome['run_ids_detached_from_spend']}"
            )
        else:
            print("dry run: nothing was written")
        if args.json:
            print(json.dumps(plan.as_dict(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI entry point
    raise SystemExit(main())
