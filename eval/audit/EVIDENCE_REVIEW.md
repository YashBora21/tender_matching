# Human evidence review

Open `evidence-review.csv`. It contains 28 real stored facts across six PDF pages, including repeated readings. It is an inventory to review, not 28 independent gold cases or an accuracy estimate.

For each row, open the document identified by `path` and verify its SHA-256 and cited page. Local paths resolve under `backend/data/manufacturers/<manufacturer>/` or `backend/data/catalogue/`; the model/manufacturer and document hash disambiguate duplicates.

These fields are for a person to fill; they currently hold **agent proposals**
filled on 2026-11-05, so treat every value as unconfirmed (see
`EVIDENCE_REVIEW_AGENT.md`):

- `human_attribute_correct`: yes/no/uncertain — does the source label actually name the extracted attribute?
- `human_value_correct`: yes/no/uncertain — does the extracted value belong to that label, row and column, with its units and conditions?
- `human_source_scope_correct`: yes/no/uncertain — does it apply to the stated model/product, rather than an actuator, accessory, another model or merely a family?
- `human_notes`: explain the deciding header/cell/condition, or what remains ambiguous.

`machine_issue` and `citation_passed` are diagnostics. A quote can exist on the right page and still be attached to the wrong attribute. `not_rejected_requires_human_review` does not mean correct. The `human_*` columns are not a human verdict until a reviewer accepts or overrides them.

The three wrong-column findings already have needs_review status. No additional currently verified facts failed this bounded automated check, so this audit changed no database rows.

To build a meaningful validation set, a reviewer must add independent cases spanning wrong columns, wrong rows, valve/accessory scope, merged cells, OCR and valid evidence across more source pages. The current six pages do not establish coverage of those cases. Agent-generated labels must not be treated as human gold.

Re-exporting overwrites this generated sheet; preserve a separately named completed copy before re-exporting. Run the exporter only against the intended development database. It uses a read-only transaction and makes no model calls.

A full-tender grouping comparison should use frozen reviewed cases, the same corpus/settings and candidate bounds, and separate cold/warm timings. Record status agreement, human correctness, failed-verification/review rates, calls, tokens, retries, table parsing time and wall time. Keep grouping disabled by default until those results support rollout.
