# Quote-based requirement extraction — 2026-11-10

Requirement extraction now requests a verbatim `source_quote` instead of
LLM-counted character offsets. Code accepts one exact occurrence in the bounded,
normalized English clause sent to the model, derives its offsets, and applies
the existing value/range-support gate. Offsets are Python Unicode code-point
indices into the stored `source_text`, not byte offsets or browser UTF-16 offsets.

Missing, whitespace-only, repeated, overlapping-duplicate, altered or unsupported
quotes remain review gaps. There is no fuzzy search, offset repair or fallback
from display paraphrase to authoritative evidence. Legacy `span` responses and
mixed old/new envelopes fail strict schema validation. If any proposal in a
clause fails, the pipeline retains the original clause and does not cache the
partial answer. Budget stops preserve unread work for resumption.

The prompt asks for source values without semantic rewriting, complete ranges,
and classification/standard prefixes in values rather than measurement-unit
fields. Document text is explicitly untrusted data, never instructions.
Unknown units remain unchanged and explicitly require review. Review
flags/reasons survive structured-cache serialization.

The original clause and code-derived `source_span` now survive cache reuse,
storage conversion, repository insertion and repository reads. The existing
database `source_span` column is used; no migration was added. Source/item/page
identity is preserved. Display paraphrases remain separate from source evidence.

## Lineage and compatibility

Requirement prompt/cache version advances from 4 to 5 in the working tree. New
matching admission requires current extraction; old historical rows and reports
were not refreshed or deleted. Page-context caches and fact extraction prompts
are unchanged. Unit/vocabulary/model/condition uncertainty is not resolved merely
because a quote passes; source regions remain review-only.

The commit includes only the quote contract and necessary provenance/cache/storage
wiring, fixtures and tests. Older OCR, source-context and matching integration
remains outside this commit rather than being blanket staged.

## Validation

- Full working-tree offline backend suite passed.
- Full isolated staged-snapshot offline suite passed, with three corpus-dependent
  checks skipped because collected files are absent from that export.
- The final focused quote suite passed 26 tests covering exact/ambiguous/Unicode quote location,
  full ranges, wrong values, old/mixed envelopes, unit-review persistence,
  partial-answer cache refusal, item identity, prompt-cache invalidation,
  repository payload/read round trips and resumable budget stops.
- Unchanged retained legacy response was rejected under version 5; the complete
  clause stayed reviewable, with zero accepted new requirements and no response
  adaptation or offset repair.
- Zero API calls, database writes or historical data changes. Database insertion
  was checked with a recording session, not a live database transaction.

The final test launch was delayed by an automatic approval-review timeout; its
permitted retry succeeded. Earlier suites were unaffected. The isolated export
was also rebuilt after an initial stale-export attempt; that aborted run was
discarded and the rebuilt snapshot passed.

Local replay: `eval/audit/quote-contract-2026-11-10/legacy-replay.json`.
Live provider adherence to the new contract is untested. The next step is a small
free-tier extraction probe, followed by reviewed vocabulary/unit handling and a
history-preserving refresh. Full-tender accuracy, runtime and grouping equivalence
remain unproven.
