"""The scoped reset planner and executor, against synthetic fixtures.

Nothing here touches a database. The repository is a fake with the same
surface as `SqlResetRepo`, and the evaluation references are files written in
a temporary directory. Every tender id, hash and filename in this file is
synthetic and labelled as such.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.cli import reset
from app.services.cli.reset import (
    NEVER_DELETED,
    ResetPlanner,
    ResetRefused,
    collect_protection,
    execute_reset,
)

# Synthetic identity, as a UUID-shaped placeholder.
TENDER_A = "aaaaaaaa-1111-2222-3333-444444444444"
TENDER_B = "bbbbbbbb-1111-2222-3333-444444444444"
# A third synthetic id the audit file names, matching no stored tender.
TENDER_REF_ONLY = "dddddddd-1111-2222-3333-444444444444"
TENDER_C = "cccccccc-1111-2222-3333-444444444444"  # the outside tender
HASH_A = "a" * 64
HASH_B = "b" * 64
CACHE_SHARED = "f" * 64   # its input is named by TENDER_C's requirements too
CACHE_OWNED = "e" * 64    # named only by A's requirements


class FakeResetRepo:
    """The tender side, in memory. Deletes are recorded, not run."""

    def __init__(self):
        self.tenders = [
            {"id": TENDER_A, "filename": "GeM-synthetic-A.pdf", "sha256": HASH_A},
            {"id": TENDER_B, "filename": "GeM-synthetic-B.pdf", "sha256": HASH_B},
            {"id": TENDER_C, "filename": "outside-synthetic.pdf", "sha256": "c" * 64},
        ]
        self.deletes: list[tuple[str, tuple]] = []
        self.detached_spend: list[list[str]] = []
        self.fail_on: str | None = None

    def all_tenders(self):
        return [dict(row) for row in self.tenders]

    def counts(self, tender_ids):
        assert set(tender_ids) <= {TENDER_A, TENDER_B, TENDER_C}
        return {
            "tenders": len(tender_ids),
            "attachments": 2 * len(tender_ids),
            "items": 3 * len(tender_ids),
            "requirements": 10 * len(tender_ids),
            "requirements_active": 6 * len(tender_ids),
            "requirements_superseded": 4 * len(tender_ids),
            "extraction_batches": 1 * len(tender_ids),
            "page_ledger": 8 * len(tender_ids),
            "source_tables": 1 * len(tender_ids),
            "reading_attempts": 2 * len(tender_ids),
            "runs": 1 * len(tender_ids),
            "results": 5 * len(tender_ids),
        }

    def tender_cache_entries(self, tender_ids):
        entries = [
            {"id": "cache-owned", "input_hash": CACHE_OWNED, "provenance":
                {"version": 2, "exclusively_tender": True, "usages": [{"tender_id": TENDER_A}]}},
            {"id": "cache-shared", "input_hash": CACHE_SHARED, "provenance":
                {"version": 2, "exclusively_tender": True, "usages": [{"tender_id": TENDER_A}, {"tender_id": TENDER_C}]}},
        ]
        # Both are attributed to A in this fixture.
        return entries if TENDER_A in tender_ids else []

    def cache_hashes_used_outside(self, tender_ids):
        return {CACHE_SHARED} if TENDER_A in tender_ids else set()

    def unattributed_cache_count(self):
        return 7

    def runs_with_spend(self, tender_ids):
        return 1 if TENDER_A in tender_ids else 0

    def run_ids_for(self, tender_ids):
        return [f"run-{tid[:8]}" for tid in tender_ids]

    def detach_spend(self, run_ids):
        self.detached_spend.append(list(run_ids))

    def delete_results(self, tender_ids):
        self._record("result", tuple(tender_ids))
        return 5

    def delete_tender_table(self, name, tender_ids):
        if name in NEVER_DELETED:
            raise ResetRefused(f"'{name}' is not a tender-derived table")
        if name == self.fail_on:
            raise RuntimeError("invariant failure in " + name)
        self._record(name, tuple(tender_ids))
        return 1

    def delete_cache(self, cache_ids):
        self._record("llm_cache", tuple(cache_ids))
        return len(cache_ids)

    def delete_tenders(self, tender_ids):
        self._record("tender", tuple(tender_ids))
        return len(tender_ids)

    def _record(self, name, argument):
        self.deletes.append((name, argument))


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def eval_dir(tmp_path):
    """Evaluation references: one gold file naming TENDER_A, one audit file."""
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "tender_id": TENDER_A,
                "sha256": HASH_A,
                "filename": "GeM-synthetic-A.pdf",
            }
        ),
        encoding="utf-8",
    )
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "results.json").write_text(
        json.dumps({"tenders": [TENDER_REF_ONLY]}), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def repo():
    return FakeResetRepo()


def _planner(repo, eval_dir):
    return ResetPlanner(repo, collect_protection(eval_dir))


def test_the_planner_selects_and_counts(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])

    assert [row["id"] for row in plan.selected] == [TENDER_B]
    assert plan.excluded == []
    assert plan.counts["requirements"] == 10
    assert plan.counts["requirements_superseded"] == 4
    assert plan.dry_run is True


def test_tenders_are_selected_by_id_hash_and_filename(repo):
    planner = _planner(repo, Path("/nowhere"))
    assert [row["id"] for row in planner.plan([HASH_B]).selected] == [TENDER_B]
    assert [row["id"] for row in planner.plan(["gem-synthetic-b.pdf"]).selected] == [
        TENDER_B
    ]


def test_an_unknown_reference_is_refused(repo):
    with pytest.raises(ResetRefused, match="not a stored tender"):
        _planner(repo, Path("/nowhere")).plan(["not-a-tender"])


def test_a_tender_referenced_by_an_eval_file_is_protected_by_default(repo, eval_dir):
    plan = _planner(repo, eval_dir).plan([TENDER_A, TENDER_B])

    assert [row["id"] for row in plan.selected] == [TENDER_B]
    assert plan.excluded == [
        {
            "tender_id": TENDER_A,
            "filename": "GeM-synthetic-A.pdf",
            "reason": "protected_eval_reference",
            "detail": "gold.json",
        }
    ]


def test_a_tender_whose_hash_or_filename_an_eval_file_names_is_protected(repo, tmp_path):
    # A different eval file that carries only the hash and the filename.
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "baseline.json").write_text(
        json.dumps({"source_sha256": HASH_B, "file": "GeM-synthetic-B.pdf"}),
        encoding="utf-8",
    )
    plan = _planner(repo, eval_dir).plan([TENDER_B])

    assert plan.selected == []
    assert plan.excluded[0]["detail"] == "baseline.json"


def test_protection_is_an_explicit_override(repo, eval_dir):
    plan = _planner(repo, eval_dir).plan([TENDER_A], include_protected=True)

    assert [row["id"] for row in plan.selected] == [TENDER_A]
    assert plan.excluded == []


def test_a_missing_eval_directory_protects_nothing_but_breaks_nothing(repo):
    plan = _planner(repo, Path("/nowhere") / "eval").plan([TENDER_A])
    assert [row["id"] for row in plan.selected] == [TENDER_A]


def test_only_demonstrably_owned_and_unshared_cache_is_deleted(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_A], include_protected=True)

    assert plan.cache_delete == ["cache-owned"]
    assert plan.cache_retained_shared == 1
    assert plan.cache_retained_unattributed == 7


def test_unattributed_cache_entries_are_counted_and_kept(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])

    assert plan.cache_delete == []
    assert plan.cache_retained_unattributed == 7


def test_execution_without_confirmation_is_refused_and_writes_nothing(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])

    with pytest.raises(ResetRefused, match="--yes"):
        execute_reset(plan, repo, confirm=False)

    assert repo.deletes == []
    assert repo.detached_spend == []


def test_execution_deletes_in_dependency_order(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])
    plan.dry_run = False
    session = FakeSession()

    outcome = execute_reset(plan, repo, confirm=True, session=session)

    names = [name for name, _argument in repo.deletes]
    assert names == [
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
    ]
    # The spend ledger keeps its rows; only the link to the deleted run goes.
    assert repo.detached_spend == [[f"run-{TENDER_B[:8]}"]]
    assert "llm_cache" not in names  # nothing attributable to delete here
    assert session.commits == 1
    assert session.rollbacks == 0
    assert outcome["deleted"]["tender"] == 1


def test_shared_cache_entries_survive_an_execution(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_A], include_protected=True)
    plan.dry_run = False

    execute_reset(plan, repo, confirm=True)

    cache_deletes = [argument for name, argument in repo.deletes if name == "llm_cache"]
    assert cache_deletes == [("cache-owned",)]


def test_an_invariant_failure_rolls_the_whole_scope_back(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])
    plan.dry_run = False
    repo.fail_on = "tender_item"
    session = FakeSession()

    with pytest.raises(RuntimeError, match="tender_item"):
        execute_reset(plan, repo, confirm=True, session=session)

    # The CLI's rollback is the caller's; the plan is never half-executed.
    assert session.commits == 0
    assert session.rollbacks == 1


def test_the_corpus_and_the_ledger_are_outside_the_executor(repo):
    plan = _planner(repo, Path("/nowhere")).plan([TENDER_B])
    plan.dry_run = False

    execute_reset(plan, repo, confirm=True)

    names = {name for name, _argument in repo.deletes}
    assert names.isdisjoint(set(NEVER_DELETED))


def test_unproven_legacy_cache_and_outside_classifier_usage_are_retained(repo):
    repo.cache_hashes_used_outside = lambda ids: set()
    repo.tender_cache_entries = lambda ids: [
        {'id': 'legacy', 'input_hash': CACHE_OWNED, 'provenance': {'tender_id': TENDER_A}},
        {'id': 'shared-classifier', 'input_hash': CACHE_OWNED, 'provenance': {
            'version': 2, 'exclusively_tender': True,
            'usages': [{'tender_id': TENDER_A}, {'tender_id': TENDER_C}]}},
    ]
    plan = _planner(repo, Path('/nowhere')).plan([TENDER_A])
    assert plan.cache_delete == []


def test_unknown_schema_counts_refuse_execution_before_any_delete(repo):
    plan = _planner(repo, Path('/nowhere')).plan([TENDER_B], dry_run=False)
    plan.counts['reading_attempts'] = None
    with pytest.raises(ResetRefused, match='incomplete'):
        execute_reset(plan, repo, confirm=True)
    assert repo.deletes == []


def test_nested_gold_directory_protects_its_tender(tmp_path):
    nested = tmp_path / 'gold-reviewed'
    nested.mkdir()
    (nested / 'labels.json').write_text(json.dumps({'tender_id': TENDER_A}))
    assert TENDER_A in collect_protection(tmp_path).ids
