# Decision log

Why the code is the way it is. Newest last. One entry per decision, with the
alternatives that were rejected and why.

Format: **D-n — title**. *Decision* / *Why* / *Alternatives*.

---

**D-1 — Phase scope: scaffold only.**
*Decision:* Phase 0 delivers repo layout, dependency manifests, `.env.example`,
`Makefile`, `docker-compose`, backend `Dockerfile`, migrations, Pydantic schemas and
enums, `GET /api/health`, tests and `AGENTS.md`/`CLAUDE.md`. No retrieval, LLM,
verifier, comparison registry, frontend or collector.
*Why:* The user asked for one phase at a time with an approval gate between them.
*Alternatives:* Building the whole pipeline in one go was rejected — it would make
review harder and hide design errors until much later.

---

**D-2 — Migrations run through Alembic on SQLAlchemy 2.0 Core.**
*Decision:* `backend/alembic/` with `env.py` reading `DATABASE_URL` from application
config; table definitions live in `backend/app/repositories/schema.py` as one
`MetaData`.
*Why:* User-approved. Versioned revisions, `compare_type=True` for autogenerate, and
a single place where the DDL is defined.
*Alternatives:* Plain `.sql` files with a hand-written runner was the lighter option
but has no autogenerate and drifts as the schema grows.

---

**D-3 — Added an `attribute` table.**
*Decision:* New table `attribute(id, canonical_term, value_type, unit_default,
notes)`; `fact.attribute_id` is a real FK to it, and `alias.canonical_term` names a
canonical term in it.
*Why:* PRD §7 lists `fact.attribute_id` but defines no `attribute` table. The
comparison registry (Phase 1) needs a canonical attribute vocabulary to select an
evaluator, and `value_type` on the attribute is where that mapping lives.
*Alternatives:* Keeping `attribute_id` as free text — rejected, it gives the
evaluator registry no stable key and no place to hang a default unit.

---

**D-4 — Repository layer is interfaces + connection layer only.**
*Decision:* `repositories/base.py` defines `DocumentRepo`, `FactRepo`, `RunRepo`
protocols; `repositories/db.py` provides the engine, `session_scope()` and `ping()`.
No CRUD yet.
*Why:* User-approved. CRUD lands in the phase that needs it, so nothing in the tree
is dead code.
*Alternatives:* Full CRUD for all 15 tables in Phase 0 — rejected, it would be
written before it is used and would churn.

---

**D-5 — Enums stored as VARCHAR + CHECK, not native PostgreSQL enums.**
*Decision:* `sa.Enum(..., native_enum=False, create_constraint=True)`.
*Why:* Adding a value to a native PG enum needs `ALTER TYPE ... ADD VALUE` inside a
transaction block, which is fiddly; a CHECK constraint needs a drop-and-recreate
migration, which is easier to review.
*Shortcut:* enum values are also checked at the database level, so adding a new
status means editing the migration for the new database *and* the CHECK on existing
databases. Upgrade to native enums if the vocabulary ever stops changing.

---

**D-6 — `chunk.embedding` is an unbounded `vector` column with no ANN index.**
*Decision:* `Vector()` with no dimension; no HNSW/IVFFlat index in the initial
migration.
*Why:* The embedding dimension depends on `EMBEDDING_MODEL`, which is not chosen
yet, and pgvector requires the dimension to create a vector column with a
dimension. An index is a Phase 1/retrieval-phase decision once the model is fixed.
*Shortcut:* no ANN index and no dimension means brute-force cosine only; add the
dimension and an HNSW index in the retrieval phase, and backfill existing chunks.

---

**D-7 — `requirement` table stores the unified record, not the §7 summary row.**
*Decision:* The `requirement` table carries the full PRD §5.3.1 unified record
(`source_span`, `extraction_method`, `value_type`, `mandatory`,
`extraction_confidence`, `review_required`, `parent_requirement_id`,
`source_table_id`, `source_row_id`, `extraction_version`, `schema_version`) plus a
duplicate `text` column for the PRD §7 summary shape.
*Why:* Both pipelines (table and clause) emit one schema (FR-T8) and it has to
survive the round trip to reporting, so the richer record is what gets stored. The
`text` column keeps the PRD §7 field list satisfied.
*Alternatives:* Storing the §7 summary and expanding later — rejected, it would
mean a second migration and a data backfill in the phase that first needs the
fields.

---

**D-8 — `tender.category` is nullable; category lives on `tender_item`.**
*Decision:* `tender.category` is a coarse nullable hint. `tender_item.category` is
the category a requirement is actually matched within.
*Why:* FR-16 — "category is inferred per BOQ item, not once per tender".
*Alternatives:* Making `tender.category` NOT NULL would force a single category onto
a tender that may span several.

---

**D-9 — `result.evidence_ref` is a FK to `fact.id`.**
*Decision:* A real foreign key, nullable (a result with no evidence — "no evidence
found" — has nothing to point at).
*Why:* PRD §13 traceability: every result links to a stored evidence record.
*Alternatives:* A free-text reference — rejected, it would not be enforceable and
the citation would be unverifiable.

---

**D-10 — `tender.sha256` is UNIQUE.**
*Decision:* One row per tender file hash.
*Why:* FR-12 makes ingestion idempotent: the same hash plus the same
extractor/prompt/schema versions reuse stored results, so the tender row is keyed by
content. Page count and processing version are stored on the same row and are what
the version check compares against.
*Alternatives:* A separate `tender_file` table keyed by (hash, version) — deferred
until a second extractor version actually exists; the column set is already there
to support it.

---

**D-11 — Health check reports `degraded` instead of failing hard.**
*Decision:* `GET /api/health` returns 200 when the database answers, 503 with
`status: degraded` when it does not, and the app still starts without a database.
*Why:* PRD §13 — "graceful degradation, never a silent drop". A health check that
crashes tells you less than one that names the broken dependency.
*Alternatives:* Raising on startup when the DB is unreachable — rejected, it would
block the container from starting and hide the reason.

---

**D-12 — No seed data, no example specs, no sample quotes.**
*Decision:* The database starts empty. No fixture row, no invented spec sheet, no
made-up page number anywhere in the repo.
*Why:* The user asked for it explicitly, and the assignment grades "evidence
quality — citations exist, are accurate, and nothing is made up". Invented
fixtures would undercut the thing the system is being judged on.
*Alternatives:* Seed scripts with realistic-looking specs — rejected outright.

---

**D-13 — Scoring policy lives in config, not in code.**
*Decision:* `scoring_*` settings in `backend/app/core/config.py` with the PRD §8
values as defaults, surfaced through a frozen `ScoringPolicy` object so a report
can snapshot the exact policy it was scored under (FR-34).
*Why:* PRD §13 — "all thresholds, weights, models and paths come from env/config,
not code", and §5.7 FR-34 requires the report to record the scoring policy version.
*Shortcut:* the policy has no version string yet; add one when the first report is
generated so FR-34 can record it.

---

**D-14 — `python-multipart` dropped from the runtime requirements.**
*Decision:* Not listed in `backend/requirements.txt`.
*Why:* Phase 0 has no upload endpoint. FastAPI needs it only when a route accepts
multipart form data, which arrives with `POST /api/tenders`.
*Shortcut:* add `python-multipart` in the phase that implements the upload route.

---

**D-15 — The comparison policy is a frozen config object, not constants in the evaluators.**
*Decision:* Every threshold and score band the evaluators use lives in
`ComparisonPolicy` (`backend/app/core/config.py`), populated from `comparison_*`
settings and read through `default_policy()` at comparison time.
*Why:* PRD §13 forbids literals in business logic. The thresholds that decide
Full vs Partial vs Missing are the ones a deployment most needs to tune, and a
report must be able to record the policy it was assessed under (FR-34).
*Alternatives:* Module-level constants beside each evaluator — rejected, they
would be invisible to configuration and would drift apart.

---

**D-16 — Numeric partial-match tolerance is a *comparison* rule, not a scoring rule.**
*Decision:* `numeric_partial_tolerance` lives on `ComparisonPolicy`
(`COMPARISON_NUMERIC_PARTIAL_TOLERANCE`), not on `ScoringPolicy`.
*Why:* It decides whether a shortfall is a capability failure or a partial match
— a comparison question. `ScoringPolicy` holds the PRD §8 score values and the
mandatory/optional weights, which are a different concern.
*Alternatives:* Keeping it on `ScoringPolicy` — rejected, it mixed the evaluator
rules into the score constants and left the env var name misleading.

---

**D-17 — Unit conversion is restricted to an explicit physical dimension.**
*Decision:* `app/comparison/units.py` wraps `pint` but refuses conversions
between quantities that merely share a mathematical dimensionality: `Hz`≠`rpm`,
`kW`≠`kVA`, `psi`≠`psig`. An unconvertible pair is surfaced for review, never
guessed.
*Why:* Pint is permissive in ways that are wrong for engineering specifications.
A 50 Hz motor is not a 477 rpm pump; converting them would manufacture a false
Full match.
*Alternatives:* Plain `pint` — rejected, it would silently equate quantities
that are not the same thing.

---

**D-18 — The attribute catalogue is a JSON data file, not Python literals.**
*Decision:* `backend/data/attribute_vocabulary.json` holds the canonical
attributes, their synonyms, value types and default units, plus the material
grade groups. `app/comparison/vocabulary.py` is the loader; the path is
configurable via `ATTRIBUTE_VOCABULARY_PATH`.
*Why:* PRD FR-6 and FR-27 — adding an attribute should be a data edit, not a
code change, and a deployment may need its own vocabulary.
*Alternatives:* A module-level `ATTRIBUTES` tuple in Python — rejected, it
repeats the "hardcoded catalogue" problem the comparison policy just fixed.

---

**D-19 — Evidence conflicts are resolved by document class, and a tie is a conflict.**
*Decision:* `app/services/evidence.py` classifies documents
(specification / datasheet / compliance schedule / technical offer / ignored),
consolidates equivalent facts, and resolves a disagreement only when one class
is strictly higher in `EVIDENCE_PRIORITY`. A disagreement inside the highest
class is recorded as a conflict and goes to review.
*Why:* PRD §10 and FR-25 — conflicts must be surfaced, not silently resolved.
A priority order that breaks ties inside one class would hide a real
disagreement.
*Alternatives:* Picking the newest document — rejected, recency does not
establish authority, and PRD §10 only permits it when the newer document
applies to the same model and market.

---

**D-20 — The evaluator vocabulary is the PRD's, not a borrowed one.**
*Decision:* The comparison engine returns the PRD §4 statuses (`full_match`,
`partial_match`, `missing_capability`, `unsupported_requirement`,
`no_evidence_found`). Evidence consolidation uses its own dispositions
(`selected`, `equivalent-duplicate`, `conflict`, `lower-priority`), and
uncertainty is recorded in `verification_outcome` / `applicability` /
`review_required` rather than in a status.
*Why:* AGENTS.md requires the PRD statuses. `status`, `verification_outcome` and
`applicability` are three separate facts and must not be collapsed into one.
*Alternatives:* A single "review" status — rejected, it would force uncertainty
into the business result, which PRD §4 forbids.

---

**D-21 — Design ideas were adapted from BidLint; no code was copied.**
*Decision:* Seven ideas from [BidLint](https://github.com/yigitcan-ozturk/bidlint)
(`yigitcan-ozturk/bidlint`) informed the Phase 1 design: policy-driven tolerance
(D-15), dimension-explicit unit conversion (D-17), document-class evidence
priority (D-19), conflict → review rather than a silent winner (D-19), an
evidence audit with dispositions (D-19), a data-driven terminology vocabulary
(D-18), and a frozen status vocabulary kept separate from the score denominator
(D-20). The comparison engine, evaluators, unit layer, vocabulary and evidence
consolidation are written against this PRD.
*Why:* The user asked for ideas worth adapting from BidLint, with reasons, and
for the credit to be recorded. The ideas are the reusable *shape*; the
vocabulary and the rules are the PRD's own.
*Alternatives:* Adopting BidLint's status vocabulary (PASS / DEVIATION /
MISSING / REVIEW) — rejected, AGENTS.md requires the PRD §4 statuses.

---

**D-22 — A number inside an identifier is not a value.**
*Decision:* The normalizer's number patterns refuse a digit that is preceded by
a letter or a digit, so `SS316` and `13Cr` are grades and not the numbers 316
and 13. A prefixed nominal size ("DN50", "NPS 2") is read separately, because
there the number *is* the value.
*Why:* A requirement that reads a grade as a number produces a confident wrong
answer: "SS316 with pressure rating 16 bar" became `operating_pressure = 316
bar` and was then compared against a real pressure capability. Wrong answers
that look right are worse than unanswered questions.
*Alternatives:* Stripping non-numeric characters before matching — rejected, it
would still leave "13Cr" reading as 13 and would lose the position of the real
value in the sentence.

---

**D-23 — Every unit in a dimension group carries that group's label.**
*Decision:* `DIMENSION_OVERRIDES` labels every member of each
`_DIMENSION_UNITS` group, not just some of them.
*Why:* A unit with no override fell through to pint's raw dimensionality
string, so `is_compatible("kW", "W")` was `False` even though the conversion
is exact. The PRD's own `power` attribute defaults to `kW`, so a requirement
in kW against a datasheet value in W was rejected as "not convertible" and
sent to review for no reason.
*Alternatives:* Comparing pint dimensionality directly for the override groups
— rejected, it would lose the whole point of the overrides, which exist
precisely because pint's dimensionality is too permissive.

---

**D-24 — Every split rule is tried; the list is never short-circuited.**
*Decision:* `_split_spans` runs the whole list of split patterns against every
span, and does not stop at the first pattern that matched nothing.
*Why:* A pattern that matched nothing on one span says nothing about the next
pattern. Short-circuiting made every rule after the first miss dead code — the
semicolon rule only ever ran when a lettered or numbered sub-clause happened
to come first in the same sentence. A semicolon-joined clause is exactly the
compound line FR-13 exists to split, and unsplit content cannot be scored
separately: a partial failure inside it is invisible.
*Alternatives:* Keeping the break and reordering the patterns — rejected, it
leaves the same trap for the next rule that is added.

---

**D-25 — Dispositions are decided before any audit entry is written.**
*Decision:* `consolidate` computes the disposition of every fact first, then
writes exactly one audit entry per fact.
*Why:* The audit exists so a reviewer can ask why a fact was used and what
happened to the others. Writing a losing group as `selected` and then
rewriting it as `lower-priority` left one fact with two entries, one of them
claiming it had won. The outcome was right; the record of how it was reached
was not.
*Alternatives:* Rewriting the earlier entry in place — rejected, deciding
everything first is the same amount of code and cannot produce a fact with two
dispositions.


---

**D-26 — The value shape decides the attribute, not the attribute word.**
*Decision:* `find_attribute` treats an attribute word as the first candidate,
not the last word on the matter. When the matched attribute cannot extract a
value from the text and another attribute's value shape can, the value shape
wins. "Seal shall be Viton" therefore resolves to `material_grade`, not
`seal_arrangement`.
*Why:* An attribute word says what the clause mentions; the value says what
it is about. "Seal shall be Viton" mentions a seal and states a material, and
routing it to `seal_arrangement` made the requirement unusable — a real
coverage gap, though not a mis-scored one, since an unusable requirement goes
to review rather than to a verdict.
*Alternatives:* Reordering the lookup to try the value patterns first — the
obvious fix, and it was tried. It is wrong: "Material of body shall be SS316
with pressure rating 16 bar" contains both a grade and a pressure, and taking
the first value shape present would return the grade and lose the pressure,
undoing D-22. A special case for "mechanical seal" was also considered and
rejected — the material pattern never matches "mechanical seal" in the first
place, so it needed no special case at all.

---

**D-27 — An identifier is not a prefix of another identifier.**
*Decision:* The categorical evaluator's substring fallback no longer applies
across a numeric boundary. A token that ends in a number is an identifier,
and one identifier is not a prefix of another: DN50 against DN500 is a
capability failure, not a full match. The substring rule still applies to
free text, where "swing check" genuinely includes "swing".
*Why:* `dn50` is a literal substring of `dn500`, so a required DN50 was a
*full match* against a DN500 capability, with a score of 1.0, on a dimension
that is about to be ordered. It would propagate straight into the
compatibility percentage as a good result. The comparison was also
asymmetric: a required DN500 against an offered DN50 came out Partial, so
offering a *larger* valve graded higher than offering a smaller one.
*Alternatives:* Comparing the numeric part of size tokens instead of the
whole token — rejected, it needed a separate parse for each identifier
family, and the boundary rule already distinguishes the cases that matter
without naming any of them.
