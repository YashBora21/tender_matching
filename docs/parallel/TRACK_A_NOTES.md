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
