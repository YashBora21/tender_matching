# Offline saved-response replay â€” 2026-11-10

The two existing proposal files contain 12 rows for valve items 10 and 11.
The replay checks frozen input hashes and each original quotation against
its item block on page 2 of the frozen specification. Raw proposals and source
values remain unchanged. Policy fingerprints are recorded in `replay.json`.

Result: 12 derived attribute fields change: six map to canonical attributes,
six remain unmapped and have a null derived attribute. One derived value
changes from `2"` to `2`; one proposed type changes from certification to
technical for the item 11 pressure-class specification. Six rows have usable
comparison inputs; six abstain because their attribute is unmapped.

These are input-processing outcomes. Retrieval, verification and matching
were not run. Accuracy, false-confident claims and confirmed matches are
unknown. No LLM calls or network operations were used.

Reproduce from the repository root using the project's configured local
environment:

```powershell
venv\Scripts\python.exe eval/replay_proposal_normalization.py --manifest eval/audit/source-normalization-2026-11-10/inputs.json --output eval/audit/source-normalization-2026-11-10/replay.json
```

`pump-labels-unconfirmed.json` and its CSV are proposals from the actual pump
PDF, not gold labels or synthetic facts. Page 4 was rendered and visually
checked (`pump-page-4.png`). All manufacturer-match and verification outcomes
remain unknown. Confirm the six rows, item binding and comparison semantics
before running the release-candidate evaluation. Resolve these source issues:

- The filename/manifest identifier differs from printed bid GEM/2026/B/8073822.
- Maximum discharge is 10 m3/h, while rated flow must exceed 15 m3/h.
- The local package manifest has no downloaded attachments.

Focused replay contract checks: two passed. No full unit suite was run here.

The user put this pump file on hold on 2026-11-10 and will identify the intended file. Its proposed rows are not approved evaluation inputs.
