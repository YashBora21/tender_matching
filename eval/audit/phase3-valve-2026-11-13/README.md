# Phase 3 measured reading — 2026-11-13

Outcome: **the RC does not pass extraction or processing-time acceptance**.
No manufacturer matching was run on incomplete ingestion. Grouping stayed off.
App source: `rc-2026-11-13.3`, commit
`eecced68afe5ae31af0e765abc66d24e1a2640e0`, exported independently of the
shared checkout's unfinished spend code. All 178 exported app/test files match
their archive. Backend unit suite: 1204 passed, 3 corpus-dependent skips, no
failures/errors. Four synthetic reservation-guard checks pass. The additional
synthetic resume reproduction demonstrates a defect; it is not an acceptance pass.

## Actual paid measurements

Saved package: `GEM_9671792_Valve.pdf`, tender
`2bc8f917-262d-4ea8-a1c9-b2997ef1f256`. Wrapper and all four attachment hashes
match the manifest. This excludes attachment network collection and uses valid
available caches; it is not a cold-upload or whole-system timing benchmark.

| Measurement | Bounded attempt | Resume | Combined |
| --- | ---: | ---: | ---: |
| HTTP calls | 20 | 374 | 394 |
| Input tokens | 27,118 | 407,640 | 434,758 |
| Output tokens | 9,277 | 33,723 | 43,000 |
| Wall seconds | 78.07 | 590.24 | 668.31 |
| Estimated INR | 3.13279 | 20.65995 | 23.79274 |
| Reported extraction cache hits | 0 | 0 | 0 |

The first attempt stopped before call 21 under its explicit 20-call limit.
The resume used the current 600-call/1.5M-token process limits. It stopped before
another request's worst-case reservation would exceed the **combined INR 25**
benchmark allowance. All 394 HTTP calls returned answers; this was not a provider
429 stop. The application's legacy `rate_limited` state also represents this
resumable budget stop. Attempts and their distinct limit snapshots are stored.

Cost uses the configured target's published standard text tariff, privately
matched on the [official pricing page](https://aistudio.google.com/docs/pricing):
USD 0.30 input / USD 2.50 output per million tokens. INR 100/USD is a conservative
environment-supplied budgeting assumption, **not a market exchange-rate quote**.
These are token-cost estimates, not an invoice; taxes, credits and actual billing
conversion are not verified. Earlier monthly spend is unknown. Production monthly
cap enforcement remains unfinished and is not established by this benchmark.
Successful calls were recorded in `llm_spend`: 394 rows, one distinct configured
model, matching token totals; per-row four-decimal rounding totals INR 23.7939.
No model identifier, inference endpoint or key is included in these artifacts.

## Where the time went

| Combined component | Seconds |
| --- | ---: |
| Saved BOQ item reading | 0.96 |
| Source package reading | 14.00 |
| Model HTTP attempts | 580.06 |
| Local extraction, including context/cache/validation | 35.69 |
| Final requirement/checkpoint persistence | 1.57 |
| Other setup and resolution | 36.04 |

HTTP latency p50: 1.453s; p95 nearest rank: 2.086s across 394 attempts.
The local extraction remainder is not isolated context-preparation time.
Source reading reused the first attempt's checkpoint on resume. No embedding
model or manufacturer retrieval was invoked. All 60 ledger pages are `parsed`:
17 wrapper pages and 43 attachment pages; the CSV has no PDF pages. Page parsing
does not establish semantic completeness. Removing requirement persistence would
not address the dominant measured cost.

## Findings requiring fixes

1. **Resume discards sibling technical candidates.** Source-level deduplication in
   `_extract_all` happens before per-record context handling. If a stored source
   row's first child is context, subsequent technical children share its identity
   and are skipped. Live BOQ technical candidates fell from 18 to 3. The previous
   988-row revision is still stored; all 1009 superseded rows remain in history.
   A synthetic two-child source reproduces retention of only the context child
   with zero model calls. Fix preservation before another paid rerun; do not
   interpret this count reduction as extraction improvement.
2. **One ambiguous quote rejects a whole response.** On a real BOQ row, the first
   resumed response proposed five fields. The serial-number quote `1` occurs five
   times in the full source, so unique-quote verification rejects it. Both the
   initial answer and its one evidence repair repeat this ambiguity; the entire
   response is rejected. Preserve the verifier. Better source/cell anchors and
   field-specific feedback need evaluation, rather than a phrase-specific guard.
3. **Primary-source routing and clause assembly create avoidable calls.** A
   24-page attachment beginning with an integrity-pact form is classified as
   `spec`, alongside the actual 17-page technical annexure. Its short prose is
   therefore exempt from the restricted-source fragment floor. Among 273 proposed
   classifications in the saved resume responses, 201 are nontechnical, 67 unknown
   and 5 technical. These are model proposals, not independently graded labels.
   Infer source/section roles and assemble clauses before calling the model;
   retain potentially technical/certification terms and every source reference.
4. **Batching has little effect in this run.** Of 226 classification calls, 199
   contain one clause; 13 contain two, 9 contain three, 4 contain four, and 1
   contains five. The resume also repeats 20 identical HTTP payloads. Improve
   batching and reuse without merging distinct BOQ assessments or stripping
   meaningful evidence context.
5. **Cache metrics require care.** The database holds 50 extraction entries and
   273 classification entries for this tender. An offline, read-only probe finds
   classifier answers for 83 of 283 remaining unassigned unknown rows. This is
   partial reuse availability, not a successful whole-row extraction cache hit.
   The reported zero hits must not be interpreted as no cache entries existing.

## Unresolved source inventory

The current revision contains 1013 rows: 37 fragments, 516 other context rows and
460 potentially unresolved candidate rows. The legacy audit's `requirements=976`
excludes fragments but includes other stored context; it is not the number of
matchable technical requirements. Independent review flags remain on 696 rows.

| Source role | Potentially unresolved rows |
| --- | ---: |
| BOQ | 3 |
| Cover | 73 |
| Specification | 384 |

Exclusive primary causes: 3 quote/value-validation rows, 117 source-coverage rows,
103 technical/certification item-assignment rows, 237 unresolved-type rows. Some
are still unread because of the spend stop. They are not 460 proven model errors.
Full role/type/cause counts are in `results.json`.

No whole-tender extraction gold inventory exists. The six confirmed compound
manufacturer labels for items 10 and 11 do not grade every extracted row, and
cannot establish a false-confident rate without manufacturer assessments.
Confirmed answers and false-confident manufacturer claims remain **ungraded**.
The current reading remains incomplete and must not be used as a passed tender.

## Reproduction and retained evidence

`measure_saved_read.py` preserves the instrumented driver; `guard_selfcheck.py`
preserves its synthetic monetary guard checks; `reproduce_resume_siblings.py`
preserves the zero-call regression reproduction. The driver reads its frozen RC
path and numeric tariff from `.audit_tmp/phase3-snapshot.txt` and
`.audit_tmp/phase3-tariff.json`, and the private runtime environment. It takes the
tender id and limits from `PHASE3_*` environment variables. Its `bounded` and
`resume` invocations supersede requirements and write history/spend; they are
**not read-only**. No automatic rerun is part of this audit.

Raw sanitized response and per-call journals remain local under
`.audit_tmp/phase3-measurement`; their SHA-256 values are recorded in `results.json`.
The quoted failure example and synthetic resume reproduction are included there.
No reset, application migration, corpus modification or matching run was made.
