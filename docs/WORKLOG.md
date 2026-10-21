# Worklog

What each phase built, what was tested, what is left open. Newest phase last.
A different agent should be able to pick this up cold: read `AGENTS.md`, then
`docs/PRD.md`, then this file.

---

## Phase 0 — Scaffold

**Built**

- Repo layout per PRD 6.2: `backend/app/{api,core,schemas,repositories,services,comparison}`,
  `backend/alembic/`, `backend/tests/`, `docs/`.
- Dependency manifests: `backend/requirements.txt` (runtime) and
  `backend/requirements-dev.txt` (adds pytest, pytest-cov, httpx).
- `.env.example` — every knob the app reads, with the PRD 8 scoring defaults.
- `Makefile` with `install-dev`, `migrate`, `run`, `test`, `test-unit`,
  `test-integration`, `docker-up`.
- `docker-compose.yml`: `pgvector/pgvector:pg16` + the backend image.
- `backend/Dockerfile`: non-root user, migrations on start, healthcheck on
  `/api/health`.
- Alembic migration `0001_initial` for all 15 tables, with `CREATE EXTENSION
  vector` first.
- `backend/app/repositories/schema.py` — the single `MetaData` the migration
  imports, so app and database cannot drift.
- Pydantic schemas and enums: `Requirement` (the PRD 5.3.1 unified record),
  `Result`, `TenderPage`, `TenderItem`, `HealthResponse`, `RunState`, plus the
  full enum vocabulary.
- `GET /api/health` — 200 with the database up, 503 `degraded` with it down, no
  secrets in the body.
- Structured error contract (`AppError` + `code`/`message`).
- Repository protocols `DocumentRepo`, `FactRepo`, `RunRepo` (no CRUD yet).
- Tests: config, schemas, health, migrations.
- `AGENTS.md`, `CLAUDE.md`.

**Tested**

- 13 unit tests pass, 1 integration test skipped without a database.
- The migration test is `integration`-marked and skips unless
  `TEST_DATABASE_URL` is set.

**Left open**

- No CRUD in the repositories; it lands in the phase that needs it.
- `chunk.embedding` has no dimension and no ANN index (D-6) — the retrieval
  phase fixes both.
- `ScoringPolicy` has no version string; the first report adds one (D-13).
- `python-multipart` is not a dependency yet; the upload route adds it (D-14).
- `docs/PRD.md` was renamed from `PRD Tender Requirement Matching System (1).md`
  so the four references to it resolve.
- `make` is not installed on this machine — run the commands from the `Makefile`
  directly, or install make.

---

## Phase 1 — The deterministic core (atomize, normalize, verify, compare, score)

Phase 1 builds the part of the pipeline that decides. It has no LLM, no
retrieval and no HTTP surface: it turns requirement text into a structured
requirement, turns documented capability into a structured fact, and compares
the two deterministically.

**Built**

- `app/services/atomizer.py` (FR-13) — splits compound clauses into atomic
  requirements, each keeping its source span. A section heading
  (`3.2 Materials:`) becomes the `parent_clause` of what follows, not a
  requirement of its own. A table row is never split: the table pipeline
  already emits one row per parameter (FR-T6) and re-splitting it would break
  the row provenance.
- `app/services/normalizer.py` (FR-15) — parses requirement text into
  `{attribute, operator, value, unit, range, mandatory}`. Direction words
  become operators (`min 1500 RPM` → `gte`, `max 1200 RPM` → `lte`), and a
  range stays a range rather than collapsing to one number. Anything it cannot
  parse comes out with `operator == unknown`, which routes it to review
  instead of to a guess (FR-29).
- `app/services/verifier.py` (FR-23, FR-24) — the four layers: citation
  validity (the quote exists verbatim on the cited page, whitespace-normalized),
  entity validity (the passage identifies the model or family under test),
  semantic validity (the passage supports the claimed attribute and value), and
  comparison validity (units, operator and conditions are consistent). Any
  failure yields a failed outcome with the reason; a failed verification is
  never published as a confirmed match.
- `app/services/evidence.py` (FR-25) — document classification, consolidation
  of equivalent facts, and conflict detection. A conflict is resolved only by
  a strictly higher document class; a disagreement inside the highest class
  stays a conflict and goes to review.
- `app/services/scoring.py` (PRD §8) — per-requirement scores, the weighted
  compatibility %, the coverage %, the status counts and the mandatory-failure
  flag, plus the ranking sort. `no_evidence_found` counts towards the coverage
  denominator and not towards the score; `unsupported_requirement` is excluded
  from both and listed separately.
- `app/comparison/registry.py` (FR-27, FR-28) — the evaluator registry:
  numeric (direction-aware), range, categorical, boolean, certification and
  material. Each returns `{status, score, reason}`; an unknown attribute
  produces a review, never a guess (FR-29). Compound requirements are scored
  by sub-requirement (FR-30).
- `app/comparison/units.py` — `pint` for the conversion, restricted to
  conversions within an explicit physical dimension (D-17).
- `app/comparison/attributes.py` + `app/comparison/vocabulary.py` +
  `backend/data/attribute_vocabulary.json` — the attribute catalogue as a data
  file with an env-configurable path (D-18).
- `app/core/config.py` — `ComparisonPolicy` and the `comparison_*` settings
  that feed it (D-15, D-16).

**Tested**

- 134 unit tests pass, 1 integration test skipped without `TEST_DATABASE_URL`.
  With `TEST_DATABASE_URL` pointed at a disposable database the migration
  integration test runs too: 135 pass.
- Unit coverage: normalizer operators/units/ranges, atomizer splitting, the
  four verifier layers, every evaluator, scoring and coverage, the unit
  dimension rules, the vocabulary loader (including a custom vocabulary that
  both adds and removes attributes), and evidence consolidation.
- Two configuration tests prove the comparison rules are policy and not code:
  changing `COMPARISON_NUMERIC_PARTIAL_TOLERANCE` or the score band changes
  the verdict, with no code edit.

**Defects found and fixed after the phase was written**

- **The normalizer read a grade as a number.** `SS316` contains the digits
  `316`, so "Material of body shall be SS316 with pressure rating 16 bar"
  parsed as `operating_pressure = 316 bar` and took the unit from the wrong
  word — a confident wrong answer on a requirement that was never about
  pressure. The number patterns now refuse a digit preceded by a letter, and
  a prefixed nominal size ("DN50") is read as the value it is. (D-22)
- **Units inside an override dimension did not convert.** `DIMENSION_OVERRIDES`
  labelled `kW` as `power` while `W` fell through to pint's raw
  dimensionality string, so `is_compatible("kW", "W")` was `False` even
  though the conversion is exact — and the PRD's own `power` attribute
  defaults to `kW`. Every member of a dimension group now carries that
  group's label. The deliberate separations (`kW` vs `kVA`, `Hz` vs `rpm`,
  `psi` vs `psig`) are unchanged and are covered by tests. (D-23)
- **The atomizer's semicolon rule was dead code.** The split list stopped at
  the first pattern that matched nothing, so every rule after the first miss
  never ran: "Valve shall be SS316; trim 13Cr; seal Viton" stayed one atom
  unless a lettered or numbered sub-clause happened to come first. The list
  is now always run to completion. (D-24)
- **A fact could get two audit entries, one claiming it was selected.** The
  losing group was written as `selected` and then rewritten as
  `lower-priority`, so a reviewer saw one fact listed twice, once claiming
  to have won. The outcome was right; the record of it was not. Dispositions
  are now decided before any entry is written. (D-25)

**Left open**

- Retrieval (FR-17–FR-21) and the LLM extractor (FR-22) do not exist yet. The
  verifier, comparison and scoring layers are written against their inputs but
  nothing produces them yet.
- The collector (`collect/`, FR-1–FR-7) and the manufacturer corpus do not
  exist yet, so the evidence layer has no real documents to consolidate.
- The PRD §11 endpoints beyond `GET /api/health` do not exist: no tender
  upload, no runs, no report, no manufacturers. The `RunRepo`/`FactRepo`/
  `DocumentRepo` protocols still have no CRUD.
- No report generator, so FR-31–FR-34 (JSON/HTML/CSV, and the config
  snapshot) are unbuilt; `ScoringPolicy` still has no version string for
  FR-34 to record.
- The React frontend, the `eval/` harness and the gold labels do not exist.
- `chunk.embedding` still has no dimension and no ANN index (D-6).
- **A clause can only carry one attribute, so a compound requirement is
  still only partly read.** "Material of body shall be SS316 with pressure
  rating 16 bar" names two attributes — a grade and a pressure — and the
  normalizer returns one, the pressure. The grade is not lost (the verifier
  still sees the whole text) but it is not compared. FR-13 wants the atomizer
  to split compound lines and FR-30 wants compound requirements scored by
  sub-requirement; the atomizer splits on commas, semicolons and lettered
  sub-clauses, but not on the "with" that joins a material to its pressure
  rating. Splitting there needs a rule that knows a "with" clause can carry
  its own attribute, and that is a design decision for the retrieval phase
  rather than a bug fix.
- **The attribute lookup could not tell a seal material from a seal
  arrangement.** "Seal shall be Viton" resolved to `seal_arrangement`
  (categorical) rather than `material_grade`, because the lookup tried the
  attribute synonyms before the value patterns and `seal` is a synonym of
  `seal_arrangement`. The requirement was not mis-scored — it came out
  unusable and routed to review, which is the correct failure mode — but it
  was a coverage gap. The vocabulary data was already correct (`viton` is a
  material group and the material pattern matches it), so the fix was in the
  lookup, not the data. The scope was `seal` alone: `gasket`, `packing` and
  `wetted parts` already resolved to `material_grade`. (D-26)
- **A larger size satisfied a smaller size requirement.** The categorical
  evaluator fell back on a substring test, and `dn50` is a substring of
  `dn500`, so a required DN50 was a *full match* against a DN500 capability —
  a score of 1.0 on a dimension that is about to be ordered. The same applied
  to `PN16`/`PN160` and to `IP67`/`IP678`. A token that ends in a number is an
  identifier, and one identifier is not a prefix of another; the substring
  rule still applies to free text, where "swing check" genuinely includes
  "swing". (D-27)

---

## Phase 2 — The collector (FR-1 to FR-7)

Phase 2 collects the manufacturer corpus offline. It has no LLM and no
database: it fetches the documents a source config lists and records each one
in a per-manufacturer manifest, which the ingestion phase turns into
`document` rows.

**Built**

- `collect/collect.py` — the collector. Reads one JSON config per category
  from `collect/sources/`, downloads each listed document, and writes it under
  `backend/data/manufacturers/<slug>/raw/` with a per-manufacturer
  `manifest.json` (FR-1, FR-2, FR-6).
- A source config is one file per category; adding a category is adding a
  file, with no code change (FR-6). `collect/sources/*.example.json` are
  templates and are skipped.
- Robots.txt is respected. A disallowed path stops the fetch and the document
  is recorded as `restricted` with a note; it is never worked around (FR-4).
- Manual documents — a gated catalogue, a JS-only page — are recorded in the
  manifest with a note and never downloaded (FR-3).
- Re-running is idempotent: a hash match is `unchanged`, a different hash is
  `changed` with the previous hash recorded for re-ingestion, and a missing
  file on disk is fetched again (FR-5).
- Distributor and reseller documents are recorded with `is_secondary` (FR-7).
- `backend/data/doc_types.json` — the document-type vocabulary, as data.
- `Makefile`: `make collect`, `make test-collect`; `make test` now runs the
  collector tests too, which it did not before.
- `collect/sources/README.md` — the config format and the entry fields.

**Tested**

- 14 collector tests and 137 backend unit tests pass, 1 integration test
  skipped without `TEST_DATABASE_URL`.
- The collector tests run against a local HTTP server, so no test touches the
  internet. The two that used to reach `example.com` now use the server.
- End-to-end against a local server: `new` → `unchanged` → `changed` across
  three runs, with the previous hash recorded on the change (FR-5).

**Defects found and fixed after the phase was written**

- **The robots.txt rule was never tested.** The test set the server to
  `"forbid"` and the server answered to `"forbidden"`, so the 403 path never
  ran and the test passed against the *allow* path — the assertion failed
  loudly, but a real `Disallow:` rule had never been exercised at all. Both
  are now tested: a 403 on `robots.txt`, and an actual `Disallow: /a/` rule
  that must leave `/b/` fetchable.
- **A document with an unknown `doc_type` was collected anyway.** The run
  printed "skipped" and then collected the document, so a document nothing
  could classify landed in the corpus. It is now actually skipped (D-30).
- **An empty response was recorded as a document.** A `200` with no body was
  hashed and written, putting a zero-byte file in the corpus. It is now a
  failure.
- **The manifest stored the slug as the manufacturer's name.** The
  configured name was never recorded, so the manifest said `"acme-valves"`
  where it should have said "Acme Valves". The name is now recorded; the
  slug stays the stable key.
- **FR-7 was not enforced anywhere.** The collector recorded `is_secondary`
  and the evidence layer never read it, so a distributor's datasheet would
  have won against the manufacturer's own datasheet. The consolidation sort
  key is now `(class rank, secondary flag)` (D-31).

**Left open**

- **The corpus is still empty.** There are no real source configs and no
  manufacturer documents; the collector has only ever run against a local
  test server. The first real source config is the next thing to write, and
  it needs the manufacturer list from PRD §19.1.
- The manifest is not yet read by anything. Turning a manifest into
  `document` rows is the ingestion phase (FR-8 to FR-11), which does not
  exist yet.
- `document.doc_type` and the app's `DocumentClass` are two vocabularies
  with no mapping between them. The mapping lands with the ingestion phase.
- The `document` table has no `notes` column, but the manifest records
  notes. Either the table gains one or the ingestion phase has to fold them
  into `restriction_note`.
- No OCR, no PDF parsing, no chunking: FR-8 to FR-11 are all unbuilt.
- The frontend, the `eval/` harness and the gold labels do not exist.
- `chunk.embedding` still has no dimension and no ANN index (D-6).
- The Phase 1 open item about a clause carrying only one attribute is
  unchanged: "Material of body shall be SS316 with pressure rating 16 bar"
  still yields the pressure and not the grade.
