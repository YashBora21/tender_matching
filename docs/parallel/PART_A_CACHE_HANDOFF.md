# Part A handoff - 2026-11-14

Scope complete:read-only inventory,PDF context recovery,current-gate re-verification,
and unapproved mapping proposals. No changes to matcher-owned files or database.

Entry points:eval/reassess_source_context.py and app.services.cli.fact_cache.
Use --output-dir NEW_DIRECTORY. Historical audit/reviewer files are untouched.
Current review files:.audit_tmp/part-a-final-review-20261114/README.md,
fact-review-claims.csv,mapping-review.csv,facts.json,inventory.json,summary.json.

Findings:325 rows,252 empty nonclaim placeholders,73 claims;17 verified rows all
lack usable context.310 rows have absent/empty context.86 pages inspected,
75 have native text.0 fully eligible repairs.2 exact approved-label canonical
proposals still fail other gates.6 conflict groups remain distinct.197 label
groups are unapproved review data.133 source/cache/verifier/normalization tests pass.

## Integration contract for Part B

- facts.json is a proposal artifact,not an approved/cache-ready source.
- proposed_status/cache_eligible_after_reviewed_apply are dry-run decisions only.
- Each fact retains its original_fact_sha256. Apply must reject stale snapshots.
- source_context contains original document hash/page,native tables/lines and
  candidate claim indices. It does not pretend the old claim was freshly
  extracted by the current model/prompt;offline recovery metadata is separate.
- An existing verified flag does not establish reusable context. Existing missing
  source contexts and conflicts must not be bypassed for a cache hit.
-252 empty placeholders are not positive capabilities. Keep negative extraction
  attempts separately from verified capability facts;never equate no fact with
  missing capability.
-73 claim rows retain all values/quotes/conditions. No guessed model inheritance,
  scalar range endpoint,wrong-column binding or missing-condition repair passes.
- Native-textless pages need an explicit verified OCR/source path;source text was
  not fabricated from proposed model output.
- mapping-review.csv has reviewed_canonical_attribute,reviewed_component_scope,
  human_notes and approved=false. Global ambiguous labels stay unresolved.

No current-repair apply command is shipped:review the dry run before database
writes. eval/apply_evidence_review.py --yes is for the historical quarantine only.
Part B owns match.py,facts.py,findings.py and its benchmarks;Part A did not edit
those or relax verify.py. No prompt/schema/cache-key changes in Part A.

## Reviewed continuation - 2026-11-14 (D-181/D-182)

The user completed all73 claim rows:48 supported,3 incorrect,22 uncertain.
The197 mapping groups remain unapproved. CLI now accepts --review-sheet and
rejects stale/modified source fields,unknown/duplicate IDs and invalid reviews.
Supported does not establish model scope or override an existing evidence gate.

Normalization v4 handles DN/NPS unit-column labels,repeated range designators,
mixed Unicode fractions and configured inch punctuation. No raw field changes.
The normalization fingerprint changes;prompts and extraction contracts do not.
Derived nominal comparability mirrors the matcher;missing operators still fail.

Private output:.audit_tmp/part-a-reviewed-final-20261114/repair-review.csv,
repair-summary.json,README.md,facts.json and readback.json.39 typed normalizations
resolve;comparison failures decrease68 to60.52 original quote locations have
native line IDs/geometry,explicitly proposals rather than model/attribute proof.
0 fully eligible repairs;17 verified-to-review changes still proposals only.
No DB writes or paid calls. Do not bulk-import this proposal file as capabilities.

Combined Part A frozen tracked snapshot:1379 backend unit passes,3 skips,
24 integration tests deselected.Collector15 and eval62 pass. A live-tree run
had2 batch-prompt test failures while Part B edited its prompt and tests;these
files were not changed by Part A. Recheck integrated Part B separately.
