# Fixed-source diagnostic with page caching — 2026-11-09

The diagnostic completed with zero generative requests and every database
transaction forced read-only, including transactions opened after matching
released an earlier one. It exercised the existing two-page specification subset,
not end-to-end extraction/matching of the complete tender package.

| Check | Observed result |
| --- | --- |
| Frozen original attachment hashes | 0 failures |
| Local subset read | 2/2 pages parsed, 48 source rows, 0.672 seconds |
| Historical extraction versions | 0 current, 48 outdated; new production matching requires refresh |
| Local CUDA embedding startup | 113.487 seconds in this fresh process |
| Retrieval workload | 144 pairs per pass; no empty candidate lists |
| Retrieval first / warm pass | 22.985 / 24.137 seconds |
| Reviewed page recall | 3/6 at top 3, 6/6 at top 20 |
| Reviewed document recall | 5/6 at top 3, 6/6 at top 20 |
| Cached-only assessment | 144 pairs, 33.414 seconds |
| Generation blocked | 130 pending pairs |
| Other assessed pairs | 14 review-only No evidence found; verification not checked |
| Page-context lookups | 9 disk hits, 24 cold misses, 111 memory hits |
| Context lookup/preparation time | 11.990 seconds, included in assessment time |
| Total diagnostic | 206.697 seconds |

Rankings match the previous frozen diagnostic exactly, including the workload
fingerprint. The additional 24 page contexts are now cached. All context hits
still verify source hashes. This diagnostic intentionally bypasses production
admission to inspect historical rows; it does not approve their reuse.

Assessment time was 49.652 seconds in the earlier diagnostic and 33.414 seconds
here, but code, verification policy and process conditions also changed. This is
not a controlled cache-only speed comparison. The separate 76-page cold/warm
cache benchmark remains the direct cache measurement. Embedding startup dominates
this fresh-process diagnostic; the existing process-local model cache can reuse
weights in a long-lived backend, but this run does not measure warm API latency.

## Saved extraction failures

The saved six-proposal response still has five rejected spans. Review causes
overlap:

- Four proposed values are supported somewhere in the full clause but their
  declared spans omit them or clip source tokens. The fifth rejected proposal
  changes a plural source value into a singular paraphrase, which the literal
  support gate does not approve.
- Two attribute names are absent from the approved vocabulary: `valve type`
  and `face to face distance`. They were not silently approved or discarded.
- `ASME class` was proposed as a unit but does not resolve as a measurement unit.

The proposed text happens to occur exactly once in the source for all six rows.
This is diagnostic evidence only: no paraphrase was retroactively treated as an
authoritative quote, no offsets were repaired, and no requirements were accepted
or written. It does not validate the proposals' technical meaning.

## Next correctness phase

Replace the requirement extraction contract's LLM-counted character offsets with
an explicit verbatim source quote, located uniquely by deterministic code. Copy
source values without semantic rewriting. Missing, repeated or unsupported quotes
must remain review gaps; keep unit and vocabulary uncertainty separate. This is a
proposed next contract change, not implemented in this phase. It needs its own
prompt/cache version and decision entry, then synthetic/offline replay before any
bounded live test. Preserve historical reports during later extraction refresh.

No generative requests, database writes, historical refresh or confirmed claims
occurred. Six compound gold labels do not establish accuracy for all 48 atomic
requirements. Full-tender correctness, grouping equivalence and processing time
remain unproven, and wider production integration remains uncommitted.

Evidence: `eval/audit/fixed-incorpus-2026-11-09/cache-replay/results.json` and
`eval/audit/fixed-incorpus-2026-11-09/saved-extraction-failure-causes.json`.
