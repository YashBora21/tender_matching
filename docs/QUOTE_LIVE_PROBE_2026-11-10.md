# Quote-contract live probe and remaining work — 2026-11-10

The two-call free-tier probe passed response-schema and quote/value support
checks for both known clauses. This is a regression result, not unseen-tender
accuracy or an end-to-end matching pass.

| Measurement | Result |
| --- | --- |
| Requests / retries | 2 / 0 |
| Item 10 extraction | 1.270 seconds; 6 supported quotes, 0 rejections |
| Item 11 extraction | 1.221 seconds; 6 supported quotes, 0 rejections |
| Schema errors | 0 |
| Unresolved measurement units | 0 |
| Local source preparation | 0.658 seconds |
| Probe time excluding local preparation/imports | 2.497 seconds |
| Reported input / output tokens | 1,720 / 1,048 |

Calls used the designated free-tier configuration, isolated to the process, with
no paid fallback. Private configuration strings were redacted before local
artifact writes. Main provider configuration was not modified. No database
connection, requirement refresh, result write or confirmed match occurred.

## Acceptance gaps still visible

Both clauses remain flagged for review by the production pipeline replay:

- Four distinct proposed attribute names lack approval: `valve type`,
  `face to face distance`, `valve size`, and `end connection type`. These affect
  six proposals across the two clauses. No aliases were silently approved.
- One pressure-class proposal was typed as certification. Its technical meaning
  needs review; correct source quoting does not establish correct clause typing.
- Size values were copied as `2"` with unit `inch`. Provenance is supported, but
  the numeric comparison helper expects a numeric string. A deterministic typed
  canonicalization check is still needed; stripping arbitrary characters would
  be unsafe for ranges, identifiers and material grades.
- Source/model/component/condition binding and the manufacturer comparison were
  not exercised by these two extraction calls. Existing review gates stay active.

The old offset failures did not recur in this small probe, and the former
classification-label-as-unit proposal did not recur. Two known cases do not
establish a general failure rate or p95 latency.

## Four completion gates

1. Finish extraction acceptance: approved attribute/alias handling, typed numeric
   normalization and clause-type checks, retaining raw source provenance.
2. Refresh the 48 legacy requirements while preserving historical reports, and
   validate vendor model/component/condition evidence on reviewed cases.
3. Run complete fixed and unseen tenders against reviewed gold labels, including
   false-confident claims, abstention causes, grouping off/on and total runtime.
4. Consolidate the outstanding integration changes into reviewed commits; check
   migrations and the complete upload/read/resume/report flow on a frozen build.

The core pipeline is implemented. Remaining work is correctness/data acceptance
and release integration. A planning estimate is roughly 3–6 focused working days
for an assignment-ready release if these gates reveal no major new failures;
this is not a delivery guarantee or a production-reliability estimate.

Next, address typed canonicalization and reviewed vocabulary/type handling using
the retained responses offline before making more provider requests.

Local artifacts: `eval/audit/quote-contract-2026-11-10/live/summary.json` and the
two retained proposal files. No code, prompt or cache-key change in this phase.
