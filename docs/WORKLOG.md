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

- 118 unit tests pass, 1 integration test skipped without `TEST_DATABASE_URL`.
  With `TEST_DATABASE_URL` pointed at a disposable database the migration
  integration test runs too: 119 pass.
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
  a prefixed nominal size ("DN50") is read as the value it is.
- **Units inside an override dimension did not convert.** `DIMENSION_OVERRIDES`
  labelled `kW` as `power` while `W` fell through to pint's raw
  dimensionality string, so `is_compatible("kW", "W")` was `False` even
  though the conversion is exact — and the PRD's own `power` attribute
  defaults to `kW`. Every member of a dimension group now carries that
  group's label. The deliberate separations (`kW` vs `kVA`, `Hz` vs `rpm`,
  `psi` vs `psig`) are unchanged and are covered by tests.

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
