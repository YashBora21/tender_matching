# Parallel implementation contract — 2026-11-12

Status: both tracks integrated into logical commits; deployment and reviewed reset
remain separate. See MIGRATION_HANDOFF.md for the newly authorized migration task.

Repository: `https://github.com/YashBora21/tender_matching.git`.
Workspace: `C:\Users\Lenovo\OneDrive\Desktop\tender_matching`.
Implementation baseline before this handoff: `688cf41` on local `main`.
Read `AGENTS.md`, `docs/PRD.md`, and `docs/parallel/OPENROUTER_TRACK_B.md` first.

## Ownership

| Track A — Codex in this chat | Track B — user's OpenRouter coding agent |
| --- | --- |
| Source roles, deterministic pre-classification, context versus requirements | Reading attempts/history and current-limit resume |
| Role-aware completion and page-ledger accounting | Read-again API and UI, effective limits in preflight |
| Template-candidate builder and review data | Dry-run reset CLI, protected-evaluation checks and cache provenance |
| Bounded classification batches/cache policy | Reading/provenance migrations and repository interfaces |
| One evidence-validation repair retry within shared budgets | Lifecycle/reset tests and operator documentation |

A owns `backend/app/services/tender/{pipeline,ingest,classify,completeness}.py`,
`backend/app/services/tender/clause_policy.json`,
`backend/app/services/requirement/`, `backend/app/services/llm.py`,
`backend/app/services/output/{report,runs}.py`, template builder/new role-policy
data, and corresponding extraction/completion/classifier tests.

B owns `backend/app/services/tender/upload.py`, `backend/app/api/{tenders,routes}.py`,
`backend/app/services/cli/e2e.py`, `backend/app/repositories/`,
`backend/app/schemas/`, `backend/alembic/versions/`, frontend reading controls,
new reset/lifecycle CLI modules, and corresponding lifecycle/preflight/UI/reset
tests. B may extend `core/config.py` only if necessary; preserve existing edits.

Neither agent edits the other's files without first documenting the requested
interface change. In one shared checkout, B leaves changes **unstaged** and lists
owned files and test results in `TRACK_B_NOTES.md`. A handles commits/integration
so a commit cannot accidentally consume the other's index. No reset/clean/stash,
branch switching, database migration or deleting data in the shared workspace.
An already separate checkout can use its own `codex/reading-lifecycle-reset`
branch, based on the published handoff commit, and return its commit hashes.

A alone updates canonical `docs/DECISIONS.md` and `docs/WORKLOG.md`, preserving
unrelated pre-existing entries. B records decisions in `TRACK_B_NOTES.md`; A
imports them at integration. One logical change per commit. Do not stage all.

## Interface between tracks

1. Keep `run_pipeline`, `audit_tender`, repository requirement lists and
   `StoredRequirement` interfaces backward compatible. A needs no schema change
   for source roles: persist these under `normalization.source_context`:

   ```json
   {
     "source_role": "specification",
     "context_only": false,
     "candidate_type": "technical"
   }
   ```

   `source_role` is one of `specification`, `boq`, `cover`, `atc`, `sla`,
   `supporting`, `unknown`. `candidate_type` is `technical`, `certification` or
   JSON null. These are metadata, not a new business result status.
   Preserve existing `page_tag`, `table`, and `nearby_source_clauses` metadata.
   Primary-source ambiguity may block; restricted context-only rows never block
   or enter matching. Role and clause type are separate facts.

2. A owns `completion_gaps()` and role-aware `page_coverage()`. B calls those
   helpers rather than reimplementing completion in API/history/reset code.
   B's read UI must retain and expose context clauses and separate source pages.
   Server reports now expose `context_clauses` (entries with source_role, type,
   page, source_text, reason and no manufacturer results). The page summary's
   `sources` entries expose `source_role`; persisted page tags also include
   `source_role:<role>`. Please display these via optional fields in B's React
   reading/report views; A supplies HTML/CSV visibility without frontend edits.

3. B wraps ingestion in an append-only reading attempt and associates its limit
   snapshot, outcome and counts. A returns existing pipeline/audit metrics;
   request extra metrics through notes before changing A's files.

4. B may add optional `tender_id`/`reading_id` parameters to `LLMCache(session)`
   for provenance; the existing one-argument constructor and get/put signatures
   must continue to work. Link shared cache use without deleting entries used
   by protected/other tenders or the corpus. Legacy unattributed entries are
   retained and reported, not guessed to be tender-only.

5. A will implement one evidence-repair retry with safe error feedback, inside
   existing retry/call/token limits. B must not alter LLM retry behavior.

## Accepted policy

- Specification and BOQ are primary sources. Cover/ATC/SLA and other supporting
  sources receive deterministic clause triage; only possible technical or
  certification clauses enter model processing. Everything else stays visible
  as typed context, with zero model calls. No blanket loss of buyer-added terms.
- If primary sources yield no technical lines, deterministic cover scanning is
  the fallback. Always retain candidate manufacturer/ISO certification clauses.
- Completion blockers are primary requirement lines and deterministic technical/
  certification candidates; legal/commercial/eligibility context never blocks.
  Requirement review flags are independent of tender completeness.
- Template candidates are normalized clauses found in at least three distinct
  stored tenders, emitted as data for user review; repetition is not approval to
  skip a clause. Batch classification preserves one validated result per clause.
- Cache identity retains role/model/prompt/schema/policy identity; reviewed,
  context-independent templates exclude neighbour context from their key.
- No more paid full-tender reruns now. First deliver source-policy checks,
  template candidates and reset preview. Reset execution requires user review;
  only after the approved reset will the original PDF be uploaded as a new tender.

## Integration order

1. A: pinned blocker report and source-role/completion contract; B can immediately
   build reading history and pure reset planning/protection tests.
2. A: deterministic template builder, batching, one repair retry; B: lifecycle,
   preflight, read-again and reset preview, using the contract above.
3. Integrate interfaces and notes; run unit suite on the combined committed
   snapshot once, plus relevant frontend tests/build and migration checks.
4. Present backup command and reset dry run to user. **Do not execute reset.**
5. After explicit reset approval and backup verification: approved scoped reset,
   fresh upload, one measured extraction/matching validation with labels where
   required. Report calls/tokens/cost if tariffs verified, source accounting,
   false-confident claims and stage wall times. Grouping remains off.
