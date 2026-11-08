# Fixed in-corpus validation — 2026-11-08

## What was frozen

The two-page valve specification has SHA-256 `cfd76796ef1d366905dc14d079a2d3a6e3c288f317f4377829a83f7e9c28c5d1`. It is the exact BOQ/spec attachment of the original 17-page valve tender. The separately uploaded record contains 11 items and 48 stored atomic requirements. Inputs, code fingerprint, corpus/config fingerprints and result files are retained under `eval/audit/fixed-incorpus-2026-11-08/`. The corpus snapshot covers 101 documents and 29,688 chunks, including vector data. Raw frozen source copies remain local audit artifacts.

The measured application includes earlier uncommitted implementation changes. Its code fingerprint identifies the measured bytes, but this is not yet a frozen release commit; running the harness against a clean checkout may differ until those changes are isolated and committed.

The six confirmed labels cover only compound requirements for items 10/11 against three manufacturers. They are valid retrieval labels, but cannot be treated as complete extraction gold or directly substituted for the 48 atomic requirements.

## Results

| Check | Observed |
|---|---|
| Local PDF read | 2/2 pages parsed; 48 source rows; 0.633s in the initial pass |
| Retrieval workload | 48 requirements × 3 manufacturers = 144 pairs per pass |
| Expected page recall | 3/6 at top 3; 6/6 at top 20 |
| Expected document recall | 5/6 at top 3; 6/6 at top 20 |
| Retrieval pass times | 25.336s first; 26.126s warm |
| Ranking stability | Same complete ranking fingerprint in both passes |
| Empty candidate lists | 0/144; candidate presence is not evidence correctness |
| Cached-only assessments | 14 review-required No evidence found; 130 pending generation |
| Confirmed match claims | 0; matching accuracy cannot be measured from these abstentions |
| Cached-only assessment time | 47.940s |
| Embedding startup | 34.659s using cached weights with external metadata checks disabled |
| Complete diagnostic elapsed | 145.425s, including two retrieval passes and frozen-snapshot preparation |
| Live generative calls / DB writes | 0 / 0 |

An earlier process measured 109.622s embedding startup and 25.907s retrieval before a diagnostic progress-output argument collision stopped it. Its saved results remain in `initial-diagnostic-results.json`; the collision was fixed in the diagnostic runner. Startup settings/cache state differ between processes, so these are not an isolated causal network-performance comparison. Neither total establishes a full-tender SLA: fresh generation, full source ingestion and verified matching are not complete.

## Correctness blockers found

1. **Extraction loses facets.** Visual and source-text review of page 2 shows `Screwed end` for item 10 and `Socket weld ends` for item 11. Neither is represented as a separate connection-type comparison requirement in the frozen stored extraction; size records can retain these words without assessing the connection. Assistant review proposals are in `extraction-review-proposals.csv`, explicitly not human-confirmed gold. Equal source/stored row counts do not establish extraction completeness.
2. **Original package integrity fails.** Two different attachment records name `raw/slafds`. The saved bytes match one record and fail the other's expected hash. The complete original tender cannot be certified from that package. Existing collection code already adds collision suffixes to new downloads; the historical package still needs recovery and validation.
3. **Verified-fact coverage is inadequate.** Under a client that raises before every remote request, 130/144 pairs require generation. They remain pending, not Missing capability or No evidence found. The other 14 abstain with review; no confirmed supplier result was published.

## What this supports

Top-20 retrieval reaches all six confirmed evidence pages on this small sample; top-3 page ranking remains weak. The larger immediate correctness issue is losing source facets before comparison, plus incomplete verified fact coverage. Do not tune retrieval to six labels or migrate embeddings based on this diagnostic alone. Keep classification review-only and evaluate general source-span/facet coverage on independent fixtures before claiming extraction completeness.

## Reproduce

From the repository root, with the existing local database/source packages and cached embedding weights available:

```powershell
venv\Scripts\python.exe eval\validate_fixed_tender.py
```

The runner uses a read-only repeatable-read database transaction, blocks all generative requests and records sanitized error types. It makes no production run/report or tender status update. Its fixed IDs/assertions define the evaluation fixture; they are not business-logic rules. Expected extraction answers still need independent human review before a full accuracy score is possible.
