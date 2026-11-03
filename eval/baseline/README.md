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
| `citations-4e7ebbc1.json` | Every quoted claim in the whole report, re-checked against the PDF page text it cites |
| `report-4e7ebbc1.frozen.json` | The same report with the database ids stripped out of every evidence block - the copy later diffs are taken against |

No model call was spent to produce this directory: the run it describes had
already been executed. Reproduce with (from the repo root):

```bash
cd backend && ../venv/Scripts/python.exe -m app.services.output.report \
  4e7ebbc1-cdc3-442e-b9ee-680d3b30b700 --format json --out ../eval/baseline/report-4e7ebbc1.json
../venv/Scripts/python.exe eval/harness.py score --report eval/baseline/report-4e7ebbc1.json \
  --gold eval/baseline/gold-items-10-11.jsonl --out eval/baseline/harness-4e7ebbc1.json
../venv/Scripts/python.exe eval/recall.py --gold eval/baseline/gold-items-10-11.jsonl \
  --json-out eval/baseline/recall-4e7ebbc1.json
../venv/Scripts/python.exe eval/harness.py cite --report eval/baseline/report-4e7ebbc1.json \
  --out eval/baseline/citations-4e7ebbc1.json \
  --frozen-report eval/baseline/report-4e7ebbc1.frozen.json
```

## The numbers

| Metric | Baseline |
| --- | --- |
| Status accuracy (6 verifiable labels) | **0.0** - all six pairs publish `no_evidence_found` |
| Confusion | 4 × `missing_capability` → `no_evidence_found`, 2 × `partial_match` → `no_evidence_found` |
| False-confident rate | **0.1667** (1 pair wrong and *not* flagged: `6ffe6648` × valmatic) |
| Coverage (answered with evidence) | **0.0** |
| Review rate | 0.8333 (5 of 6) |
| Citation correctness, six labelled pairs | `claims = 0`, so `null` - all six publish silence, and silence carries no quote to check |
| Citation correctness, whole report | **0.375** - 16 distinct quoted claims (`27` predictions): 6 verified, 10 failed |
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

## How a citation is checked here

A result is keyed by **(document sha256, page, normalised quote)** and the quote
is looked for in the text of that PDF page on disk - not in a database row. That
is what `harness.py cite` does, and it is the number above.

`result.evidence_ref` stores `evidence["fact_id"]` (`app/services/output/runs.py:629`);
all 63 results of this run resolve to a `fact` row, and every evidence block
carries the document hash, page and quote it came from, so 16 of 16 quoted claims
could be re-checked against a file that is still in `backend/data/`. Nothing here
is unreproducible.

What the check found: **all 10 failures are Val-Matic** (`VMC-4702AAL-AAW_11-18-14.pdf`
p1, `Series-51-53-v5b` p3 and p6, `Series-58-2022-M2` p3), and all 10 have the same
reason - `the quote is longer than the cited page`. On those pages PyMuPDF *and*
pdfplumber both return **0 characters**; the quoted text is only in the database as
chunks stored with `ocr = true`. So the quote is real, it is on that page, and the
verifier cannot see it because it re-reads the page's text layer and an OCR page
does not have one.
