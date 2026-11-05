# Agent-filled evidence review — NOT a human audit

`evidence-review-agent-filled.csv` was filled by an AI agent on 2026-11-05. It is
a proposal, not the review the sheet exists for.

`EVIDENCE_REVIEW.md` says the `human_*` fields are to be filled by a person and
that agent-generated labels must not be treated as human gold. This file does
not change that: **status accuracy, false-confident rate and citation
correctness remain unmeasured** until a person reviews these rows and accepts
or overrides them. The `human_*` column names are kept so the sheet stays one
schema; the provenance is this file.

## What was checked, mechanically

- **SHA-256:** all 28 rows match the file on disk.
- **Citation:** all 28 quotes were re-found in the cited page text (whitespace
  normalised), so `citation_passed = True` holds for every row.
- **Attribute / value / scope:** judged against the cited page's own labels and
  table structure, not against the extraction.
- No database row was read for a write; no model call was made.

## Counts

| Column | yes | no | uncertain |
| --- | ---: | ---: | ---: |
| `human_attribute_correct` | 19 | 7 | 2 |
| `human_value_correct` | 25 | 3 | 0 |
| `human_source_scope_correct` | 22 | 5 | 1 |

## What the review found

1. **Flowserve nominal size (3 rows, already `needs_review`).** The quote is the
   *actuator* size row (`Actuator 1/2“ … 4“ - 6“`). `6` is the largest actuator
   size, not a valve nominal size. Attribute, value and scope are all wrong.
2. **AVK 13.7 bar (2 rows).** The value is real, but the exact string
   `13.7 bar (200 PSI)` belongs to **Series 55, DN450-600**, while the fact names
   **Series 06/71/820**. Scope is wrong, and the page gives a bare rating line —
   it never labels it operating or working pressure.
3. **CIRCOR, four facts with an empty attribute.** The claim names no attribute
   at all. The value (150 / Class 150) is genuinely one of the listed classes;
   the defect is the missing label, not the number.
4. **Bray.** `RF15: ASME Class 150` is correct, but `RF30` is Class 300, so the
   named product ("Ball valve range") is not uniformly Class 150 — scope is
   family-level, marked `uncertain`.
5. **Duplicates.** 17 of the 28 rows are repeated readings of the same claim
   (10 × Flowserve Class 150, 4 × CIRCOR Carbon Steel, 3 × Flowserve nominal
   size). The sheet holds **nine distinct claims**, not 28 independent cases.
   The `table_count` column already says this; it should not be read as a
   sample size.

## What a person still has to do

Open each cited page for the rows marked `uncertain` or `no`, then override the
three columns where your judgement differs and replace `human_notes` with what
decided it. Only then is the sheet a human audit, and only then can any
accuracy figure be quoted from it.
