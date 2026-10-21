# Tender Requirement Matching

A system that reads a tender PDF, extracts every technical requirement from it,
compares each requirement against a corpus of manufacturer documents, and
produces a compatibility report in which **every decision is backed by a
verified citation** (document, page, quote).

The design follows [`docs/PRD.md`](docs/PRD.md). The five status definitions
that the write-up must state are in
[`AGENTS.md`](AGENTS.md#status-definitions-prd-4--the-write-up-must-state-these).

## The one rule everything else serves

> **The LLM extracts and reads; deterministic code decides; a verifier gates
> every claim.**

An LLM may propose a candidate fact. It never decides full / partial / missing.
Every claim passes a four-layer verifier before it is published, and a failed
verification is never shown as a confirmed match. "No evidence found" is not
"Missing capability": absence of evidence is not evidence of absence.

## Statuses

| Status | Meaning |
| --- | --- |
| `full_match` | Documented capability meets or exceeds the requirement, with a verified quote |
| `partial_match` | Close but not meeting it, met only via an optional feature, or only part of a compound requirement |
| `missing_capability` | Documents state the capability exists with a failing value, or state it is not offered |
| `unsupported_requirement` | Not technically verifiable (commercial, legal, financial, warranty, vague). Excluded from the score, listed separately |
| `no_evidence_found` | Technical and verifiable, but the corpus is silent. Neither pass nor fail. Flagged for review, reported as coverage |

`status`, `verification_outcome` and `applicability` are **three separate
fields**. Uncertainty is recorded in the last two, never forced into one
status.

## Layout

```
backend/app/
  api/            # FastAPI routes
  services/       # atomizer, normalizer, verifier, evidence, scoring
  comparison/     # registry + evaluators, units, attribute vocabulary
  repositories/   # PostgreSQL access behind an interface
  schemas/        # Pydantic v2 models and enums
  core/           # config, logging, errors
backend/data/     # attribute_vocabulary.json (the attribute catalogue)
backend/alembic/  # migrations
backend/tests/    # pytest
docs/             # PRD, DECISIONS.md, WORKLOG.md
```

## Running it

```bash
make install-dev      # install runtime + dev dependencies
cp .env.example .env  # then fill in DATABASE_URL and the LLM keys
make migrate          # apply migrations
make run              # uvicorn on :8000
make docker-up        # or: pgvector + backend in containers
```

Tests: `make test` (all), `make test-unit` (no database needed),
`make test-integration` (needs `TEST_DATABASE_URL`).

Every threshold, weight, model name and path comes from the environment
(`backend/app/core/config.py`). There are no thresholds written into the
comparison code, the scoring code or the evidence logic.

## Inspiration

This project stands on two pieces of work, and it would be dishonest not to
say which is which. **No code was copied from either.** The engine here is
written against this repository's own PRD statuses, its own data model and its
own tests.

### The PRD and the assignment

[`docs/PRD.md`](docs/PRD.md) and
[`docs/Tender_Matching_Intern_Assignment.pdf`](docs/Tender_Matching_Intern_Assignment.pdf)
are the source of truth for everything in this repository: the five statuses,
the separation of status / verification / applicability, the page ledger's
four states, the "no evidence found ≠ missing capability" rule, the scoring
model, and the requirement that every decision be backed by a verified
citation. Where this README or the code describes a rule, that rule comes from
the PRD.

### [BidLint](https://github.com/yigitcan-ozturk/bidlint) — yigitcan-ozturk

BidLint is a compliance-checking tool for tender specifications against vendor
packages. It is a different system with a different vocabulary
(`PASS` / `DEVIATION` / `MISSING` / `REVIEW`), and none of that vocabulary was
adopted. But three of its documented design decisions were good enough to be
worth re-deriving independently, and they are the direct answer to the
"you are hardcoding the thresholds" problem.

Only three of its documents were read: `docs/STABLE_CONTRACT.md`,
`docs/ENGINEERING_UNITS.md` and `docs/VENDOR_PACKAGES.md`. The ideas below are
the ones taken, with the reason each was taken and where it landed here.

#### 1. Tolerance as policy, not as code

*Source:* `ENGINEERING_UNITS.md` ("Decision behavior") and the frozen-status
discipline in `STABLE_CONTRACT.md`.

BidLint keeps the tolerance that separates a pass from a deviation in a policy
object rather than in the comparison code. We took the **shape** of that idea:
every threshold the evaluators use now lives in `ComparisonPolicy`
(`backend/app/core/config.py`) and is read from the environment at the moment
the comparison runs, not snapshotted at import time.

*Why:* PRD §13 forbids thresholds in business logic, and the whole complaint
that started this work was that the comparison code was full of literals.
*Where:* `backend/app/core/config.py` (`ComparisonPolicy`),
`backend/app/comparison/registry.py` (`default_policy()`, `request.rules`),
`backend/tests/test_comparison.py` (two tests that change the environment and
watch the verdict change).

#### 2. Dimension-explicit unit conversion

*Source:* `ENGINEERING_UNITS.md` ("Deliberate limits").

BidLint refuses to convert between units that share a dimensionality but not a
physical quantity: `Hz` ≠ `rpm`, `kW` ≠ `kVA`, `psig` ≠ `psi`. We re-derived
the same rule independently on top of `pint`, which is permissive in exactly
the wrong way — it will happily convert a 50 Hz motor into a 477 rpm pump.

*Why:* a conversion that is not physically meaningful manufactures a false
match, and a false match is worse than no answer.
*Where:* `backend/app/comparison/units.py` (`DIMENSION_OVERRIDES`,
`_DIMENSION_UNITS`, `is_compatible`), `backend/tests/test_units.py`.

#### 3. Document classification, evidence priority, and conflict → review

*Source:* `VENDOR_PACKAGES.md` ("Document classification", "Explicit evidence
priority", "Conflicting evidence without priority", "Package evidence audit").

Three linked ideas, re-implemented against this PRD:

- **Filename-based document classification.** A supplier's file set is not a
  uniform evidence pool. A datasheet, a compliance schedule, a technical offer
  and a copied employer specification do not carry the same authority, and a
  commercial pricing sheet is not technical evidence at all. A specification
  can never satisfy a requirement — it *is* the requirement.
- **Priority resolves a class-level conflict, never a tie inside one class.**
  The priority order is configuration (`EVIDENCE_PRIORITY`). If a compliance
  schedule and a datasheet disagree, the higher-priority class wins. If two
  compliance schedules disagree, the result is a conflict and goes to review —
  priority never breaks a tie within the same class.
- **Every fact's fate is recorded.** Each piece of evidence ends as
  `selected`, `equivalent-duplicate`, `conflict` or `lower-priority`, so
  nothing disappears silently.

*Why:* PRD FR-25 and §10 require conflicts to be surfaced, not resolved by
guessing. A system that picks a winner and hides the disagreement is
indistinguishable from one that never saw the second document.
*Where:* `backend/app/services/evidence.py` (`DocumentClass`, `Disposition`,
`consolidate`), `backend/tests/test_evidence.py`.

#### 4. A terminology vocabulary that is data, not code

*Source:* `VENDOR_PACKAGES.md` ("Project terminology aliases").

BidLint lets a project map its own terminology onto canonical parameters
without touching the code. We took that as the reason to move the attribute
catalogue out of Python and into a JSON file with an env-configurable path, so
adding an attribute or a synonym is a data edit.

*Why:* PRD FR-6 and FR-27 want categories and attributes to be extensible
without code changes.
*Where:* `backend/data/attribute_vocabulary.json`,
`backend/app/comparison/vocabulary.py`, `backend/app/comparison/attributes.py`,
`backend/tests/test_vocabulary.py` (loads an alternative vocabulary and shows
both new attributes being recognised and old ones stopping being recognised).

#### What was deliberately **not** taken

- **BidLint's status vocabulary.** `PASS` / `DEVIATION` / `MISSING` / `REVIEW`
  is a different model with a different denominator. This project uses the
  PRD's five statuses, with `status`, `verification_outcome` and
  `applicability` kept separate, and PRD §8's own scoring rules.
- **BidLint's scoring denominator.** BidLint excludes `REVIEW` findings from
  the denominator. Here, `unsupported_requirement` is excluded from the score
  and reported separately, while `no_evidence_found` counts towards
  *coverage* — PRD §8, not BidLint's rule.
- **Its CLI, its JSON report schema and its package format.** This project
  has its own API and its own report schema, driven by the PRD.
- **Any code.** Nothing was copied, transliterated or adapted line by line.

## Current state

Phases 0 and 1 are built and tested. See
[`docs/WORKLOG.md`](docs/WORKLOG.md) for what each phase built, what was
tested, and what is still open, and [`docs/DECISIONS.md`](docs/DECISIONS.md)
for the decision log.

The retrieval and LLM extraction pipeline, the corpus collector, the report
and run API endpoints, the React frontend and the evaluation harness are not
built yet. The comparison, verification, evidence and scoring layers they will
call are.
