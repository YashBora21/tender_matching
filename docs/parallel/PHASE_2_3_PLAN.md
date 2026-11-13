# Codex plan for Phases 2 and 3

Status: Phase 2 implemented and verified on 2026-11-13 after user approval.
Code commits: f5c3299 (source policy/storage/counts) and ceaa298 (report views).
Verification: 1264 Python unit passes, 6 artifact-dependent skips; 8 disposable-
database checks; 40 frontend tests, lint and build pass. Four local corpus
no-hardcoding checks also pass. No paid calls, application migration or reset.
See eval/audit/phase2-fragments-2026-11-13/results.json for the tested code hash.
The separate Phase 1 diagnostics and spend-runtime edits remain outside these
Phase 2 commits. Integrate/review them before freezing the next measured RC.
Phase 3 still awaits approval; no full-tender speed or accuracy gain is claimed.

## Confirmed code findings

RequirementType and the persisted PostgreSQL enum contain six PRD clause types,
without fragment. A new enum value would require migration 0024. Recommend the
user's no-migration fallback: type unknown, context_only true, and explicit
fragment metadata/reason. Keep source text, quote, page, parent/table context,
item provenance, and independent review flags. This is a display/source-context
classification, not a new comparison status.

The existing context path already excludes such rows from matching, requirement
counts and completion blockers. It already appears in a collapsed upload panel;
HTML report context is currently expanded. Reuse these paths and add a distinct
collapsed fragment view without duplicating rows.

Read-only inventory of the two current readings (373e9eb7 and7a1384ad):
- 1283 active stored rows:459 specification, 795 cover, 29 supporting.
- 116 restricted prose rows fall below 20 chars OR 4 words.
- 109 of those 116 are already context-only.
- 7 are currently candidates/unresolved; none matches a whole-label approved
 alias or the unit/standard signal in this inventory. Re-triage of their own
 text returns restricted context, but inherited/parent interpretation must be
 checked before downgrading them.
This is eligibility inventory, not accuracy grading or a live call saving.
The floor alone cannot be claimed as a large speed improvement.

## Phase 2 - implemented scope

1. Add REQ_MIN_CHARS=20 and REQ_MIN_WORDS=4 in Settings and .env.example.
   Count trimmed, whitespace-normalized Unicode prose; either failed threshold
   is below the floor. Preserve the original source unchanged.
2. Apply only to restricted-source prose (cover/ATC/SLA/supporting).
   Primary specification/BOQ table rows stay exempt. Determine a real table by
   extraction method and actual headers/cells, not source_table_id: prose also
   has a source-table identity in the current reader. Do not suppress primary
   unknown prose automatically under this scope.
3. Preserve candidates with NUMBER_WITH_UNIT, certification_pattern or an
   approved exact alias label. Reuse the alias registry/resolve_name and its
   approval/confidence/ambiguity rules; load the alias snapshot once per read.
   Existing technical/obligation/inherited context and ambiguous mappings stay
   review candidates. Length alone must not erase a requirement signal.
4. Tag only genuine no-signal short fragments as unknown context. Set a fragment
   reason and policy metadata in normalization.source_context. Ensure tagging
   happens before old fragment handling, which must retain these context rows.
   No model call; no completion blocker; no requirement/flag count inflation.
   Page-ledger states remain independent and failed pages stay unresolved.
5. Expose fragments with source, page, type and reason in a collapsed report
   section and reading UI, preserving other context and avoiding duplication.
   Prefer optional metadata/fields to keep existing API/report callers working.
6. Include floor settings and the relevant approved-alias snapshot in affected
   source/checkpoint identities. Record every cache-key change in DECISIONS.
   Add floors to the numeric reading-limit snapshot/preflight. Reuse existing
   valid response/OCR caches when their semantic inputs are unchanged. No prompt
   or verifier changes are planned.
7. Test short noise retention; unit/cert/approved-alias retention; unapproved or
   ambiguous mappings; obligations/parent context; Unicode/Hindi; exact boundary
   values; real table exemption including OCR tables; no model calls; storage
   round trip; count/completion exclusion; collapsed escaped report output;
   and checkpoint invalidation when floors or aliases change.

Likely owned files: core/config.py and .env.example; source_policy.py/json;
tender pipeline/ingest/upload and completion helpers as required; output/report;
requirements API metadata; frontend report/upload types/views; targeted tests.
No overlap with the Phase 1 llm.py/e2e.py files. Preserve existing spend WIP.
Stop with tests and a reviewable diff after Phase 2; wait for Phase 3 approval.

## Phase 3 - prepare now, execute only after integration and approval

- Freeze an RC containing both agents' changes. Keep grouping off. Do not run
  a paid benchmark on an uncommitted mixed checkout.
- Inventory reading_attempt counters and llm_spend coverage before claiming
  baseline cost. Spend runtime is separate unfinished local work. Missing
  historical cost rows mean unavailable, not zero; earlier audits may supply
  tokens/calls but are not automatically comparable across prompts/models.
- Use 2bc8f917 as the planned tender, subject to source availability and reviewed
  labels. Set LLM_CALL_BUDGET=20 in one new foreground CLI process, not the shared
  server's .env. Respect token limits; test clean resumable stopping and record
  the limits each attempt actually used.
- After approval, resume the same frozen bytes under the full configured
  limits, using valid caches. Report bounded attempt, resumed attempt, combined
  totals and cache state separately. Avoid presenting resume-only time as the
  time to read a fresh whole tender.
- Report page/source roles, context/fragments, calls, tokens, stage wall times,
  retries, confirmed answers and abstentions by cause. Grade false-confident
  claims only against confirmed labels; otherwise mark the result ungraded.
  Estimate cost only with verified configured prices and stated coverage.
- A restricted attachment still needs an authorized supplied copy. Preserve
  prior readings and reports. No reset is included in the measured-read task.
