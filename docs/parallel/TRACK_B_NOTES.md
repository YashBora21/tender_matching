# Track B handoff notes — 2026-11-12

Track B (user steps 5 and 6) implemented in the shared checkout, **all changes
unstaged**. Codex (Track A) owns integration and commits. Nothing was committed,
staged, reset, stashed or branch-switched. No migration was applied, no reset
was executed, no live tender was rerun, and no paid API call was made.

## Owned paths and what changed in each

### Step 5 — reading attempts, resume, read-again, preflight limits

| File | Change |
| --- | --- |
| `backend/app/repositories/schema.py` | New `reading_attempt` table (append-only, one row per attempt: `tender_id`, `attempt_no` unique per tender, `started_at`/`finished_at`, `status`, `stop_reason`, `reason_code`, source identity `sha256`/`filename`, `limits` JSONB, `metrics` JSONB). New nullable `provenance` JSONB column on `llm_cache`. Preserves the pre-existing spend-ledger edits in this file. |
| `backend/alembic/versions/0023_reading_attempts.py` | **New, NOT applied.** Creates `reading_attempt`, adds `llm_cache.provenance`. `down_revision = "0022_llm_spend"`. |
| `backend/app/repositories/tender.py` | `TenderRepo.start_reading_attempt / finish_reading_attempt / list_reading_attempts`. `LLMCache(session, *, tender_id=None, reading_id=None)` — the one-argument constructor and get/put signatures are unchanged (PLAN interface point 4); when provenance is given, `put()` stamps it on insert and on update. Provenance is *recorded*, never used for lookup; the last writer owns the stamp, so sharing is decided by usage, not by the stamp. |
| `backend/app/services/tender/upload.py` | `effective_limits()` — the current numeric limits read at attempt start (call/token budgets, max output tokens, retries, spend caps + caps when enabled, schema/app versions; **no model id, no endpoint**). `ingest()` opens an attempt first, writes `PROCESSING`, commits (so a later rollback cannot lose the attempt), runs the read, closes the attempt with status/stop reason/reason code/metrics. `_read()` returns a 4th element (metrics dict) and passes `reading_id` into `LLMCache`. Attempt bookkeeping is defensive: a missing table (pre-migration) or any bookkeeping failure is logged and rolled back — it never fails or changes the read. |
| `backend/app/api/tenders.py` | `POST /api/tenders/{tender_id}/read-again` (202): refuses 404 (unknown/malformed id) and 409 (already queued/processing — no double read), sets `queued`, commits, schedules `ingest` in the background. Supersedes the old reading by replacement (`replace_items`/`replace_requirements` already supersede, never destroy); runs, results, reports and previous attempt rows are untouched. |
| `backend/app/services/cli/e2e.py` | `preflight()` now returns `limits` (current effective numeric limits, via `upload.effective_limits()`) and, for a stored tender, `reading_attempts` (historical, each with its own limits — kept **separate** from the current ones). `model.target` in the HTTP payload is redacted to `"configured (identity withheld)"` (or `null` when unconfigured); the CLI's human text output still names the model because it runs on the operator's own machine. Attempt listing degrades to empty with a log line if the table does not exist yet. |
| `frontend/src/api.ts` | `readAgain(tenderId)` → `POST /api/tenders/{id}/read-again`. |
| `frontend/src/components/UploadPage.tsx` | "Read this tender again" button for `rate_limited` / `incomplete` / `failed` phases; on click it calls `readAgain`, switches to `reading`, and follows the new attempt with the stream/polling as before. No re-upload required. |
| `backend/tests/test_tenderupload.py` | One pre-existing assertion loosened: `test_an_exception_is_recorded_and_never_left_processing` now asserts `session.rollbacks >= 1` (the attempt bookkeeping may roll back once more; the fake repo there has no attempt methods, so the defensive path runs). All other tests in the file pass unchanged. |

Duplicate-upload behavior needed no change: a duplicate whose reading is
`done` is reused and no read is scheduled; a duplicate in
`failed`/`rate_limited`/`incomplete` resumes through `ingest`, which now runs
as a recorded attempt under the current limits. Old stop reasons and old
numeric limits never govern a new attempt — the pipeline reads settings live
(A-owned), and the attempt table records history, not configuration.

Completion is consumed from A's `completion_gaps()` (via `audit_tender` in
`_read` and via `settle` in the duplicate path) — no second completion gate was
invented. `item_id`, source quotes and run/report references are untouched.

### Step 6 — scoped reset, dry run first

| File | Change |
| --- | --- |
| `backend/app/services/cli/reset.py` | **New module.** `Protection` + `collect_protection(eval_dir)`: reads `eval/gold*`, `eval/baseline*`, everything under `eval/audit/` for UUIDs, 64-hex source hashes and per-file text (tender filenames are matched against the file that names them). `ResetPlanner(repo, protection)` with an injectable repo (tests use synthetic fixtures, no database); builds `ResetPlan`: selected/excluded tenders with exclusion reasons, per-table counts, attributable-cache analysis, spend detach count, and `notes` for anything it could not read. `execute_reset(plan, repo, confirm=...)` raises `ResetRefused` without confirmation or on a dry-run plan; deletes in FK order `result → run → requirement → tender_page → tender_table → extraction_batch → reading_attempt → tender_attachment → tender_item → tender`, detaching `llm_spend.run_id` before runs are deleted (billing history is kept, only the link goes). Any invariant failure rolls the whole scope back. `main()` CLI: dry run is the default; a scope selector (`--tender` repeatable by id/sha256/filename, or `--all`) is required (exit 2 otherwise); `--include-protected` is the explicit override; `--yes` executes; `--json` prints the plan as data. |
| `backend/tests/test_reset_planner.py` | 12 tests, all synthetic: protection by id/hash/filename from eval fixtures, override, unknown reference refusal, shared-cache retention, unattributed retention, refusal without `--yes`, delete order, spend detachment, rollback on invariant failure, corpus tables never in the delete set. |

Cache provenance for the reset: only entries whose `provenance.tender_id` is in
the selection **and** whose `input_hash` is not named by any requirement of a
non-selected tender are deleted. Entries with no provenance are legacy: counted
and reported, never guessed to be tender-only. `llm_spend` is in
`NEVER_DELETED` alongside the corpus tables (`manufacturer`, `product`,
`document`, `chunk`, `fact`, `alias`, `attribute`); `delete_tender_table`
refuses any name outside the tender-derived set.

## Migration 0023 — situation and plan (for integration)

- Graph is linear: `0021_requirement_history → 0022_llm_spend → 0023_reading_attempts` (verified with `alembic history`, read-only).
- `0022_llm_spend` is **untracked** (pre-existing spend-ledger work) but the local development DB already carries it; `0023` depends on it. This is a **documented** dependency, stated in the migration docstring — not a secret one. The untracked `0022` must be committed together with (or before) `0023` at integration, then `alembic upgrade head` applied. Track B did not apply anything.
- Until 0023 is applied, the system degrades gracefully: ingest skips attempt bookkeeping (logged), `preflight` omits history, and the reset dry run reports the affected counts as `unknown (see notes)` — never as zero.

## Test commands and results

Backend (from `backend/`, `..\venv\Scripts\python.exe`):

```
python -m pytest tests/test_reading_attempts.py tests/test_reading_lifecycle.py tests/test_reset_planner.py tests/test_preflight_limits.py -q
    → 27 passed
python -m pytest -m "not integration" --tb=no -p no:warnings
    → 1152 passed, 22 deselected, **1 failed** — tests/test_audit.py::
      test_a_rate_limited_tender_is_not_stored_as_done (see integration issue below)
```

Frontend (from `frontend/`):

```
npm test -- --run   → 3 files, 38 tests passed (incl. the new read-again test)
npm run build       → tsc + vite build clean
```

## Safe dry-run command and actual output

Run against the live local DB (`tender-pg-local`, 127.0.0.1:15432), **read
only** — the dry-run path executes no DELETE and no migration:

```
python -m app.services.cli.reset --all
```

Output (abridged to one exclusion line per group; ids are not secrets):

```
DRY RUN - nothing was written
  selected  e4cbbb39… GEM_9848272_Valve.pdf
  selected  70fa963c… GEM_9935793_Valve.pdf
  selected  5b68e376… GeM-Bidding-10078785.pdf
  selected  acdd1135… GeM-Bidding-9713870.pdf
  selected  7a1384ad… GeM-Bidding-8721586.pdf
  selected  3b6befdc… GeM-Bidding-9605024.pdf
  selected  4e0a9c5f… GeM-Bidding-10021878.pdf
  selected  279d6c4c… Tender_Matching_Intern_Assignment.pdf
  selected  41aaa025… GeM-Bidding-10014119.pdf
  excluded  70ec03cb… GeM-Bidding-10059225.pdf (protected_eval_reference:
              audit\ocr-comparison-2026-11-08\comparison-summary.json)
  excluded  9d62cf12… specification_for_gem_2026-07-28…pdf (protected_eval_reference:
              audit\fixed-incorpus-2026-11-08\frozen-requirements.json)
  excluded  373e9eb7… GeM-Bidding-10103860.pdf (protected_eval_reference:
              audit\parallel-implementation-2026-11-12\blocker-baseline.json)
  excluded  ec27ee5a… GeM-Bidding-10092551.pdf (protected_eval_reference: audit\ocr-comparison-2026-11-08\…)
  excluded  4154c1b2… GEM_9935572_Pump.pdf (protected_eval_reference: audit\acceptance-2026-11-11\gate2-gaps.json)
  excluded  2bc8f917… GEM_9671792_Valve.pdf (protected_eval_reference: audit\acceptance-2026-11-11\gate2-gaps.json)
  counts that would be deleted:
    tenders 9 / attachments 36 / items 99 / requirements 4012
    (active 1847, superseded 2165) / extraction_batches 38
    page_ledger 120 / source_tables 115 / reading_attempts unknown (see notes)
    runs 8 / results 1415
    cache retained (shared): 0; (unattributed legacy): unknown (see notes)
    spend rows kept, run links detached: 0
  note: the reading-attempt count …: relation "reading_attempt" does not exist
  note: the attributable cache entries …: column llm_cache.provenance does not exist
  note: the unattributed cache count …: column llm_cache.provenance does not exist
dry run: nothing was written
```

Six tenders were excluded as protected **by real references found in the eval
tree** — the protection check works against the actual files, not just in
tests. `python -m app.services.cli.reset` with no selector prints help and
exits 2.

The backup commands in `docs/parallel/OPENROUTER_TRACK_B.md` remain documented
but were **not** executed — backup creation is the user's action.

## Decisions for A to import into DECISIONS.md / WORKLOG.md

- **D (reading attempts):** one append-only `reading_attempt` row per ingest run, frozen with the effective numeric limits at start; attempt status reuses the tender's own status vocabulary plus `running`. Old stop reasons and old limits are history and never govern a new attempt; resume always reads the current settings.
- **D (attempt bookkeeping is advisory):** a failure to record or close an attempt is logged and the session recovered; it can never fail or alter the reading it describes. This keeps ingest working before migration 0023 is applied and keeps fake-repo tests compatible.
- **D (cache provenance):** `llm_cache` gains nullable `provenance {tender_id, reading_id}`; the cache key is unchanged and provenance never selects. Sharing between tenders is proven by requirement `input_hash` usage, not by the stamp (the last writer owns the stamp). Legacy unattributed entries are retained and reported.
- **D (reset scope):** deletion is limited to the tender-derived tables in `DELETE_ORDER`; the corpus tables and `llm_spend` are named `NEVER_DELETED` and the executor refuses any other table. Run-linked spend rows are detached (`run_id = NULL`), not deleted — billing history survives the run.
- **D (preflight redaction):** the HTTP preflight payload no longer names the model id or endpoint (`target` redacted); the CLI text output still does, because it prints on the operator's own machine. This applies the "no model ids/endpoints in new output" rule at the API boundary.
- **D (preflight limits separation):** current effective limits are reported as `limits`; historical per-attempt limits are `reading_attempts[]`. They are never merged.
- **D (protected tenders):** protection matches tender id, source sha256 and filename against `eval/gold*`, `eval/baseline*` and `eval/audit/**`; exclusion names the eval file that pins the tender. `--include-protected` is the only override.

## Interface notes and one integration issue for A

1. **`audit.py` breaks one unit test (A's concurrent edit, not B's).** A's new
   `validation_attempts` read in `backend/app/services/corpus/audit.py`
   (`validation_attempts=result.validation_attempts`) is unconditional, and
   `tests/test_audit.py::test_a_rate_limited_tender_is_not_stored_as_done`
   fails with `AttributeError: 'types.SimpleNamespace' object has no attribute
   'validation_attempts'`. B did not touch `audit.py` (not a B-owned file).
   Fix at integration: read it defensively (`getattr(result,
   "validation_attempts", [])`) or update the test's stub. Everything else in
   the combined working tree passes (1152 unit tests).
2. `LLMCache` stays one-argument-compatible; A's files need no change. `run.py`
   and any other caller can pass `tender_id=` when it wants provenance.
3. `run_pipeline` / `audit_tender` interfaces unchanged; B consumed
   `completion_gaps()` through `audit_tender` (existing behavior in `_read`)
   and `settle` — no completion logic was reimplemented.
4. `_read()` now returns a 4th element (metrics). It is module-private;
   `ingest()` is the only caller.

## Limitations

- `reading_attempt` lifecycle against real PostgreSQL is exercised only via
  the fake-repo unit tests and the graceful-degradation notes; the integration
  phase should run the two integration-marked ingestion suites after applying
  migrations.
- `execute_reset` was never run against a database — by design (step 6: no
  reset execution in this session). The deletion order follows the FK graph
  but only a real execution after backup can prove the whole chain; the plan
  and the executor use the same table set and the same transaction.
- Frontend has no reading-history display yet (attempts are visible via
  `/api/preflight`); the button is the minimal UI the step asked for.
- Eval protection reads text; a reference to a tender inside a binary file
  under `eval/` (none exist today) would be missed.
- The dry-run output above shows cache counts as `unknown` because migration
  0023 is not applied to the live DB; after integration applies it, the same
  command fills them in.


## Codex integration follow-up - 2026-11-13

This handoff above records B's original delivery. Integrated behavior supersedes
its last-writer provenance, CLI-identity and non-forced reread descriptions.
The merged implementation accumulates cache usages, retains unproven ownership,
uses transactional savepoints, forces explicit local rereads, redacts CLI output
and applies stricter reset protection. Nested eval directories and attachment
source hashes increased actual protected tenders from6 to8; the new source-page
audit protects a ninth. See TRACK_A_NOTES.md
and canonical DECISIONS for verification. Both migrations are committed; the
user now delegates their application to another agent. No application migration
or reset was performed by Codex.
