# Baseline - the six gold pairs, as the current pipeline answers them

Frozen 2026-11-03. **Do not edit these files.** Every speed-up is diffed against
this directory; a file changed here is a baseline nobody can trust.

## What this is

| File | What it holds |
| --- | --- |
| `report-4e7ebbc1.json` | The canonical report of run `4e7ebbc1-cdc3-442e-b9ee-680d3b30b700` (tender `GEM_9671792_Valve.pdf`, status `done`, corpus hash `28ea0d128c3f`, model `openai/gpt-oss-120b`) |
| `gold-items-10-11.jsonl` | `../gold/confirmed-GEM_9671792-items-10-11.csv` re-keyed to the field names `harness.py` reads (`true_status`→`status`, `gold_*`→`document`/`page`/`quote`, page as int) |
| `harness-4e7ebbc1.json` | Those six labels scored against that report |
| `recall-4e7ebbc1.json` | Retrieval recall@3 / @20 for the same labels, with every miss attributed |

No model call was spent to produce this directory: the run it describes had
already been executed. Reproduce with (from the repo root):

```bash
cd backend && ../venv/Scripts/python.exe -m app.services.output.report \
  4e7ebbc1-cdc3-442e-b9ee-680d3b30b700 --format json --out ../eval/baseline/report-4e7ebbc1.json
../venv/Scripts/python.exe eval/harness.py score --report eval/baseline/report-4e7ebbc1.json \
  --gold eval/baseline/gold-items-10-11.jsonl --out eval/baseline/harness-4e7ebbc1.json
../venv/Scripts/python.exe eval/recall.py --gold eval/baseline/gold-items-10-11.jsonl \
  --json-out eval/baseline/recall-4e7ebbc1.json
```

## The numbers

| Metric | Baseline |
| --- | --- |
| Status accuracy (6 verifiable labels) | **0.0** - all six pairs publish `no_evidence_found` |
| Confusion | 4 × `missing_capability` → `no_evidence_found`, 2 × `partial_match` → `no_evidence_found` |
| False-confident rate | **0.1667** (1 pair wrong and *not* flagged: `6ffe6648` × valmatic) |
| Coverage (answered with evidence) | **0.0** |
| Review rate | 0.8333 (5 of 6) |
| Citation correctness | `claims = 0`, so `null` - nothing published a quote to check |
| Document recall | @3 0.667, **@20 1.0** |
| Page recall | @3 0.333, **@20 1.0** |
| Misses by cause | `corpus_gap` 0, `chunking` 0, `retrieval` 0, **`verifier` 6** |

`unlabelled_predictions = 57`: the run answered 21 requirements × 3
manufacturers and only the 6 labelled pairs are scored.

## What the baseline says the work is

Retrieval is not the problem. Every gold page is in the top 20 and 6 of 6 misses
land in the `verifier` bucket, so the answer reaches the pair and then dies:

| Manufacturer | Stored `review_reason` | Why it died |
| --- | --- | --- |
| delval_flow | `the document declares Series 7/8 at family level; attribute_not_mapped` | the clause was never given a canonical attribute, so there was nothing to compare |
| Flowserve | `pressure class comparison needs a class on both sides` | the requirement row has `attribute`, `operator`, `value` and `unit` all `NULL` |
| valmatic | `the claim could not be verified: the quote is longer than the cited page` | the verifier rejected the proposed quote, and its `no_evidence_found` was not flagged for review |

Also frozen here: all six results cite an `evidence_ref` chunk id that no longer
resolves (`select count(*) from chunk where id = evidence_ref` = 0 for each).
The corpus was re-ingested after the run, so a citation re-check against the
current chunks cannot pass - that is why `citations.claims` is 0 rather than a
low correctness rate.
