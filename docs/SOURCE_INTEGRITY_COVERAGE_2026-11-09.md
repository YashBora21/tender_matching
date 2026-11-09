# Source integrity and coverage checkpoint — 2026-11-09

The source package is readable again. Extraction and fresh matching have not
passed end-to-end validation; no new confirmed match was published.

## Source recovery and safeguards

Recovered the overwritten 24-page attachment from its recorded public source.
Its bytes exactly match the manifest's original SHA-256. The surviving 17-page
attachment was preserved. Only the recovered attachment's path metadata changed;
historical requirements and matching results were not replaced.

Frozen-source validation checks paths and checksums before ingestion cache reuse
and new/resumed matching. Stored extraction prompt versions are checked before
matching. All 48 historical atomic rows in the two-page subset require a validated
refresh; the existing replacement path can delete historical results, so it was
not invoked on these tenders.

The integration retains original source text, native table headers, row identity
and inherited header page. Structured-source coverage compares approved attributes,
values, units and operators with source tokens. Unrepresented tokens retain a
review warning, including for unreadable or empty-output clauses. This is a
conservative review trigger, not proof of semantic completeness.

## Measurements

| Check | Observed result | Interpretation |
| --- | --- | --- |
| Recovered full package, local read/staging | 60/60 pages parsed; 0 failed/unaccounted; 45.171 seconds | Includes extensionless PDF; no remote generation |
| Candidate clauses from that package | 927 across 11 items | Candidates, not validated requirements |
| Frozen two-page diagnostic | 149.417 seconds | Retrieval and cached-only assessment, not full matching time |
| Retrieval, 144 pairs | 24.743 seconds first pass; 23.651 warm | Rankings unchanged |
| Confirmed source-page recall | 3/6 at top 3; 6/6 at top 20 | Six reviewed compound cases, not whole-tender accuracy |
| Cached assessments | 14 review/no-evidence; 130 pending generation; 0 confirmed | Pending is not manufacturer silence |
| Retrospective source coverage | 26/48 rows trigger review | Not 26 confirmed errors; includes the omitted connection facets |
| Frozen source hashes | 0 failures | Recovered package checks pass |

The full-package read intentionally stopped before generation: one blocked local
attempt, zero remote calls. Its ledger is an evaluation artifact, not a replacement
for the historical database ledger.

## Bounded fresh validation

Two free-tier requests were made, with zero retries and no paid fallback:

- A complete source clause produced an invalid extraction proposal. The source was
  retained with a coverage warning. Raw response was not retained, so the precise
  rejection cause is not established.
- An independent atomic matching probe produced an invalid fact proposal:
  `requirements.1.operation` was rejected as an extra field; `operator` is expected.
  The `requirements` envelope itself was correct. Validation stopped the probe.

Reported usage across the two requests: 3,145 input and 711 output tokens. No
requirement, fact or matching-result writes were made by these probes. The internal
budget-stop exception was used to stop after invalid output; it was not provider
quota exhaustion. No additional generative calls were made.

## Remaining boundary

Keep grouping opt-in and automatic promotion disabled. Fix and offline-replay the
general proposal contract before another bounded live test, then refresh extraction
without destroying historical reports. Whole-tender correctness and the accepted
2–4 minute target remain unproven. Six compound gold labels cannot validate all 48
atomic rows. The broader OCR/source-context integration remains mixed with earlier
uncommitted work; the independently committed utilities and matching gates are not
a frozen release of that integration.

Local evidence: `eval/audit/fixed-incorpus-2026-11-09/` contains the replay,
retrospective coverage, full-package accounting and bounded-probe summaries.
