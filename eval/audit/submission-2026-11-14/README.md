# Measured submission candidate — 2026-11-14

**Performance targets met; end-to-end acceptance remains incomplete.**
RC `rc-2026-11-14.1`, commit `2e3627a820d08a0343c102edec834b4208a40c22`.
Frozen saved valve package:60 pages (17 wrapper/43 attachments),11 BOQ items.
All source hashes were verified before calls. Collection/download time is excluded.

| Metric | Measured |
| --- | ---: |
| Total wall time | 130.856s |
| HTTP calls | 31 |
| Input/output tokens | 62289 / 20033 |
| Estimated recorded INR | 6.8782 |
| Unresolved child rows | 108 |
| Distinct unresolved role/page/source groups | 68 |
| BOQ technical candidates | 21 |
| Reported cache hits | 11 |
| Context/form/contract calls | 0 |

| Purpose | Calls | Input tokens | Output tokens | Estimated INR |
| --- | ---: | ---: | ---: | ---: |
| extraction | 24 | 47998 | 16367 | 5.5326 |
| repair | 3 | 6129 | 2184 | 0.7300 |
| classification | 4 | 8162 | 1482 | 0.6156 |

Extraction requests:23 batches of8 plus1 single;3 bounded8-clause repair calls.
Classification requests:3 batches of10 plus1 batch of3. No rate limit, provider
timeout or spend-cap stop. Native ledger totals agree; no unsettled reservations.
Budget cap was INR25, recorded monthly capINR1000. Earlier unrecorded billing is
unknown. The verified tariff uses a conservative100 INR/USD budgeting assumption.
No provider/model/key/endpoint values are included in these artifacts.

Stage wall time:items0.535s; source reading
14.829s; extraction including client calls/cache/
validation78.826s; final persistence
0.543s; other orchestration
36.123s. Client attempt p50/p95
2.391/2.837s
includes native-ledger overhead. Local embedding/model loading and manufacturer
retrieval were not exercised. This cache-assisted saved-package measurement is
not a fresh large scanned-tender guarantee.

## Remaining acceptance work

108 child rows are unresolved; causes overlap:78 source-coverage flags,35 missing
item assignments,29 unresolved clause types.198 rows have per-requirement review
flags, a separate metric. The parser's39 ambiguous property scopes were preserved.
The paid result retains21 BOQ technical candidates, above the18-child preservation
regression baseline; this count is not an accuracy label.

Inspection shows several coverage flags name already-extracted open-vocabulary
labels (for example field-label words absent from a value-only quote). Diagnose
coverage/type accounting offline against verified row/header anchors; do not
remove the verifier or automatically approve canonical aliases. Blank/merged-cell
scope and human vendor-evidence verdicts remain separate review work.

The detailed source-review queue is kept privately by the project owner and is excluded from GitHub. No new manufacturer comparison was
started; incomplete reading must not become a manufacturer capability failure.
Accuracy and false-confident counts remain **ungraded**, not zero. All previous
reading attempts and4564 superseded rows remain preserved. Grouping stays off.

Offline suite:1365 passed/6 skipped. Do not describe this RC as a full acceptance
pass or production-ready system. No further paid rerun was made.
