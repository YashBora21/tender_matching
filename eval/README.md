# Evaluation harness (PRD 15, FR-35/FR-36)

Scores a report a run already produced against hand-labelled pairs. It is
offline on purpose: no API, no database, no model. The same gold file scores the
same report the same way on any machine, which is what makes the numbers
comparable between runs.

```bash
make eval REPORT=report.json GOLD=eval/gold/tender-a.jsonl
make eval-check GOLD=eval/gold/tender-a.jsonl
```

The report is the canonical JSON (`make report RUN=<run_id> FORMAT=json`); the
gold file is JSONL, one label per line.

## What is measured

| Metric | Definition |
| --- | --- |
| Status accuracy | Correct verifiable pairs ÷ labelled verifiable pairs |
| Per-status precision / recall / F1 | One-vs-rest over the verifiable statuses |
| False-confident rate | Wrong pair that was **not** flagged for review ÷ labelled verifiable pairs. The key metric: a wrong answer a reader would believe |
| Coverage | Pairs whose prediction is not `no_evidence_found` ÷ labelled verifiable pairs. It says how often the corpus answered at all; whether the answer was cited is the citation metric below |
| Review rate | Flagged predictions ÷ all labelled predictions |
| Citation correctness | Verified quotes ÷ predicted claims. A claim with no quote counts against; a source that is not on disk is reported and excluded, never counted as correct |
| Unsupported | Reported in its own block, excluded from every metric above (as it is excluded from the score) |

A quote counts as verified only if `app.services.verifier.check_citation` finds
it on the cited page — the same function that gates publication, so the
evaluation cannot be more forgiving than the product.

Two refusals keep the numbers honest:

- a label naming a requirement × manufacturer pair the report does not contain
  stops the run and lists the keys, instead of being dropped;
- a prediction with no label is reported as *unlabelled*, never counted as
  correct.

## The gold file

One JSON object per line:

```json
{"tender_sha256": "…64 hex…", "requirement_id": "…", "manufacturer": "Acme", "status": "full_match", "document": "acme-d-series.pdf", "page": 12, "quote": "Body: ASTM A351 CF8M (SS316)"}
```

| Field | Rule |
| --- | --- |
| `tender_sha256` | The tender's hash, as the report records it |
| `requirement_id` | The requirement's id **from the extractor's output**, so a label describes a requirement the system can actually be asked about |
| `manufacturer` | The manufacturer name as the report prints it |
| `status` | One of the five PRD statuses |
| `document`, `page`, `quote` | Required for `full_match`, `partial_match`, `missing_capability`: the label must quote the sentence that decides it. A label for another status must not carry them |
| `note` | Required for `no_evidence_found` and `unsupported_requirement`: where the labeller looked, or why it is not technically verifiable |

A label without a quote for a claim is not a label, it is an opinion;
`make eval-check` refuses it. A silent corpus is also a claim, so it needs a
note saying where the labeller looked.

`eval/gold/synthetic.example.jsonl` shows the shape. It is **synthetic** — it
scores nothing and says so in its notes. The real gold set is not written yet:
it needs requirements from an actual model run, and inventing labels without
one would be exactly the kind of unverified claim this project refuses to
publish (see `docs/WORKLOG.md`, Phase 10).

## Retrieval recall@k

The report scorer above asks whether the published status was right. Before
that question is the retrieval one: did the top *k* chunks even include the
page the answer is on?

```bash
make recall GOLD=eval/gold/tender-a.csv ARGS="--k 3 20"
```

The input is the review CSV (or JSONL) with `requirement_id`, `requirement`,
`manufacturer`, `item_id`, `item_title`, `gold_document`, `gold_page`, and
optionally `true_status`. `gold_document`/`gold_page` are the labeller's
answer, not the run's cited `document`/`page`.

A file may also carry `label_state` / `confirmed`. When it does, only the rows
the reviewer confirmed are measured. A proposed row is counted as
*unconfirmed* and left out; a file with only proposals is refused, because a
recall over labels nobody confirmed is not a measurement.

Every label that is not a hit is attributed to one of four causes:

| Cause | Meaning |
| --- | --- |
| `corpus_gap` | the gold document is not in the corpus |
| `chunking` | the document is indexed, but no chunk covers the gold page |
| `retrieval` | the page is indexed, but it is not in the top *k* |
| `verifier` | the gold page was retrieved, but the published status is wrong |

The harness refuses a label file with no rows, or with no confirmed row that
names a gold document and page: recall over an empty set is not a measurement.
A row without a gold page is counted as *unlabelled*, never as a miss.

## Not built

- The 50–100 hand-labelled pairs (PRD 15) — they need a live model to produce
  the requirements they label.
- Ablations, and the manual page audit (PRD 15) — both are manual work over a
  real corpus.
