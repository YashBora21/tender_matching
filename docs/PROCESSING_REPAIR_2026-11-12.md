# General processing repair — 2026-11-12

Tested source: `99a7ceb73b9f538b066ffaff77ed3504676ce350`; candidate `rc-2026-11-12.1`.

The local smoke profile allowed one call, 2,000 total tokens and 512 output tokens.
It is now a bounded 200 calls, 400,000 total tokens, 4,096 output tokens and one
retry. These are process limits, not a guarantee that any document or monthly
cost will fit. Restart the backend to reload private configuration, then upload
the same tender to use the changed extraction-stage identity.

The observed vehicle-parts tender had 332 stored rows, 18 accounted pages,
no extracted attributes, corpus gaps and unresolved scope. Missing catalogue
evidence was suppressing extraction. Extraction is now independent of vendor
inventory; the matching preflight still abstains when applicable evidence is absent.
Unknown OCR, source quotes and BOQ assignments still require review.

Repeated full requirement snapshots were overhead, not the sole bottleneck.
A rollback-only real 332-row snapshot measured 0.1358, 0.1050 and 0.0822 seconds,
excluding commit time. All real rows were unchanged afterward. Progress now
commits small response-cache entries; full requirements are stored once at the
end of an attempt or a resumable stop. Reports, historical revisions and cached
paid work remain available. Synthetic 2/100/332-clause cases assert one full write.

Malformed/partial extraction is no longer marked as a finished extraction stage.
Retries reconstruct clean local source records and reuse only complete cached
answers. Existing incomplete cache entries are checked and not reused. The
stage key includes the source-first policy and output-token limit. Requirement
prompts and valid response-cache identities are unchanged. The screen now gives
interpretation recovery advice instead of always asking for a missing document.

Validation: 1,130 Python tests passed and six artifact-dependent tests skipped in
an isolated committed snapshot; 37 frontend tests, lint and build passed. No text
generation requests were made. A read-only provider model-list request returned
HTTP 200 and confirmed the configured model was advertised; no identifiers or
credentials were logged. This does not prove whole-tender extraction accuracy
or processing time. Separate unfinished monetary-ledger work was preserved and
excluded from these commits. GeM discovery follows this repair, as requested.

Audit: `eval/audit/processing-repair-2026-11-12/summary.json`.
