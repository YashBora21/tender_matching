# Live proposal-contract probe — 2026-11-09

Two independent free-tier calls passed the revised response schemas. This is a
contract result, not a successful end-to-end match. No database connection,
historical refresh, requirement/fact writes or confirmed matches occurred.

| Measurement | Result |
| --- | --- |
| Requests / retries | 2 / 0 |
| Requirement extraction request | 1.149 seconds |
| Single-page fact request | 1.355 seconds |
| Combined probe wall time | 2.508 seconds, excluding local source preparation |
| Reported input / output tokens | 4,213 / 808 |
| Schema errors | 0 for both responses |

Configuration was isolated to the process using the designated free-tier key.
No paid fallback, provider/configuration edits or further calls were made.

## Extraction findings

The complete item-10 clause produced six proposals: construction/type, size,
connection, class, material and face-to-face distance. Connection is now separate
from size. The pipeline replay preserved the source and retained a review warning.

The warning is partly vocabulary scope: `valve type` and `face to face distance`
are not approved attribute names in the current vocabulary. They were not silently
approved. The warning is not proof that the model omitted those facets.

Offline source replay also exposed a separate provenance defect: the current
`span_holds` check verifies bounds and nonempty text, not whether the proposed
value is supported inside the span. Three spans omitted their proposed literal
value: size 2, complete material text, and dimension 122. All three values occur
elsewhere in the complete clause. Schema success therefore does not establish
correct evidence binding. No acceptance or source-offset repair was applied.

## Fact findings

Four proposals identify model/series variants and quote `Screwed end`, which occurs
on the checked page. The PDF page has no machine-detected tables. Model-specific
binding remains unverified; quote presence alone cannot establish it. The existing
source-anchor check rejects one proposed condition because it is absent from the
stored source context. No four-layer verified matching verdict was computed.

## Next bounded phase

Strengthen the general span-support gate so out-of-span values remain rejected
review gaps, with synthetic tests for valid, wrong-span, range and normalized
representations. Do not add source-specific offset fixes or silently approve new
attributes. Then replay these retained responses offline before spending on another
live request. Model-specific binding on pages without detected tables remains open.

Local raw proposals and source-check summaries are under
`eval/audit/fixed-incorpus-2026-11-09/contract-v3/`. Private configuration strings
are redacted before artifact writes. Two samples cannot establish latency p95,
whole-tender accuracy or the 2–4 minute processing target.
