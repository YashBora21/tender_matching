# Track A progress

## Completed in the task-split phase

- Assigned ownership and cross-track interfaces in PLAN.md and wrote a complete
  independent Track B assignment in OPENROUTER_TRACK_B.md.
- Pinned the historic 138-blocker/506-row reading separately from the older
  487-row reading in `eval/audit/parallel-implementation-2026-11-12/blocker-baseline.json`.
  This file intentionally protects the target tender under the planned reset
  policy. The reset preview must show that protection, not silently override it.
- Fixed normalization so only technical/certification clauses receive technical
  alias/unit normalization review flags. Unknown clause type and source-scope
  review remain independent. Existing review flags are preserved; no historical
  revisions or live records have been rewritten.
- Preserved existing `normalization.source_context` while normalizing records.
  Derived-value policy version is now `structured-values-2`; resolution keys
  already include this fingerprint, so affected normalization can rerun without
  invalidating valid LLM answers.

## Next A work (not yet implemented by this slice)

1. Attach deterministic source roles/context/candidate metadata in the reader and
   preserve it in stage/cache/storage round trips. Enforce no model calls for
   restricted noncandidate context; retain possible technical/ISO clauses.
2. Make completion and page accounting source-aware, with context visible and
   review flags independent. Update output paths to exclude context from scoring.
3. Generate template candidates across distinct stored tenders for user review;
   never auto-approve repetition. Implement bounded batch classification and
   versioned, role-aware cache identities.
4. Tighten existing generic structured validation to exactly one evidence-repair
   retry with safe error feedback inside the shared retry/token budget.
5. Integrate B's lifecycle/preflight/reset preview, consolidate notes and validate
   one combined snapshot. No reset or paid full-tender rerun before user review.

## Test status

Focused normalization, comparison, source typing, pipeline, classifier, coverage
and run tests are being checked for this slice. The first attempt had pytest
temporary-directory setup errors under the sandbox; the repeat uses a fresh,
verified workspace-contained temporary directory. Repeat result: **202 passed**, zero skipped/failures/errors, 31.578 seconds. No provider calls or database mutations for this slice.

## Source-role/completion implementation update

Source-role policy now exists in services/tender/source_policy.py and its JSON
data. The package reader assigns roles before item matching; restricted
noncandidate rows are typed context, never sent to a model, never guessed onto
an item and never discarded as fragments. ISO/certification candidates and open
vocabulary product obligations remain readable. A cover candidate fallback also
works when a BOQ exists but no specification attachment is available.

Role/context/candidate metadata survives record, cache and storage round trips.
Completion ignores context-only and nontechnical clauses; primary uncertainty,
candidate gaps, unresolved technical item scope and failed source pages remain
blockers. Technical field candidates cannot be hidden by a nontechnical model
label. Existing legacy records without role metadata retain conservative handling.

Report JSON exposes `context_clauses`, with source_role/type/page/source_text and
no manufacturer results. Matching excludes context; HTML and CSV still show it.
Page summary sources expose source_role, with role tags on ledger rows. **B UI
integration:** please display these optional fields in reading/report controls.
Do not duplicate A's completion logic.

This is an offline-tested source-policy slice, not a live tender pass. No new
full-tender rerun, reset or migration has been performed. Template review data,
batch classification and the one-error-feedback-retry policy are still next.


## Integration and source-page correction - 2026-11-13

Both tracks are committed through4d700b3. Validation repair, source routing,
batching, schema prerequisite, reading lifecycle, reset and fixture isolation
have separate commits and canonical DECISIONS entries. Context is visible in
the upload UI; complete readings also offer read-again. Source policy and batch
signatures participate in checkpoints regardless of the legacy classifier flag.

B handoff corrections: force explicit rereads; use savepoints for history; close
finished attempts only once; serialize read-again; accumulate cache users on
reads and writes; retain legacy/unproven usage; protect nested eval directories
and attachment hashes/names; refuse reset with unknown counts; freeze writers
and check cross-scope references before execution; redact CLI model identity.

Committed snapshot4a32018:1155 unit passes,3 corpus-dependent skips. All4
no-hardcoding checks passed against the local corpus. Database migration,
reading/cache/reset integration checks:8 passes in a disposable database that
was removed. Application database was not changed by Codex. Frontend39 tests,
lint and build pass. Reader17's new focused regression set:74 passes; final
committed snapshot rerun is recorded in eval/audit after completion.

Read-only template discovery:137 unapproved candidates across13 tenders,0 calls.
Refreshed reset preview:6 selected,9 protected; current row counts are recorded in the final audit; history/cache counts remain unknown before migration.
No application reset. No paid calls after the parallel-policy baseline.

New user direction delegates migration/deployment to the second agent. Codex
verified that the other tender's failure was a blank specification page, not a
poor three-page scan. Reader17 now requires no text and entirely white rendered
pixels to skip a page. The26 local attachment pages read with0 failures. The
restricted attachment still needs an authorized supplied copy. This is source
accounting evidence, not a full extraction/matching quality result.

Final snapshot4d700b3:1158 unit tests passed,3 corpus-dependent skips; no failures.

The latest read-only preflight of the database revision reports0023. Codex did
not apply this application migration. Latest preview:6 selected,9 protected;
1848 requirement rows,21 attachments,45 items,4 runs,73 results;737 legacy cache
entries retained and0 cache deletions. No unknown counts remain. Reset is still
unexecuted and requires the user's dry-run review.
