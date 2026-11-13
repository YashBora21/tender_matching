# Valve re-read: failed acceptance, 2026-11-13

Measured code: `c306e3c9bbaa021fd3d79ad21c7e836eaf5809c8` (`rc-2026-11-13.4`). Same frozen valve package,
same four attachment hashes,60 parsed pages:17 wrapper/43 attachments.
Collection/network downloading is excluded. All historical attempts and rows retained.

The read stopped before another maximum-cost reservation would exceed INR25.
It is not a completed-tender cost benchmark.147 HTTP attempts produced146 answers;
one provider timeout took120.148s and retains an INR1.2301 conservative reservation.
Known tokens:192554 input/67491 output. Total recorded bound:INR23.8861.
Tariff was checked against the configured text target privately; INR100/USD is
a conservative budgeting assumption, not a market exchange-rate claim.
Monthly pre-ledger billing remains unknown.

| Purpose | Calls | Input tokens | Output tokens | Recorded INR |
| --- | ---: | ---: | ---: | ---: |
| extraction | 129 | 164530 | 57095 | 20.4456 |
| repair | 15 | 19828 | 9608 | 2.9974 |
| classification | 3 | 8196 | 788 | 0.4431 |

Matching:0 calls. Matching batch setting8 is configured, not exercised.
The native ledger and per-request journal agree. One unresolved reservation is
not represented as known zero spend. Classification's three new live calls used
prior valid caches and new batching: this is not an uncached226-to3 comparison.
Independent saved-output replay packs273 clauses into31 requests,0 changed labels,
0 external calls. It validates scheduling/evidence, not fresh grouped generation.

Two measurement-guard failures were corrected without changing app verification:
literal2. occurred in both a form and a specification; transformed text also
invalidated a raw-row lookup. Final instrumentation uses originating records.
Saved answers were replayed without HTTP. The four append-only attempts include
both failures and the offline repair; details and stage wall times are in results.
Paid-attempt wall time:510.029s; with offline
instrumentation passes:590.081s.
These are cache-assisted partial reads, not cold complete-tender latency.

## Correctness and remaining blocker

- BOQ technical candidates:broken stored revision3 -> latest21. Synthetic
  preservation regression remains18 ->18; candidate counts are not accuracy labels.
- Form attachment269 retained context rows:0 attributable model calls.
- Unresolved child rows:460 ->892 (acceptance failed). The latest892 children
  span186 distinct role/page/source-text groups; these are different metrics.
-825 specification children have no safe BOQ item assignment; causes overlap.
-944 of1564 active rows carry review flags.3000 superseded rows remain stored.
- No manufacturer comparison, confirmed-match or false-confident accuracy grade.
  The six confirmed compound manufacturer labels do not grade full extraction.

Read-only inspection found that the largest repeated source windows have a
page-table ID but **null source_row_id**. One302-character source has104 children.
Without a row identity the resume scheduler can re-extract each child as its own
source. A later offline follow-up `30b627c34a8e792cb5ad1315c4a43ec4d10ebae7` assigns stable page-local prose
ordinals before atomization and bumps Reader18;78 targeted tests pass.
It has **not** received another paid test. Prompts and valid clause-cache keys
are unchanged; source checkpoints must rebuild. Existing duplicates are preserved.

RC4 full backend unit suite:1242 passed/3 skipped. Disposable PostgreSQL checks:10
passed. Later row-identity fix:78 targeted passes. Four-good-fields/one-rejection
saved replay:2 replayed requests/1 repair/0 external requests.

No more paid reruns. Next approved direction is primary specification/BOQ sources
only, other source text retained as context; that stricter policy is **not yet
implemented** in this measured RC. Then resolve item binding and batch extraction.
Grouping stays off. Storage deletion is not the performance fix.
