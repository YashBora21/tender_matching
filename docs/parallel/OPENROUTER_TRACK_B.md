# Copy this assignment to the OpenRouter coding agent

You are Track B on the Tender Requirement Matching project. Work in parallel
with Codex (Track A), following `docs/parallel/PLAN.md`. The user explicitly
authorized this two-track split. Read the plan and repo AGENTS/PRD before coding.
Do not implement Track A's extraction/classification/completion changes.

## Your scope: user steps 5 and 6

### 5. Reading history, resume and read again

- Same PDF bytes stopped at a budget/rate limit must resume using **current**
  process limits, reusing only valid source/prompt/schema/policy caches. Old
  stop reasons/old numeric limits must not govern the new attempt.
- Reuse an actually complete reading on duplicate upload. Add an explicit
  read-again action (API and frontend) which supersedes the old reading, never
  destroys requirements, historical reports or previous reading snapshots.
- Store each reading's effective numeric limits, versions, status, source
  identity and metrics; show the current effective numeric limits in
  `/api/preflight`, separately from historical attempt limits. Never expose
  credentials, actual model IDs or endpoints in new output/logs/artifacts.
- Keep tender-level incomplete separate from requirement `review_required`.
  Consume A's `completion_gaps()` rather than inventing another completion gate.
- Preserve `item_id`, raw source quotes and original run/report references.

### 6. Scoped reset tool — plan/dry run now, execution only after user review

- First document the backup commands below. Do not execute them yourself.
- Build a CLI whose **default is dry run**, listing selected/excluded tender IDs,
  exclusion reasons and counts of tender-derived tenders, attachments, items,
  requirements (including superseded), extraction batches, source tables/page
  ledger, readings, runs, results and attributable legacy cache entries.
- Protect tenders referenced by `eval/gold*`, `eval/baseline*` and `eval/audit`.
  Check UUIDs, source hashes and referenced source paths/filenames as appropriate;
  missing protected paths do not mean evaluation references elsewhere vanished.
  `--include-protected` is an explicit override, not the default.
- Destructive execution requires `--yes`; a preview must never write database
  state. No code in this session is authorized to execute reset yet.
- Never touch `manufacturer`, `product`, `document`, `chunk`, `fact`, `alias`,
  `attribute`, corpus files/vectors, or their cache entries. Do not erase billing
  history/spend records to reset budgets. "Ledger" here includes tender pages.
- Shared `llm_cache` currently has no tender ownership column. Add provenance
  safely for future writes/uses if needed. Only remove entries demonstrably
  tender-derived and unshared; retain/report unattributed legacy entries. A broad
  prompt-version filter alone cannot prove an entry belongs to a tender.
- Respect FK dependencies and cross-tender/protected references. Plan and delete
  the same scope transactionally, rollback on invariant failures. Test protected
  exclusions, dry-run zero writes, confirmation refusal, cross-reference/shared
  cache retention and corpus untouched. Do not test deletion on the live database.
- Prepare fresh-upload instructions after an approved reset; do not re-upload,
  delete local PDFs or run paid matching now.

## Exact backup commands for the user (PowerShell at repo root)

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
$BackupFile = ".\backups\tender-before-reset-$(Get-Date -Format yyyyMMdd-HHmmss).dump"
docker exec tender-pg-local sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner --no-acl -f /tmp/tender-before-reset.dump'
docker cp tender-pg-local:/tmp/tender-before-reset.dump $BackupFile
docker exec tender-pg-local pg_restore --list /tmp/tender-before-reset.dump
Get-FileHash -Algorithm SHA256 -LiteralPath $BackupFile
```

Do not use PowerShell binary output redirection to create the dump. Backup
creation and restore-list/checksum verification remain the user's actions.

## Current state and evidence

- Repo/workspace/base and ownership are in PLAN.md. Existing local environment
  is private. Credentials and actual model IDs/endpoints must never be copied
  into code, logs, screenshots, commits or your notes. No credentials are needed
  for offline tests. Budget: user has INR1,000/month, prior spend unknown.
- Target filename: `GeM-Bidding-10103860.pdf`.
  Existing tender ID: `373e9eb7-b5b2-4fa3-97cd-4ea478f571f0`.
  Frozen files: `backend/data/tenders/<id>/manifest.json`, wrapper PDF and `raw/`.
- The pinned 138-blocker reading has **506 rows**: cover128 (109 proposal gate,
  17 quote rejection, one coverage gap, one assignment); scanned attachment9
  proposal gate; specification1 quote/value failure; BOQ0. Only two are candidates
  under stored types/current deterministic rules: one technical spec row and the
  cover ISO9001 bidder/OEM clause. That certification candidate must be retained.
- The older487-row reading had265 blockers. All487 had empty attributes and were
  flagged `unmapped attribute`:449 cover,32 scanned,6 spec. Normalization applied
  technical alias review to administrative/context rows. A owns that correction.
- Latest completed experimental reading:498 rows,86 blockers, still incomplete.
  Its test ran classification on administrative clauses; that approach is now
  **superseded by the user's source-role policy**, not a successful release.
- No live reruns after this split. User will review dry run before reset.

Useful entry points:

- `backend/app/api/tenders.py`: upload duplicate scheduling and snapshots.
- `backend/app/services/tender/upload.py`: `store_upload`, `ingest`, `_read`.
- `backend/app/services/tender/pipeline.py`: A-owned stages/persisted metadata.
- `backend/app/repositories/tender.py`: `_supersede_requirements`, cache and stages.
- `backend/app/services/cli/e2e.py:preflight`; `backend/app/api/routes.py` delegates.
- `backend/app/services/output/runs.py`: A-owned matching/config snapshots.
- `backend/app/repositories/schema.py`, `backend/alembic/versions/`.

## Preserve existing unrelated work

Pre-existing dirty files include `.env.example`, `core/config.py`,
`repositories/schema.py`, `services/llm.py`, tests `test_config.py` and
`test_fact_cache_conflicts.py`, plus `docs/DECISIONS.md`. They contain unfinished
spend-ledger work. Untracked `0022_llm_spend.py`, `services/spend.py`,
`tests/test_spend.py`, old notes and eval scripts also predate your task.
Do not discard, stage or claim them as your work.

Migration caution: last tracked migration is0021; local0022 spend migration is
untracked and the development DB may already carry it. Inspect the graph read-only,
report the situation and propose a migration plan. Do not apply migrations or
publish a new migration that secretly depends on an uncommitted file. You can
implement/test the lifecycle and reset planner without migrating the live DB.

## Deliver back to Codex

In the shared checkout leave files **unstaged**, do not switch branches or commit
the shared index. Write `docs/parallel/TRACK_B_NOTES.md` with owned paths, changes,
new interface/migration choices, exact test commands/results, safe dry-run command
and output, limitations, and decisions for integration into DECISIONS/WORKLOG.
No secrets, no actual model targets. Ask the user to relay interface questions to
this chat. If using an existing separate checkout, return commit hashes instead.

Use synthetic labelled fixtures. Local FastAPI test sockets may require tool
approval on Windows; do not bypass a rejection. A will run final combined checks
and manage logical commits/publication. Do not mark the whole project complete
merely because your independent tests pass.
