# Part B — capability reuse across requirements (handoff)

Date: 2026-11-14. Scope: match.py, facts.py, findings.py (read-only), their tests,
offline benchmark. No requirement mappings, no verify.py, no migrations touched.
All work offline — no paid LLM calls in this track.

## What changed

1. **Batch context dedup by shared ID** (`facts.py`, D-183). Identical table
   context is sent once in a top-level `contexts` array; each passage carries a
   `context_id` index into it instead of its own copy. Passage-to-context binding
   is preserved (table/row/column indices refer to that passage's context).
   `BATCH_INSTRUCTIONS` and the batch input shape changed, so `contract_version()`
   changes and previously cached facts are re-read rather than reused under a
   stale contract. `batch_input_chars` now measures the deduplicated payload, so
   the configured char bound admits more passages per call without exceeding the
   true sent size.

2. **Verified-fact cache gated on contract + source identity** (`match.py`,
   D-184). `_cached_fact` treats a stored fact as a miss unless
   `source_context.fact_prompt_version == contract_version()` and it carries a
   non-empty `document_sha256`. Byte-level integrity stays with
   `load_page_context` (raises on sha mismatch when the page is re-read); the gate
   does not re-fetch the document sha. The pre-existing verifier re-checks
   (`check_source_anchor`, `check_range_endpoint`, `check_table_binding`) and the
   conflict rule (`findings.cached_fact` returns None on conflicting values) are
   unchanged. Ranges, units, conditions and component/model scope are preserved.

## Measured before/after (offline, `eval/part_b_benchmark.py`)

- Input chars per shared-context batch: **before=214, after=128**.
- Requests for 3 requirements on one product/attribute: **no-cache=3, cache=1**.
- Verified outcomes unchanged: **True**.

## Tests

- `tests/test_part_b_reuse.py` (5, new): context sent once by ID; dedup reduces
  input chars; one verified read serves several requirements; cached candidates
  trigger no paid batch; wrong-column evidence still fails.
- `tests/test_fact_batches.py` (10): bound check now reads `contexts[context_id]`;
  the uncached trio fits one bounded call after dedup (`seen == [3]`).
- `tests/test_matching.py` (45): `_cached_fact_row` carries the contract version
  and a synthetic sha so the reuse tests exercise the new gate.
- Full unit suite: exit 0 (this repo's pytest prints no summary line).

## Acceptance mapping

- One read serves several requirements: via the verified-fact cache (D-184) plus
  same-page context dedup (D-183). A true single-read / many-requirement
  multi-attribute extraction contract is **deferred** (larger contract redesign);
  this increment does not change the one-requirement / many-passages batch shape.
- Wrong-model / wrong-column still fails: verifier re-checks intact and exercised.
- Identical clauses under different BOQ items stay separate: per-requirement
  assessment and item scoping unchanged; only the wire context is deduplicated.
- Fewer requests / input chars with unchanged verified outcomes: benchmark above.
- No provider names / endpoints / credentials in code or logs: unchanged.

## Concurrency note

Shared worktree with the main session. The D-183/D-184 DECISIONS entries were
swept into concurrent commit `673f0e6` (the main session staged `docs/DECISIONS.md`
while these edits were uncommitted); the ids and wording are correct and committed.
A main-session Part B background agent may also be running — re-check ownership
before further edits to match.py / facts.py.