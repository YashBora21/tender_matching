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

---

## Phase 3 — Document ingestion (FR-8 to FR-11)

Phase 3 turns a collected document into searchable chunks, and it is the phase
where the corpus stops being a folder of PDFs. It has no LLM: everything here
is local parsing.

**Built**

- `app/services/docparser.py` (FR-8) — PDF and DOCX into chunks, each with its
  page, its section heading and whether it is a table. Tables become one
  chunk per row, carrying their column context and a row id (FR-T6). The
  extension-to-parser map is a registry, so a new format is a new entry.
- OCR fallback (FR-9) — a page with no usable text layer is read through
  Tesseract, tagged `ocr=true` and given a lower confidence. A page that
  cannot be read is reported as failed, never as processed.
- Chunk metadata (FR-10) — page, section, table flag, OCR flag, confidence.
  The document-level metadata (manufacturer, model/family, category) lives on
  the `document` row the chunks belong to.
- `app/services/ingestion.py` — reads the collector's manifest *and* a
  catalogue's CSV manifest, and writes `document` and `chunk` rows.
- `app/repositories/postgres.py` — the corpus writes. A document whose hash is
  already stored is left alone; one whose content changed is re-ingested and
  its chunks are replaced.
- Migration `0002_document_ingestion` — `chunk.confidence`, `document.model_or_family`
  and `document.category`, a generated `tsvector` on `chunk.text` with a GIN
  index, and a unique constraint on (manufacturer, sha256).
- FR-11's keyword half: the chunk text is searchable through the generated
  `tsvector`. The semantic half waits for the embedding model (D-6).

**Type-driven evaluation (the audit's finding, and the PRD's own rule)**

The attribute list was a fixed valve-and-pump list living in a JSON file that
the code read by name. It is now:

- `app/repositories/aliases.py` — the `alias` table is live. It was dead
  schema: nothing read or wrote it. It now resolves an attribute name to a
  canonical term, and every row carries a confidence and an approval flag.
- `app/services/resolution.py` — resolves an extracted `(attribute, operator,
  value, unit)` into a typed requirement. The value type of the canonical
  term selects the evaluator, so the comparison depends on the *shape of the
  value* and not on the product category. An attribute that does not resolve
  goes to review (FR-29).
- The alias table is seeded from the vocabulary file, so it starts minimal and
  a reviewer can change a row without a code edit (FR-20).

**Hardcoded constants moved out of the code**

- The material grades, the DN/NPS size prefixes, the requirement wording and
  the boolean words now live in `attribute_vocabulary.json`. The atomizer
  had its own copy of the material grades that was *narrower* than the
  vocabulary's, and it was missing 1.4408, 13Cr, the PH grades, ductile iron
  and Hastelloy.
- The document classes, the filename markers and the file extensions moved
  into `doc_types.json`. There were three document-type vocabularies that
  did not agree, and a collected document could have no class at all.
- The code no longer names a canonical attribute. It asks the vocabulary
  which attribute a value shape identifies, so renaming one in the file does
  not break the code.
- The ingestion policy (OCR language and resolution, the chunk length, the
  header band) moved into config.

**Tested**

- 163 backend unit tests and 14 collector tests pass, 6 integration tests
  skipped without `TEST_DATABASE_URL`.
- The parser is tested against synthetic PDFs and DOCX: pages, sections,
  table rows, the OCR path, the failed-page path, the running-header
  removal, the short-line joining and the empty-cell collapsing.
- The ingestion is tested with a fake repository, and against a real
  database with a rollback session: the insert, the re-ingest, the
  manufacturer, and the keyword index.
- The resolution is tested: a canonical attribute, an alias, an unknown
  attribute, a low-confidence alias, an unapproved alias, and the evaluator
  selection.

**Against the real corpus**

The 59 collected catalogues (Delval Flow, Sulzer, Valmatic, Wilo, Bharat
Bijlee) parse to 726 chunks and 386 table rows with **zero failed pages**:
the five image-only documents are read through OCR. Two catalogues had a
running header on every page; it is now dropped from every page but the
first, and the content under it is no longer hidden.

**Defects found and fixed after the phase was written**

- **pdfplumber spent 26 seconds a page on pages with no table.** It was
  asked about every page and found nothing. PyMuPDF's finder now gates the
  pages (D-32).
- **A designed page's text layer became 263 chunks.** A brochure page is a
  scatter of short fragments, and chunking on every newline produced one
  chunk per fragment. Short lines are now joined.
- **A running header was on every page of every bulletin.** It is dropped
  from every page but the first, and it is found by position rather than by
  comparing the text (D-33).
- **An empty table cell became a column of separators.** A merged-cell table
  gave nine columns where three held text. A run of empty cells is now one
  separator.
- **`insert(chunk)` with a list of values is not valid SQLAlchemy.** The
  corpus write failed on the first live run. The integration test catches it
  now.
- **The manifest listed 25 of the 59 files.** The other 34 were silently
  skipped. A document on disk that no manifest lists is now hashed and
  ingested (D-34).

**Left open**

- **The embedding dimension and the ANN index are still unset** (D-6). They
  need the embedding model, which is still PRD §19.2.
- **FR-11's semantic half is unbuilt.** The keyword half works.
- **The alias table is seeded but nothing proposes aliases yet.** The LLM
  proposal path (FR-20) lands with the extractor.
- **The `attribute` table is still empty.** The canonical terms live in the
  vocabulary file; populating the table is a step the fact layer will need.
- **`product` rows are not created.** A document names a model or family, but
  the products the facts hang off are not built.
- **The tenders in `backend/data/tenders/` are not ingested.** They are
  tender documents, not corpus documents: they go through the tender
  pipeline (FR-12 to FR-T16), which does not exist yet.
- **One page of one catalogue failed to read.** The ingester reports it as
  an incomplete document rather than counting it as processed.
- The frontend, the `eval/` harness and the gold labels do not exist.
- The Phase 1 open item about a clause carrying only one attribute is
  unchanged: "Material of body shall be SS316 with pressure rating 16 bar"
  still yields the pressure and not the grade. The LLM extractor is what
  fixes that, because it extracts the two attributes as two facts.

---

## Phase 4 triage — the tenders, measured before building

The PRD says to run a triage script over the tenders *before* building the
tender pipeline, and to choose the tools from the evidence. This is what the
evidence says.

**The corpus of tenders**

24 GeM tender PDFs, 266 pages. Two kinds: 15 named `GEM_<id>_<Category>.pdf`
(pump, valve, motor) and 9 named by their content hash.

| Measure | Result |
| --- | --- |
| Pages | 266 |
| Pages with a text layer | 264 (99.2%) |
| Pages needing OCR | 2 (0.8%) |
| Pages containing tables | 158 (59%) |

**What that changes**

- **OCR is nearly irrelevant here.** 2 pages out of 266. The selective-OCR
  path exists and works (Phase 3 proved it on the catalogues, where 5 of 59
  documents were image-only), but it is not what this phase is about. The
  catalogue corpus and the tender corpus have opposite shapes: the
  catalogues are designed pages with almost no text layer, the tenders are
  text documents that are mostly tables.
- **The table pipeline is the main event.** 59% of pages hold a table, and
  the requirements live in them: "Face to face distance 267mm", "Working
  pressure 8 bar", "Cast carbon steel". FR-T6 is the path that matters.
- **The tenders are bilingual.** Every heading is Hindi and English on the
  same line, and the Devanagari text is sometimes mangled by the PDF's
  encoding ("बड ववरण/Bid Details"). A classifier reading these lines has to
  cope with that, and the English half is the one to read.
- **The PDF is a wrapper, not the tender.** The specification and the BOQ are
  *links* inside the wrapper ("Specification Document — View File", "BOQ
  Detail Document — View File"), pointing at `mkp.gem.gov.in`. The wrapper
  PDF has **zero embedded files**. So the requirements are not in the file
  that was downloaded; they are one fetch away.

**The attachments**

- `specification_for_gem_...pdf` — the specification, 2 pages, a table of
  "Sr. No. | Item Descriptions | Qty | Unit | Rate/Unit | Amount" where the
  Item Description carries the requirement: "Flanged Gate valve 6" Class 150
  Cast carbon steel Face to face distance 267mm".
- `boq_item_sample_file_...csv` — the BOQ, with the columns `Item Number,
  Item Title, Item Description, Item Quantity, Unit of Measure, Consignee ID,
  Delivery Period`. **This is the `item_id` spine**: one row per BOQ item,
  and "GATE VALVE SIZE 150 MM CLASS 150" is the item a requirement belongs
  to.

Both are reachable (HTTP 200) and both are what the phase must work on.

**What this means for the phase**

The tender pipeline is three things, in this order:

1. **Fetch the attachments** a tender PDF links to, and record the links as
   part of the tender. Without the specification, there is nothing to
   extract: the wrapper PDF is a cover sheet.
2. **Read the BOQ** into `tender_item` rows, one per item, with a stable
   `item_id`. FR-T9: a requirement is only ever compared against models in
   its own item's category.
3. **Extract the requirements** from the specification table, per item, and
   carry `item_id` through.

The PRD's own cut order says to keep "triage, the table/prose split, item
identity, clause classification and the recall check" and to cut
cross-reference resolution and parallel batching first. That is the scope.
## Phase 4 — Tender ingestion (FR-12, FR-T1 to FR-T14)

Phase 4 turns a tender into requirements. It starts with a survey, because
the PRD says to measure the tenders before building for them, and the
measurement changed the design.

**The survey**

24 GeM tender PDFs, 266 pages, 99.2% with a text layer, 59% holding a table.
Every tender is a **wrapper**: it links to its bill of quantities and its
specification and embeds nothing. The attachments are reachable, and they
are what the phase reads.

A link's file name says nothing. The same URL shape carries the BOQ in one
tender, a checklist in another and a declaration in a third, so a document
can only be identified by its content, and the buyers write the BOQ header
four different ways.

10 of the 24 tenders are **image-only** - no text on any page. OCR reads
them: a scanned pump specification gives "The total range of pump speed
shall be given by a RPM gauge ranging from 0-2000 RPM" in 0.7 seconds a
page. Without OCR the phase would have found nothing in them.

**Built**

- `app/core/fetching.py` — the fetch fence: an allowlist of hosts, a
  private-address check, no redirects, a size limit and a timeout (D-38).
- `app/services/tendercollect.py` — finds the links, fetches what is
  allowed, classifies each document by its content, and freezes it with its
  URL, its hash and the date it was taken. A refused or failed fetch is
  recorded as failed, with the reason (D-39, D-40).
- `app/services/tenderingest.py` — the bilingual lines, the items, and the
  requirement records. The item key is the composite `(tender_id, item_id)`
  (FR-T9).
- `app/services/llm.py` — the client: an OpenAI-compatible API, strict JSON,
  and the reasoning model handled (D-41, D-42).
- `app/api/tenders.py` — `GET /api/tenders/{id}/attachments` lists every
  attachment with its name, URL and hash, and names the ones that are
  missing; `POST` uploads the file a failed fetch could not get.
- `requirements.txt` — `python-multipart` (D-14, the upload), PyMuPDF,
  pdfplumber and python-docx (FR-8).

**Tested**

- 204 backend tests and 14 collector tests pass, 6 integration tests skipped
  without `TEST_DATABASE_URL`.
- The fetch fence is tested against a local server: a host off the
  allowlist, a private address, a loopback address, a `file://` URL, a
  redirect, a document over the size limit, and a server that lies about
  its size.
- The collection is tested: the links found, a refused fetch, a restricted
  path, a frozen document with its hash, and the manifest.
- The upload is tested: the listing, the missing attachments, an upload
  that replaces the record it stands in for, and a file of the wrong kind.
- The bilingual handling is tested: the English half, a line with no
  English, and a mangled line.
- The items are tested against all four BOQ header spellings.

**The LLM and the embedding model**

- The LLM is `openai/gpt-oss-20b` on Groq. `LLM_API_KEY` is a Groq key and
  Groq serves gpt-oss under exactly the `openai/` prefix the configured
  model name uses. It answers in about 1.4 seconds.
- gpt-oss is a **reasoning** model: the answer comes in `message.content`
  and the reasoning in `message.reasoning`, and a 32-token budget is spent
  entirely on reasoning and returns an empty answer. The client asks for
  room and keeps the reasoning apart.
- The embedding model is the local Ollama's `nomic-embed-text` (768
  dimensions), which is free and needs no account. The HuggingFace token
  now has the inference permission, but the account has **no credits** (HTTP
  402), so `BAAI/bge-small-en-v1.5` is a configuration change away, not a
  code change.

**Left open**

- **The extraction prompt and the requirement assembly are not written.** The
  LLM client, the span validation and the deterministic fallback are ready;
  what the model is asked, and how its answer becomes requirements, is not.
- **The tender is not written to the database.** The `tender`,
  `tender_item`, `requirement` and `tender_page` rows, and the page ledger
  (FR-T2), are not populated yet.
- **There is no frontend.** The API exposes the attachments and the upload;
  the screen that lists them and prompts for an upload does not exist, and
  the whole React app is unbuilt.
- **The report does not list the attachments.** The manifest holds every
  attachment with its name, URL and hash, and the API returns them, but the
  report generator is a later phase.
- **Only 24 tenders' wrappers have been surveyed.** The attachments have
  not been fetched into the corpus yet.
- The item identity is read from the BOQ; where the BOQ and the
  specification disagree about which item a requirement belongs to, the
  requirement is not yet flagged as ambiguous (FR-T9).

### The vertical slice — one tender, end to end

The milestone was a narrow slice: one tender, from its attachments to its
requirements, with the checks that make the result worth having.

**GEM_9671792_Valve** — a GeM valve tender with both a specification PDF and
a bill of quantities CSV.

| | |
| --- | --- |
| Attachments fetched | 2 (the specification PDF and the BOQ CSV), plus 2 more |
| Attachments failed | 0 |
| Items | 11, keyed `GEM_9671792_Valve:1` … `:11` |
| Requirements | 11, each carrying its item |
| Failed quotes | 0 |
| Bad pages | 0 |
| Unassigned | 0 |
| Requirements the LLM extracted | 31, from the 11 descriptions |

The LLM turns one bill-of-quantities description into the atomic
requirements inside it, each with its attribute, operator, value and unit:

    [technical] size                  eq  6    inch  :: Size 6 inch
    [technical] class                 eq  150  None  :: Class 150
    [technical] body material         eq  cast carbon steel None
    [technical] face to face distance eq  267  mm    :: Face to face distance 267 mm

**The checks**

- **The quote check.** A requirement has to be found in the text of the page
  it names. 0 of 11 failed. One requirement the LLM produced was *rejected*
  because its span did not hold what the model said it held — the model
  rewrote the text instead of quoting it, and the code caught it.
- **The page check.** A requirement has to name a page the document has. 0
  of 11 failed.
- **The type check.** Every requirement is classified; only `technical` and
  `certification` go to matching, and the commercial and eligibility ones
  are listed separately (FR-14).
- **The recall check.** The lines the specification stated that produced no
  requirement are listed, so a missed requirement is not invisible (FR-T14).

**The tests** cover the four outcomes: a tender that works, a tender whose
attachments are missing, a tender with a malformed file, and a requirement
that was never extracted.

**Three defects the slice found**

- **A malformed attachment stopped the run.** A PDF that is not a PDF raised
  out of the reader. One broken file is a file that cannot be read, and the
  tender is still read from the other attachment.
- **A classification change never took effect.** The frozen copy was the
  cache, and the cached record kept the kind it was first given, so a better
  classifier could not be applied without fetching everything again. The
  document is now classified from the file on disk every time: the download
  is the expensive part, the classification is local.
- **The rate limit is per model, not per organisation.** `gpt-oss-120b` was
  exhausted for the day while `gpt-oss-20b` answered normally. The default
  is now 20b, which is also the cheaper and faster of the two.

**The rest of the corpus.** All 24 tenders' attachments are fetched and
frozen — 82 attachments, 211.8 MB, 0 failed. Six of the tenders yield items
and 46 requirements with no failed quote and no bad page. The rest are
image-only documents that need the OCR path, and two produce specification
rows that name no item in their bill of quantities (233 and 9 rows), which
is the ambiguity FR-T9 says to flag rather than guess at.

---

## Phase 5 — A reusable, incremental tender pipeline

Phase 5 makes the tender pipeline reusable: it takes a directory of tenders
and treats every one of them the same way, it does the same work twice only
when something changed, and it reports what it did.

**The stages**

    attachments -> items -> requirements -> extraction -> resolution

Each stage is keyed by a hash of everything that can change its output,
including the key of the stage before it. A stage whose key has not changed
is not run, and neither are the stages after it. The checkpoints live in
`extraction_batch`, which the PRD gives for exactly this.

**The invalidation chain**, and what each change moves:

| Changed | Re-read | Re-extract | Re-download |
| --- | --- | --- | --- |
| An attachment's hash | items, requirements | yes | no |
| The prompt, model or schema version | no | yes | no |
| The attribute vocabulary | no | no | no |
| The fetch configuration | attachments | yes | yes |

**The LLM cache** is keyed by the hash of the input plus the model, the
prompt version and the schema version, and every stored requirement records
all four. A rate-limited run is recorded as `rate_limited`, which is
resumable: the next run picks it up where it stopped.

**The database** gained what the PRD's schema did not have: the attachments
of a tender (`tender_attachment`), the LLM cache (`llm_cache`), the model,
prompt version and input hash on a requirement, and the attachment a page
came from. An item is identified by its tender and its number within it,
because two tenders both have an item "1" - so `tender_item`'s key is the
pair, and a requirement points at the pair.

**No tender is named in the code.** The pipeline takes a directory, and a
test reads the corpus and checks the application code against it: no tender
id, no file name, no attachment URL appears in `app/`.

**Tested**

231 backend tests and 14 collector tests pass. The pipeline tests cover a
tender that works, a changed attachment, a changed prompt, a changed model, a
changed vocabulary, a cached rerun, a malformed attachment, an ambiguous
item mapping, a line that cannot be read, and a tender with no bill of
quantities.

**Defects the audit found**

- **A scanned page was read as a readable one.** A scanner writes whatever
  the page had on it - a file number, a date - so a scanned technical
  specification carried 214 characters per page and was classified as a
  readable specification, read as if it had a table, and produced no
  requirements at all. A page is now scanned when its images cover it, not
  when its text is short (D-47).
- **A bill of quantities that was scanned was never read.** The OCR path
  existed for the corpus and not for the tender, so a scanned bill of
  quantities gave no items. It is read now, and the item number is taken
  from the line that names it whether the scanner put the number on its own
  line or with the words.
- **One tender's failure broke every tender after it.** The error arrived
  when the work was written, after the savepoint had closed, and the whole
  transaction was then unusable. Each tender now runs in a transaction of
  its own (D-49).
- **The two stages disagreed about which file was the bill of
  quantities.** Each stage looked for what it needed, so a scanned document
  was the bill of quantities in one stage and nothing in the other, and the
  tender was reported as having no items when it had them. The roles are
  resolved once now (D-48).
- **A manifest written before the wrapper was recorded could not be
  resolved.** The hash of the wrapper is now taken from the wrapper file
  beside the manifest, so an older manifest still identifies its tender.

**The corpus audit**

The audit reports, per tender, whether its specification and its bill of
quantities were found, how many items and requirements it produced, and why
it failed if it did - and then ranks the failure causes, so the thing that
goes wrong most often is the thing that is looked at first.

---

## Phase 6 — Retrieval, extraction and matching (FR-11, FR-17 to FR-28)

Phase 6 answers a requirement from the corpus. It is the phase where the
pieces from Phase 1 - the verifier, the comparison registry, the scoring -
are finally given something to work on.

**Built**

- `app/services/retrieval.py` (FR-17 to FR-20) — the keyword search, the
  semantic search, and the merge that combines them. The exact tokens a
  requirement carries are boosted, and the words are ORed rather than ANDed.
- `app/services/facts.py` (FR-22) — the LLM reads a passage and states
  what it documents, with the sentence it says it in. The answer is strict
  JSON, validated by Pydantic; an answer that is not JSON is a failed
  extraction, never a capability that was never stated.
- `app/services/matching.py` (FR-21 to FR-28) — retrieve, extract, verify
  through the four layers, compare deterministically. The LLM never decides
  whether a capability matches. `make match REQ="..."` answers one
  requirement from the corpus.
- `app/services/corpusseed.py` (FR-17, FR-20, FR-21) — the catalogue
  seeding: a collected document becomes a product (the model it names, or a
  family-level product when it names none), the canonical attributes are
  written, and the alias table is seeded from the vocabulary. A document
  points at the product it is about (`0006_document_product`), so a passage
  is read for that product and not once per product of the manufacturer.
  `make ingest` runs the seeding after it writes the documents (D-56).
- `app/services/findings.py` (FR-21) — the structured fact cache. A
  verified fact is filed under its canonical attribute, and
  `match_requirement` reads it before it retrieves anything; retrieval and
  extraction do not run for a fact already held.
- `app/services/embeddings.py` (FR-11) — the corpus is embedded once, in
  batches, and a chunk that already has an embedding is left alone.
- Migration `0004_chunk_embedding` — the embedding column gets its
  dimension and an HNSW index (D-6).
- Migration `0005_product_family_level` — a product records whether it is
  family-level evidence, which caps the score and flags review (PRD 4).
- Migration `0006_document_product` — the document-to-product link the
  retrieval joins on.

**Three defects the corpus found**

- **The keyword search found nothing.** The words were ANDed, and a
  requirement names several things at once, so no datasheet sentence
  matched. They are ORed now, and the common words are left out: a chunk
  that says "the" matches every requirement.
- **A datasheet's value never matched.** A requirement says "267mm" and the
  datasheet writes "267" in the cell with the unit in the column header, so
  "267 mm" appears nowhere. A token is looked for in three forms now.
- **The rank put a page footer above the answer.** The full-text rank
  cannot tell "stainless" from "SS316", because the first is the common
  word and the second is the one that identifies the product. The exact
  tokens are boosted over the rank.

**The rate limit**

The provider's limit is 8,000 tokens a minute and it says when the limit
resets. A fixed five second backoff retried into it, and every retry was
another call against the same exhausted limit. The retry now waits as long
as the provider says (D-53).

**The embedding model**

The local Ollama's `nomic-embed-text` (768 dimensions) is served without an
account. The HuggingFace router needs a token *and* credits: the account
answers 402, and the anonymous path answers with a web page rather than
JSON. The corpus is embedded with the local model, and the dimension in the
migration matches it (D-52).

**The corpus, answered from**

Five manufacturers were added to the corpus - CIRCOR, Emerson Fisher,
Flowserve, Xylem Godwin and Xylem Goulds - and their catalogues were read,
chunked and embedded. The passage the model reads is often a table row,
and the prompt now says so: a row states the values in its columns, and
one passage can state more than one capability. The answer is a list
(`{"requirements": [...]}`) and the comparison runs on the one that
answers the requirement.

`embed_corpus` had no caller, so the corpus could not be embedded at all:
`make embed` runs it now, and reports how many chunks were embedded, how
many already had one, and how many failed.

**A defect the big catalogues found**

- **The ingestion died on the last document.** The corpus grew to 156MB of
  PDFs, and a 52MB catalogue takes minutes to read. The ingestion held a
  transaction open across the read, and the server killed the connection:
  `IdleInTransactionSessionTimeout`, after half an hour of parsing that
  would have been thrown away. The transaction is now closed before the
  read and every document is committed on its own, so one document that
  fails does not take the corpus with it (D-54).

**The OCR that was not running**

Two of the new catalogues have a page with no text layer, and the page
came back unreadable: `No tessdata specified and Tesseract is not
installed`. Tesseract was installed, at `C:\Program Files\Tesseract-OCR`,
but `TESSDATA_PREFIX` was unset, and PyMuPDF's OCR looks for the language
data in the directory the variable names - the `tessdata` directory
itself, not the one above it. The setting was already in the config; it
had no value.

**Three defects found while closing the phase**

- **A document had no product.** `seed_corpus` existed but nothing called
  it: `make ingest` wrote the documents and stopped. Every passage then
  named no product, and the fact cache - keyed on a product - could never
  hit. The ingestion seeds the products and removes the stale ones now
  (D-56).
- **A fact read in a synonym was unsupported.** The LLM answers in the
  document's own words ("body material"), and `_value_type` looked the
  wording up only as a canonical term, so no evaluator was found and a
  correctly read fact came back `unsupported_requirement`. The term is
  resolved through the vocabulary's synonyms before the evaluator is
  chosen (FR-27, FR-29).
- **A public host was refused on a NAT64 network.** The private-address
  fence treated the well-known NAT64 prefix as reserved, and two tests
  failed on a machine whose resolver returns one. The IPv4 the address
  carries is checked now, so a public GeM host passes and a NAT64 address
  wrapping a private one does not (D-55).

**Tested**

264 backend tests pass and 14 collector tests pass; 9 integration tests are
skipped without a database. No test fails. The fact tests were moved to the
list shape the model now answers with (`{"requirements": [...]}`, read as
`result.facts[...]`); no stale `.fact` accessor is left, and the matching
tests use the same shape end to end.

The retrieval tests cover the tokens that identify a product, the merge of
the two searches, and the exact-token boost. The fact tests cover a fact
read from a passage, a passage that states nothing, an answer that is not
JSON, and an answer with a field the schema does not have. The matching
tests cover a requirement nothing answers, a quote that is not on the page
it names, a failed extraction, the fact taken from a multi-fact answer, a
family-level document capped and flagged, and a cache hit that retrieves
nothing. The ingestion test covers the transaction and the seeding order.
The fetching tests cover a public resolve, a NAT64 address carrying a
public IPv4, and a NAT64 address carrying a private one; they no longer
touch the network.

**Left open**

- **FR-16 (category inferred per BOQ item) is still unbuilt.** `category`
  reaches retrieval from the document row; nothing infers it from the BOQ
  item a requirement belongs to.
- **The retrieval SQL has not run against PostgreSQL in this session.**
  The integration tests are skipped without `TEST_DATABASE_URL`, so the
  `tsvector` search, the pgvector ordering and the `document.product_id`
  join are proven by unit tests only.
- **The corpus is not in git.** `backend/data/manufacturers/` is 157 MB of
  PDFs behind the ignore rule; only the manifests and the four new source
  configs are left to commit.

**Fixed after the merge**

- **`test_tendercollect` wrote fixed file names into `backend/tests/`.**
  `_boq.pdf`, `_form.pdf` and `_scanned.pdf` were real files in the source
  tree, so two pytest processes at once collided on them and a test failed
  for a reason unrelated to the code. They are written to `tmp_path` now.
- **One ingestion test named an absolute path.** The doc-type check read
  `C:/Users/Lenovo/.../backend/data/catalogue/manifest.csv`: on another
  machine the check silently skipped itself, and a check that skips is a
  check that is not there. It reads every `manifest.csv` under the
  configured data directory, the same place the ingestion looks.
- **A relative `DATA_DIR` depended on the working directory.** `.env` says
  `DATA_DIR=backend/data`; read as a plain relative path it resolved
  against the current directory, so the suite run from `backend/` looked in
  `backend/backend/data` and three tests that inspect the corpus skipped
  themselves instead of running. A relative data directory is resolved
  against the repository root now (D-57).

With those three fixed, 269 backend tests pass and 6 skip (all six need a
live database); the collector's 14 pass. Three of the six that used to skip
were the corpus checks that could not find the corpus, and the two new
config tests cover the absolute and relative forms of `DATA_DIR`.


## Phase 7 — Runs, reporting and the API (FR-16, FR-31 to FR-34, PRD 11)

Phase 7 turns the matching service into something a caller can start, poll and
read: a run is one tender assessed against a chosen set of manufacturers, and
its answer exists in three renderings of one stored report. It also closes the
Phase 6 entry "FR-16 is still unbuilt": a requirement is now compared in the
category of the BOQ item it belongs to.

**Built**

- `app/services/categories.py` and `backend/data/categories.json` (FR-16,
  FR-6) — the category is inferred per item from the item's own title and
  description. The most specific term wins, a trailing plural is the same
  term, and an item naming nothing — or two categories equally specifically —
  is `unknown` with a reason, never guessed (D-58). The vocabulary is a data
  file: adding a category is editing JSON.
- `TenderItem` carries `category`, `category_term`, `category_ambiguous` and
  `category_reason`, and `pipeline.assign_categories` fills them in the items
  stage. The category vocabulary is part of that stage's key, so correcting
  the vocabulary re-reads the items and nothing before them.
- The corpus's document `category` is the product category (`valves`,
  `pumps`), not the manufacturer's slug (D-59): the collector's source
  configs and the manufacturer manifests now declare the same vocabulary a
  tender item is classified in.
- `retrieval._category_filter` (D-60) — a search excludes only documents that
  declare a *different known* category. A document with no category, or one
  from an older manifest, is kept, so a metadata gap cannot empty the corpus
  and read as *no evidence found*.
- `PostgresDocumentRepo.refresh_metadata` (D-61) — an unchanged document is
  not re-parsed, but its category, doc type, path, URL and model are brought
  up to date from the manifest; the ingestion reports the count as
  `metadata_refreshed`.
- `app/repositories/runs.py` — `RunRepo` creates a run before any work
  starts, moves its state, and reads its results joined back through the fact
  to the document, page and quote.
- `app/repositories/corpus.py` — the manufacturers with their coverage, the
  corpus hash a run was scored against (FR-34), and a name-to-id lookup.
- `app/services/runs.py` — `create_run` validates the tender and the
  manufacturer names (an unknown name is a 422 listing the known ones, not an
  empty result), deduplicates them and records the config snapshot;
  `execute_run` answers every requirement for every manufacturer. A clause
  whose type is not technical or certification is recorded as *unsupported*
  and never sent to the corpus (FR-14); an item with no category is searched
  unfiltered and the result is flagged. Each result is committed as it is
  found, and a failure is written to `run.error` with the run left `failed`
  (D-62). `summarise` reuses the Phase 1 scoring, so a summary and the rows it
  summarises cannot disagree.
- `TenderRepo.list_requirements` now carries `id`, `value`, `unit` and
  `mandatory`; the mandatory weight and the "fails mandatory" rule need them.
- Migration `0007_run_error` — the run says why it stopped.
- `app/services/report.py` (FR-31 to FR-34) — `build_report` assembles the
  canonical JSON from stored rows: the run, the tender and every page in its
  ledger, the config snapshot, the manufacturer summaries, one row per
  requirement × manufacturer with its status, score, confidence, flags and
  evidence, the unsupported clauses listed separately, and the review queue.
  An incomplete ingestion (a failed page, or a ledger short of the page count)
  is labelled, not hidden. `render_html` is the bid-manager view with the
  statuses explained; `render_csv` is one flat row per assessment. Both
  escape document text: a quote is data, never markup or a formula (PRD 10).
  `make report RUN=<id> FORMAT=json|html|csv` builds the same report without
  the server.
- `app/api/runs.py` (PRD 11) — `POST /api/runs` creates the run, answers 202
  with its id and schedules it, so the request that starts a run is not the
  request that waits for it; `GET /api/runs/{id}` reports state and progress;
  `/report`, `/report.html` and `/export.csv` serve the three renderings. A
  report of a run that has not stopped is 409 with the progress in the detail
  (D-63). A malformed id is a 404, not a 500. The PRD's "options" are not
  defined anywhere, so the request accepts none rather than ignoring keys.
- `app/api/manufacturers.py` (PRD 11) — the manufacturers with their document,
  product and chunk counts and the categories their documents cover, plus the
  corpus totals and hash.
- `app/api/deps.py` — the request session as a dependency, so a test can
  replace it and no route opens a connection by hand.
- `ConflictError` in `app/core/errors.py` — 409 for "not ready yet", distinct
  from a validation error and from a missing resource.

**Tested**

332 backend tests pass and 6 skip (all six need `TEST_DATABASE_URL`); the
collector's 14 pass. No test fails.

The new tests are: `test_categories.py` (the shipped vocabulary, an item
classified by its own words, the most specific term, two categories named
equally, a spare part left unknown, a vocabulary read from a data file, a
missing and a broken file, the items stage keyed on the vocabulary);
`test_retrieval.py` (a document in another category excluded, a document with
none kept, an unrecognised category not treated as a product category);
`test_runs.py` (a technical requirement matched and a commercial one not, an
unknown category searched unfiltered and flagged, a requirement with no item
flagged rather than guessed at, an unknown clause type
flagged, a raising matcher recorded as failed with its reason, a finished run
not re-run, creation refusals, deduplication, the summary's ranking and the
mandatory rule, coverage against evidence); `test_report.py` (the evidence in
a row, unsupported clauses listed separately, a failed page and a short ledger
labelled incomplete, the run's own config, partial results, the review queue,
the CSV rows, a formula-shaped cell defused, markup in a quote escaped, every
status explained); `test_runs_api.py` (202 and a background task, manufacturer
selection, an empty list refused, malformed and missing ids, progress, a
report of a live run refused with 409, the three renderings, the manufacturers
route, an empty corpus). `test_ingestion.py` gained the metadata-refresh test.

**Left open**

- **`POST /api/tenders` and `GET /api/tenders/{id}/requirements` (PRD 11) are
  not built.** Uploading a tender and running it through the pipeline is a
  job of its own — collection, parsing and the stages — and a run today needs
  a tender that `make tenders` has already ingested. The two routes are the
  next API work, and the frontend cannot offer "upload a tender" until they
  exist.
- **The run SQL has not run against PostgreSQL in this session.** `RunRepo`,
  `corpus.py`, `refresh_metadata`, the new `run.error` column and the category
  filter are proven by unit tests and by compiling the filter, not by
  executing against a database. The integration tests are skipped without
  `TEST_DATABASE_URL`, exactly as in Phase 6.
- **The background queue is in-process.** `BackgroundTasks` runs the match in
  the API process; a restart loses runs that were queued and they stay
  `queued`. A durable queue, or a startup sweep that fails runs older than a
  configured age, is the fix; neither is built.
- **`document.category` keeps its old values until the next ingestion.** The
  manifests are corrected (D-59) but an existing database still holds the
  manufacturer slugs until `make ingest` refreshes the metadata (D-61).
- **No live LLM or database was exercised.** Matching, runs and reports were
  tested with fake matchers, stores and repositories; the end-to-end run over
  the real corpus and a real model is not measured here.
- **The corpus is still not in git.** `backend/data/manufacturers/` is
  behind the ignore rule; only the manifests and the source configs changed.


## Phase 8 — Tender upload and the requirements API (PRD 11, FR-12)

Phase 8 closes the two API gaps Phase 7 named: a caller can hand the system a
tender PDF and then read what was extracted from it. It does not change how a
tender is read — the same collection, parsing and pipeline stages run — only
who can start that read and how its state is observed.

**Built**

- `app/services/tenderupload.py` — `store_upload` validates the name and the
  bytes, hashes the file and freezes it under `DATA_DIR/tenders/<tender_id>/`
  with the manifest shape `make tenders` produces, so a run and a report cannot
  tell an uploaded tender from a collected one. `ingest` is the background
  read: `processing` on entry, then collection and `audit_tender`, then `done`,
  or `failed` with the reason in `tender.error`. A tender that is its own
  specification or bill of quantities becomes its own attachment instead of
  being reported as an empty wrapper; a document uploaded by hand survives a
  re-read (D-64).
- `app/schemas/enums.py` — `TenderStatus` (`queued`, `processing`, `done`,
  `failed`), separate from a run's states because a tender is read when it is
  uploaded, not when a run asks for it.
- `TenderRepo.get_tender` carries `status` and `error`; `set_status` moves the
  state; `upsert_tender` applies `status` only when it creates the row and
  leaves absent fields alone on an existing one, so re-offering a file neither
  erases what was read nor claims a field the caller did not state.
- Migration `0008_tender_status` — the two columns; existing rows are
  backfilled to `done`, since every tender in a database predates the column
  and was ingested by the corpus runner.
- `app/api/tenders.py` — `POST /api/tenders` (202; `.pdf` name, `%PDF-`
  content, non-empty, `fetch_max_bytes`; duplicate by hash; a background read
  for a new tender or one whose earlier read failed) and
  `GET /api/tenders/{id}/requirements` (each requirement with its item's name
  and category, counts, and the tender's state; 409 while queued or processing,
  404 for an unknown or malformed id).

**Tested**

369 backend tests pass and 6 skip (all six need `TEST_DATABASE_URL`); the
collector's 14 pass. No test fails.

The new tests are: `test_tenderupload.py` (the name reduced to its final
component, non-PDF and empty bytes refused, the file frozen with its hash
before it is read, a duplicate not duplicated, a manual attachment carried
across a re-read, a self-contained PDF used as its own attachment,
processing→done, a failed read with its reason, an exception recorded instead
of left `processing`, a vanished file reported with what to do);
`test_tender_upload_api.py` (202 and a scheduled read, a path in the name not
trusted, non-PDF / wrong extension / empty / oversized refused before anything
is stored, a duplicate `done` not rescheduled, a duplicate `failed`
rescheduled, a duplicate `processing` left alone, the requirements payload with
`item_id` and item title, 409 while unread, a failed tender carrying its error,
unknown and malformed ids as 404). The OpenAPI schema lists `POST
/api/tenders` and both tender read routes.

**Left open**

- **No live PostgreSQL or LLM was exercised.** The status columns, migration
  `0008`, `set_status` and `upsert_tender` are proven by unit tests and
  compilation only; the integration tests skip without `TEST_DATABASE_URL`.
- **The background read is in-process**, like the run queue: a restart loses
  tenders left `queued` or `processing`, and there is no sweep that fails rows
  older than a configured age.
- **The frontend (PRD 12) is not built.** The two endpoints it needs now exist;
  the upload, progress and report screens are Phase 9.
- **The eval harness (FR-35/FR-36), Docker Compose and the README are not
  built.**
- **The corpus is still not in git**; `backend/data/manufacturers/` is ignored,
  only manifests and source configs are tracked.


## Phase 9 — The React frontend (PRD 12)

Phase 9 puts the pipeline in front of a person: upload a tender, watch it being
read, run the match, then read the report and its evidence. It decides nothing
— every status, score and count on screen is a value the API stored (D-65).

**Built**

- `frontend/` — Vite 8 + React 19 + TypeScript 6, `oxlint`, Vitest 5 with
  jsdom and `@testing-library/react`. Two views in `App.tsx` (`upload`, `run`)
  and no router: the run id is the only state that survives a screen change.
- `src/api.ts` — a typed client over the five endpoints the UI needs
  (`POST /api/tenders`, `GET /api/tenders/{id}/requirements`,
  `GET /api/manufacturers`, `POST /api/runs`, `GET /api/runs/{id}`,
  `GET /api/runs/{id}/report`) plus the HTML/CSV export URLs. `VITE_API_URL`
  points at the backend; a 409 is passed through untouched so the tender poll
  can read it as "not read yet" instead of an error.
- `src/components/UploadPage.tsx` — drag-and-drop or click for a PDF, upload,
  poll the tender once a second, show the file, pages, item and requirement
  counts and the flag count, then the corpus as checkboxes and **Run the
  match**. A failed read shows the reason and offers another upload instead of
  a run; a `done` tender with no requirements says so and also offers no run.
- `src/components/RunPage.tsx` — polls the run until `done` or `failed`, shows
  the state, `results / expected_results` and the percentage, surfaces
  `run.error`, then hands over to the report.
- `src/components/ReportPage.tsx` — ranking cards (rank, compatibility,
  coverage, fails-mandatory, review count), status chips that filter the table
  and explain themselves from `status_meanings`, filters by status,
  manufacturer and needs-review, a partial-results warning and an
  incomplete-ingestion warning listing unaccounted and unresolved pages,
  HTML/CSV export links, and the run's config snapshot under "What produced
  this report".
- `src/components/AssessmentTable.tsx` — one row per requirement ×
  manufacturer, unsupported clauses included and labelled "Excluded from
  score"; `src/components/EvidenceDrawer.tsx` — quote, document, page, type,
  hash, the source link, and the score / verification / applicability /
  confidence / review fields; `src/components/ReviewQueue.tsx` — the API's
  `review_queue` with its reason, and a jump back to the filtered table.
- `src/report.ts` — the pure part: status labels, counts, the requirement ×
  manufacturer flattening, the three filters. `src/fixtures.ts` holds the
  synthetic report the tests share.
- `RunRepo.results` now selects `document.url` and returns it as
  `evidence.url`; `test_report.py` asserts it. A display field only — nothing
  is decided by it.
- `frontend/README.md` and `frontend/.env.example`.

**Tested**

21 frontend tests pass in three files and `npm run lint` and `npm run build`
are clean. `report.test.ts` covers the flattening (unsupported clauses kept),
each filter and the combination, counts, the PRD status order, labels for
unknown words, "no score" staying absent rather than zero, and finding a
reviewed requirement's text. `ReportPage.test.tsx` renders the ranking and both
manufacturers' rows, filters from a status chip and clears them, shows the
review queue and jumps to the row, opens the drawer with its quote, document,
page and source `href`, closes it, and labels a partial report and an
incomplete ingestion. `UploadPage.test.tsx` reads a tender and starts the run
with both manufacturers, disables Run when none is chosen, refuses a non-PDF
before uploading, and shows a failed read with no Run button.

A browser pass against a throwaway mock API (synthetic payloads, no database)
walked upload → manufacturer picker → run → report, the drawer, the review
queue and a status filter, and found two layout defects that are now fixed: a
wide table widened the whole page instead of scrolling in its own box (a
`min-width: 0` panel is not enough — the absolutely positioned screen-reader
labels in the header needed the scroll box to be their containing block), and
the item id and title now stay on their own line. `scrollWidth` equals
`clientWidth` at an 800 px window after the fix.

The backend suite still passes: 369 pass, 6 skip.

**Left open**

- **No live backend was exercised.** No PostgreSQL (`TEST_DATABASE_URL` unset)
  and no LLM, so the new `evidence.url` SQL and the real payloads are covered
  by unit tests and the mock only. The browser pass proves the screens, not the
  integration.
- **Past runs (PRD 12.4) are not built.** It needs a `GET /api/runs` list the
  API does not have; the PRD marks it as the first thing to cut.
- **Polling is per-tab and unauthenticated**, one request a second for the
  tender and for the run; a refresh loses the run id because the view is not in
  the URL. There is no auth in front of the API at all, so the app assumes a
  trusted network.
- **Not covered by tests:** the run-error path, the corpus-error path and the
  `no_evidence_found` drawer text have no render test; only the paths above do.
- **The eval harness (FR-35/FR-36), Docker Compose and the top-level README are
  still not built.**
- **The corpus is still not in git**; `backend/data/manufacturers/` is ignored,
  only manifests and source configs are tracked.


## Phase 10 — Evaluation harness (PRD 15, FR-35/FR-36)

Phase 10 makes a run's quality a number instead of an impression: a stored
report is scored against hand-written labels, offline and reproducibly. What it
cannot verify, it refuses (D-66).

**Built**

- `eval/harness.py` — `score` joins a report JSON with a JSONL gold file on
  `(tender sha256, requirement_id, manufacturer)` and reports status accuracy,
  a confusion matrix, per-status precision/recall/F1, the false-confident rate,
  coverage, review rate, citation correctness and a separate unsupported block.
  `check` validates a gold file on its own. `--corpus` overrides the data
  directory (`DATA_DIR` by default), `--out` writes the JSON summary.
- Citations are checked with `app.services.match.verify.check_citation` against the
  cited page of the real PDF, so "verified" means what it means in the
  pipeline. Documents are found through the corpus manifests, by hash then by
  filename; a document that is not on disk is reported as unavailable and left
  out of the citation denominator rather than counted either way.
- Refusals: a label naming a pair the report does not contain stops the run and
  lists the keys; a prediction with no label is *unlabelled*, never correct; a
  claim label without a document, page and quote is invalid, and a
  `no_evidence_found` or `unsupported_requirement` label needs a note saying
  where the labeller looked or why it is not verifiable.
- `eval/README.md` — the metric definitions, the gold format and what is not
  built; `eval/gold/synthetic.example.jsonl` — the shape, explicitly synthetic.
- `Makefile` — `make eval REPORT=… GOLD=…`, `make eval-check GOLD=…`; `test`
  and `test-unit` now run `pytest eval` alongside the backend and collector.

**Tested**

17 harness tests pass (`eval/tests/test_harness.py`), with the whole suite now
31 non-backend tests and 369 backend tests (6 skip without `TEST_DATABASE_URL`).

The tests pin the arithmetic with hand-computed examples: accuracy and the
confusion matrix on four pairs, per-status precision/recall/F1 including "no
precision, not precision 1" for a status that is never predicted, the
false-confident rate ignoring flagged mistakes and counting unflagged ones,
coverage, the unsupported block staying out of the score, and unlabelled
predictions not moving accuracy. They pin the refusals: a label whose pair is
absent, a report without a tender hash, a duplicate label, a claim without a
quote, an unknown status, a silent label without a note, and a malformed line.
Citation checks are exercised against a generated PDF: quote on the cited page
verifies, a real quote on the wrong page fails, a claim with no quote is
uncited, a document that is not on disk is excluded, and an unlabelled
prediction is never citation-checked. `main()` is covered for `check`, `score`
and `--out`.

`make` is not installed in this environment, so the Makefile targets themselves
were not executed; their commands were (`python eval/harness.py …`), including
a scored synthetic report and a green `check` on the example gold file.

**Left open**

- **No real gold set.** The 50–100 hand-labelled pairs PRD 15 asks for need
  requirements from an actual model run; without a live LLM there is nothing
  honest to label, and writing labels anyway would invent the evidence. The
  harness, format and validator are ready for them, and `eval/README.md` says
  so.
- **Ablations and the manual page audit (PRD 15)** are not built; the first is
  a harness flag over two reports, the second is human work.
- **The eval has no HTML rendering or CI hook**; it writes Markdown and JSON to
  the console and `--out`.
- **Citation correctness depends on the local corpus.** On a machine without
  the PDFs, every claim is reported *source unavailable* and the metric is
  `—`, which is the honest answer but not a measurement.
- **The corpus is still not in git**; `backend/data/manufacturers/` is ignored,
  only manifests and source configs are tracked.


## Phase 11 - Packaging (PRD 14)

Phase 11 puts the app in two images, one command, and a README that says what
the commands do. The API address turned out to be the interesting part (D-67):
Vite bakes it into the bundle, so it is a build argument and the browser-visible
value, not the compose-internal one.

**Built**

- `frontend/Dockerfile` - multi-stage: `node:22-alpine` runs `npm ci` and
  `npm run build` with `ARG VITE_API_URL` (default `http://localhost:8000`),
  then `nginx:1.27-alpine` serves `dist`. Healthcheck on `/healthz`.
- `frontend/nginx.conf` - `try_files $uri $uri/ /index.html` for client-side
  routes, `/healthz`, and a one-year immutable cache on hashed `/assets/`.
- `frontend/.dockerignore`, `backend/.dockerignore` - the frontend never ships
  `node_modules`/`dist`, the backend never ships the 157 MB corpus, the tests or
  the virtualenv.
- `docker-compose.yml` - `frontend` service publishing 3000, `VITE_API_URL` as
  a build arg, waiting for the backend to be healthy; header updated.
- `.env.example` - a `Frontend (PRD 14)` block naming `VITE_API_URL` and the
  rebuild it needs.
- `Makefile` - `docker-build` builds both images; `docker-up` describes the
  three services.
- `README.md` - rewritten: the updated layout, container and local run
  instructions, the two images with the exact `build`/`pull`/`push`/`run`
  commands and a shared-network example, a synthetic sample export (labelled),
  the one-command eval, and an honest current state.
- `frontend/README.md` - the container build line and why the argument is the
  browser's address.

**Tested**

- `docker compose config --quiet` exits 0 and the rendered config publishes
  `8000`, `5432` and `3000`, with `VITE_API_URL: http://localhost:8000`.
- The frontend image builds (74 MB). A container on 8088 served `/` (200,
  `text/html`), `/healthz` (200, `ok`), and `/runs/example` (200 with
  `<div id="root">`, so the SPA fallback works); the hashed JS and CSS were 200,
  the default `http://localhost:8000` was in the bundle, and the container
  healthcheck reported `healthy`.
- `--build-arg VITE_API_URL=http://api.example.test:9000` produced an image with
  that address in the bundle, so the argument is real and not ignored.
- The backend image builds. The full stack started from the repo file plus a
  throwaway port override (host 5432 and 8000 are held by local services): db
  healthy, then backend healthy after `alembic upgrade head`, then frontend
  healthy. Inside the stack `GET /api/health` returned `status: ok` with
  `database: ok` and the expected CORS origins, `/api/manufacturers` returned
  `count: 0` on the fresh database (an empty corpus is reported as empty, not
  invented), and `psql` counted 17 tables.
- Because the override used `!reset` and therefore did not publish ports to the
  host, host access was proven separately with the standalone container on
  8088; port publishing itself is what `docker compose config` shows.
- `make` is not installed in this environment, so the Makefile targets were not
  executed; their commands were.

**Left open**

- **Nothing is pushed to Docker Hub.** The README says so next to the pull
  lines, and there is no CI pipeline to build or publish.
- **The matching pipeline still has not run end to end here**: no live LLM, and
  the 6 integration tests skip without `TEST_DATABASE_URL`. The stack around it
  (migrations, health, empty corpus) was verified against real PostgreSQL.
- **Changing `VITE_API_URL` needs a frontend rebuild**; a runtime env var on the
  nginx container does nothing. This is documented rather than worked around.
- **Images are unversioned** (`latest` only), and the compose file builds rather
  than pulls; version tags are left to whoever publishes.
- **The index is not built on first start.** The image runs migrations, but
  `make ingest` and `make embed` stay explicit steps, because they need the
  corpus and the embedding provider; PRD 14's "builds the index" is therefore
  a documented two-command step, not automatic.
- **The corpus is still not in git**, so a fresh clone starts with an empty
  corpus and an empty manufacturer list, which the app reports honestly.

## Phase 12 - The real end-to-end run (PRD 13, FR-34)

**Status: paused.** The first real tender was read and run end to end, and the
report that came out of it is honest about what it covers. The measured run that
was meant to finish the phase did not finish, so the write-up's numbers are not
final yet.

The tender is `GEM_9671792_Valve.pdf` (`2bc8f917-262d-4ea8-a1c9-b2997ef1f256`)
on the local corpus: 11 items, 21 requirements, of which 16 are technical and 5
are unsupported. Its extraction stage is `rate_limited`, so the report carries
`incomplete_ingestion: true` (D-75) rather than reading as final.

Run `7910e15b` (CIRCOR, `MATCH_MAX_CANDIDATES=3`) is what PRD 13 asked for at
the first limit: it stopped and recorded `rate_limited` with the provider's own
message rather than retrying past it - "Limit 8000, Used 6658, Requested 2967,
try again in 12.1875s". The report it produced holds 3 results, 16 requirements,
5 unsupported and 7 rows in the review queue. No number in it is invented: the
stopped stages say they stopped.

**Built**

- The corpus was collected and embedded locally: 11 manufacturers, 70 documents,
  20,892 chunks, all embedded with `bge-small-en-v1.5` at 384 dimensions (D-68,
  D-72).
- Thirteen defects found by running the real thing instead of fixtures were
  fixed and recorded as D-73 to D-80: a failed verification is *no evidence
  found* (D-73), a stage that stopped is re-read on resume (D-74), a short
  extraction labels the report incomplete (D-75), the stored operator reaches
  the comparison (D-76), the snapshot names the embedding backend (D-77), the
  candidate cap is config and is recorded (D-78), a verified full match ends the
  search (D-79), and the report names a document from the path it has (D-80).
- The app now runs against the local Docker Postgres while Neon re-embeds
  (D-81); the Neon line stays in `.env` with its switch-back condition.
- A blank gold slice exporter (`phase12_slice.py`) writes the report's
  requirement/manufacturer pairs to CSV with the label columns empty, for the
  user to label by hand.

**Tested**

- The report endpoint served a real report over the real corpus: 16 technical
  requirements plus 5 unsupported, `incomplete_ingestion: true`, and one
  manufacturer ranked.
- The full backend suite passes (408 passed, 7 skipped) and the frontend tests
  and build pass, on the code as it stands after Phase 13.

**Left open**

- **The measured complete run is not done.** Run `5a9d4ecc` was started on 28
  Oct, paced to the 7,000 TPM budget with the measured prompt reservation, and
  the machine restarted while it was in `retrieving` with 10 of its results
  stored. It has no report, so the phase's own numbers (calls, tokens, retries,
  wall clock) are still the first run's, which stopped at the limit.
- **The tender row was repaired on 28 Oct.** The killed run left
  `GEM_9671792_Valve` at `processing` while its own extraction stage said
  `rate_limited` - the process died between the last stage write and the status
  write. Its status was set to `rate_limited`, which is what the stage says and
  what keeps the report labelled incomplete; the requirements were not touched.
  A crash inside that window leaves a tender that the run guard (D-82) refuses
  and that a re-upload does not reschedule, which is recorded in Phase 13 as an
  open gap rather than worked around in the data layer.
- **The gold slice is not exported or labelled.** The exporter exists; nothing
  has been labelled, by design - the labels are the user's.
- **Neon is still half-embedded** (3,904 of 20,891 chunks) and predates the
  current code (`tender_page` empty, `document.product_id` null). The re-embed
  is resumable with `python -m app.services.embeddings` under a one-off
  `DATABASE_URL`, then the ingestion needs re-running before `.env` is switched
  back (D-81).
- `tender_page` holds no rows for the local tender, so the page ledger cannot
  yet be shown as a per-page table; the report says ingestion is incomplete from
  the extraction stage instead.

## Phase 13 - Watching a tender being read (PRD 12, D-82)

Reading a tender is minutes of model calls, and until this phase the page could
only show a spinner: the extractor wrote nothing until the last requirement, and
`GET /api/tenders/{id}/requirements` answered 409 for the whole read. This phase
makes the work visible without letting a partial list pass for a complete one.

**Built**

- `pipeline._extract_all` takes an `on_read` callback and calls it with what has
  been read after every requirement. `run_pipeline` writes that set with the same
  `replace_requirements` the final persist uses, commits it, and throttles the
  writes by `STREAM_INTERVAL_SECONDS` (default 1.0, `core/config.py`). Every
  write is a complete snapshot: no half-written requirement, no duplicated rows
  on resume.
- `GET /api/tenders/{id}/requirements` answers with the rows stored now and
  `partial: true` while the status is `queued`/`processing`, instead of a 409.
- `GET /api/tenders/{id}/requirements/stream` is a `text/event-stream`: a
  `snapshot` on connect, a `requirements` event on each change, an `end` event
  carrying the final payload, and a `: keep-alive` comment while nothing moves.
  An unknown tender is a 404 before the stream opens; `X-Accel-Buffering: no`
  stops a proxy from holding the events until the read ends.
- `POST /api/runs` refuses a tender in `queued`, `processing` or `failed` with a
  409, because a run over a partial list would report unread requirements as
  "no evidence found". `rate_limited` is still allowed and labelled (D-75).
- The upload page subscribes with `EventSource` and shows "Requirements read so
  far" with a growing list and count; the existing 1 s poll is the fallback when
  there is no `EventSource`, when a frame cannot be parsed, or when the stream
  breaks mid-read. The connection is closed on unmount and on `end`.

**Tested**

- New backend tests: the progress write and its throttle (fake clock and fake
  session, no database), the partial payload and its `partial` flag, the SSE
  frames from the generator (snapshot, change, end) and the 404 before the
  stream, and the run guard for `processing`/`failed` refused and `rate_limited`
  allowed. Full suite: 408 passed, 7 skipped.
- New frontend tests: a fake `EventSource` drives the list from a snapshot to a
  second requirement and then `end`; a stream error falls back to the route.
  Frontend: 24 passed; `npm run build` (tsc + vite) passes.
- To be verified against the live app after the restart: the stream endpoint on
  a tender with a non-terminal status, and the partial payload on the real DB.

**Left open**

- **A stream is a second reader of the same table**, so the throttle is a
  trade-off: a requirement read inside the interval is only shown with the next
  snapshot. One second is the default because the extraction is model-bound.
- **No browser was driven against the live stream** in this phase; the endpoint
  was exercised through the generator and the component through a fake
  `EventSource`. A real connection through Vite's dev proxy is the check that
  remains.
- **A tender left at `processing` by a crash is stuck.** The status is written
  after the stages, so a process that dies in between leaves a row that says
  `processing` while its extraction stage is terminal. The stream then reports
  `partial: true` forever, a run is refused (D-82), and a re-upload does not
  reschedule it, because only a `failed` or `rate_limited` duplicate is read
  again (D-74). Found on the Phase 12 tender on 28 Oct and repaired by hand;
  the fix is to write the status from the stage states at read time or to
  reschedule a duplicate whose stages are all terminal, and it is not in this
  phase.
- A tender that is `failed` cannot be run, and nothing in the UI offers to
  re-read it yet; re-uploading the same file is the current path (D-74).


## Phase 14 - The Neon corpus re-embed, and the ground truth for the next one (D-83 to D-85)

**Built**

- `embeddings._write_embeddings` writes a whole batch with one statement
  (D-83). `embed_corpus` still encodes in batches of 32 and commits once per
  batch; only the write changed.
- `alembic/env.py` passes `version_table_schema` when the caller sets it, and
  `test_migrations` sets it to the schema it migrates (D-84).
- Nothing in the matching path was touched. No filter was added: the point of
  this phase was to make the next change measurable, not to change it.

**Measured**

- The venv had the CPU wheel of torch (`2.14.1+cpu`), so CUDA was unusable as
  installed. Replaced with `torch==2.14.1+cu130` from the pytorch index; no code
  change, because `embed()` already lets `SentenceTransformer` pick the device.
  On the RTX 3050 (6 GB): 1,011 chunks/s steady state, 32 s first-call warmup,
  160 MB peak VRAM. The CPU path was 15.8 chunks/s.
- Neon re-embed, from 3,904/20,891: **20,891/20,891, 0 failed, 462 batches**,
  14,779 chunks in this run. The first 2,048 chunks ran on the old per-chunk
  write at 2.84 chunks/s (measured: one round trip 300 ms, 32 per batch = 9.55 s);
  after the change the same job ran at 22.5 chunks/s, 7.9 times faster. The
  remaining time is the commit per batch, not the model.
- The local database is unchanged: `python -m app.services.embeddings` reports
  0 embedded, 20,892 already embedded, 0 failed.
- The twelve documents with no text layer (6 DelVal, 6 Valmatic, 212 chunks)
  were re-OCRed outside the pipeline with `hin+eng`: 1,023 Devanagari letters,
  364 Devanagari digits, all noise (D-85). Tesseract 5.4.0 is installed at
  `C:\Program Files\Tesseract-OCR` but not on `PATH`, and it ships `eng` and
  `osd` only; `hin.traineddata` was fetched to a temporary tessdata directory
  for this check.
- Neon's switch-back checklist: 20,891/20,891 chunks embedded, **0/70 documents
  linked to a product** (65 products exist), 0 tender pages, 14 tenders, schema
  `0010_stage_detail`. The local database has 70/70 linked, so the product filter
  is only blocked on Neon.
- `ix_chunk_embedding` is an HNSW index (`vector_cosine_ops`). On CIRCOR's 79
  chunks the indexed scan and the exact scan return the same rows in the same
  order at limits 3, 20 and 50, so the filter/hnsw trap is not visible at this
  corpus size yet.
- The category filter admits **all 70 documents** for every item: the documents
  carry a manufacturer slug in `document.category` (`xylem`, `emerson`,
  `flowserve`, `circor`) or nothing, never a product category (`valves`,
  `pumps`, `motors`), and `_category_filter` keeps a document whose category is
  null or is not one of the *other* product categories. FR-16/FR-17 scoping is
  therefore inert today, and only the manufacturer filter narrows anything.

**Tested**

- `test_a_batch_of_embeddings_is_written_in_one_statement` asserts one `UPDATE`
  for a 32-row batch and an exact element-by-element round trip of the stored
  vector; `test_a_vector_literal_keeps_full_precision` covers the literal.
- `test_migrations` now passes against an already-migrated database, which it
  could not before: alembic read `public.alembic_version` through the test's
  search path, found it at head, and migrated nothing.
- Full suite with `TEST_DATABASE_URL` set (integration included): **417 passed**.
- Live: `/api/health` ok on the restarted server; the app stays on the local
  Docker database (D-81).

**Left open**

- **Neon is re-embedded but not ready to serve**: 0/70 documents are linked to a
  product and `tender_page` is empty, so a run there would still retrieve without
  a product scope and have no page ledger. Switching back needs both, and the
  app stays local until then.
- **The category filter does nothing** (above). Making it work means
  `document.category` holding a product category, or the filter reading the
  product's category instead; that is a decision, not a one-line fix, and it
  comes after the gold labels, because it changes what retrieval can see.
- **Five of the 21 slice pairs are `unsupported_requirement` for a tooling
  reason, not a PRD reason**: `class` resolves to no value type, `cast carbon
  steel` is not a recognised grade, and two comparisons had a value on one side
  only. PRD 4 reserves Unsupported for clauses no document could answer, so these
  are mislabelled; D-73 already sends a failed verification to
  `no_evidence_found`, and the same reasoning applies.
- **The gold labels are the blocker.** The 21 pairs of `GEM_9671792_Valve`
  against CIRCOR are exported and waiting for the reviewer; recall@3 and
  recall@20 cannot be measured until they come back.
- Filters are still not visible in the snapshot or the report. That is the next
  change, and it is cheap: record which filters ran and how many chunks each
  removed, before adding any.
- `backend/data/catalogue.zip` (458 MB) is still untracked and deliberately not
  committed.

## Phase 15 - A comparison that could not run is not a finding about the product (D-86)

**Built**

- `ReasonCode` in `schemas/enums.py`: `attribute_not_mapped`, `value_missing`,
  `grade_not_recognised`, `unit_not_comparable`, `operator_not_comparable`.
- `Evaluation.code` in `comparison/registry.py`, and all twelve "cannot compare"
  returns changed from `unsupported_requirement` to `no_evidence_found` with the
  matching code. `_compare_numeric` keeps returning `unsupported_requirement`
  for an operator it cannot apply (it has no way to import the enum's new
  member without a cycle-free change); the call site maps that one return to
  `operator_not_comparable`.
- `services/matching.py` sets `verification_outcome = not_checked` and prefixes
  `review_reason` with `<code>: <reason>` whenever `Evaluation.code` is set, and
  keeps `reason` in step with `review_reason`. Nothing else in the matching path
  changed, and no filter was touched.
- No migration. The structural distinction is `verification_outcome`, which
  already exists; the code is a prefix on `review_reason`.

**Tested**

- `test_comparison.py`: the two existing assertions that expected
  `unsupported_requirement` now expect `no_evidence_found` plus the code, and
  two tests were added - a material the vocabulary does not know
  (`grade_not_recognised`) and a comparison with no value on one side
  (`value_missing`).
- Full suite with `TEST_DATABASE_URL` set (integration included): **419 passed**.
- `test_report`, `test_runs` and `test_scoring` still assert
  `unsupported_requirement` for commercial/unknown clauses and still pass: those
  are decided by requirement type in `runs._unsupported()`, not by the registry,
  so the PRD's use of the status is untouched.

**Left open**

- **The stored results still read `unsupported_requirement`.** This changes what
  the next run writes, not what the database holds; `GEM_9671792_Valve` has to
  be re-run for the five pairs to change, and the gold slice exported before that
  carries the old statuses.
- **The 21-pair gold slice is waiting for retrieval labels** (`gold_document`,
  `gold_page`), so baseline recall@3 and recall@20 are still unmeasured.
- Filter visibility, the document-kind filter and the product-link filter (which
  replaces `_category_filter`) are unchanged and not started, in that order.
- Neon remains 0/70 product-linked with no tender pages, so the app stays on the
  local database (D-81).





## Phase 16 - Read the page, not the letterhead: extraction stops flooding the report (D-87 to D-92)

**Built**

- **Step 0 - the audit** (`bae8760`): `scripts/extraction_audit.py` reads the
  database only and prints, per tender, the requirement count by type, the
  unknown share, the unknown clauses under 25 characters and the unknown
  clauses that look technical. `docs/extraction_audit_before.txt` is the
  baseline.
- **Step 1 - page triage** (`1e450be`, D-87): `services/pageclassify.py`
  scores every page against four keyword families and tags it `technical`,
  `commercial_legal`, `safety_hse`, `boilerplate` or `mixed`. Clauses on a
  non-technical page are stored as `unknown` with
  `review_reason = "page classified as <tag>"` and are never sent to the model.
- **Step 2 - the reader** (`0ed2ba2`, D-88): `read_specification` returns
  `SpecificationRead(rows, pages, error)`. A table is read as a table only when
  it is a real grid; prose is joined into paragraphs and split on clause
  numbers and sentences; all-caps headings are section context, not
  requirements; OCR runs when the text layer is too thin or pictures cover the
  page. `ExtractionMethod.TEXT` is new (VARCHAR, no migration) and
  `READER_VERSION = "3"`.
- **Step 3 - fragments** (`06b5a0a`, D-89): `drop_fragments` removes pure
  clause numbers, junk runs, too-short lines with no unit or spec word, and
  lines with no value, verb or spec word; it merges a lowercase tail into the
  clause before it only when that clause does not end a sentence. Counts go to
  the requirements stage `detail` JSON.
- **Step 4 - re-typing** (`1565932`, D-90): `retype_technical` runs inside the
  extraction stage and moves a clause from `unknown` to `technical` only when
  it names a specification attribute and is not on a non-technical page. It
  fills the attribute, operator, value and unit from the clause's own words,
  never overrides a type the model gave, and flags a clause whose value it
  cannot read. Guards: a bare number must follow the attribute word, a range
  is not one value, a sign is kept only when it is a sign.
- **Step 5 - the ledger** (`9a6760e`, D-91): the requirements stage writes
  `tender_page` from the reader's `PageRead` list before extraction (tag,
  status, method, attachment; a failed page's reason in `skip_reason`). No
  migration. `TenderStatus.INCOMPLETE` is new (VARCHAR(64), no check
  constraint) for a tender read without failing but with no page, no item or
  no requirement; a run against it is refused. A `SpecificationRead.error`
  fails the requirements stage. The report's declared page count is the
  specification's, with the wrapper's count as a fallback.
- **Step 6 - unknown is not a result** (`D-92`): a run assesses only typed
  requirements, so no `result` row is written for an `unknown` clause and
  `expected` counts assessable clauses only. The report adds `not_assessed`,
  `unknown_requirements` (unknown/total/percent and the split
  non-technical pages / fragments / unparsed), the banner line
  `Requirements typed unknown: N of M (X%)` and an HTML *Not assessed*
  section. Fragments come from the requirements stage `detail`; the reason a
  page was not read comes from `NON_TECHNICAL_REASON`, the same constant the
  pipeline writes.
- **Step 7 - a skipped stage is a no-op** (`D-93`): the after-audit found that
  a run with nothing to do still rebuilt the raw clauses from the file and
  wrote them over the stored requirements, replacing every model-read type
  with `unknown` and minting new `uuid4`s. A skipped requirements stage now
  loads the stored rows (`_stored_records`), and `_persist` writes no
  requirements when both the reading and the extraction were skipped.
  `list_requirements` also returns `source_section`, `extraction_method` and
  `confidence`, so the resume path keeps the reader's fields.

**Tested**

- Step 0: audit run against the local database.
- Step 1: 13 synthetic page-classification tests.
- Step 2: 5 synthetic PDF reader tests and a single-item fallback test.
- Step 3: 12 tests in `test_fragments.py`; BHEL `GEM_9915844` read 494 rows,
  dropped 146, kept 348, merged 0; the valve BOQ read 11 rows and dropped 0.
- Step 4: 16 tests in `test_retyping.py`, one pipeline test and one fragment
  test; the re-typer typed 11 of the 173 stored unknown clauses.
- Step 5: ledger rows and their fields, an unreadable specification, the three
  incomplete states, the run guard and the declared page count.
- Step 6: an unknown clause writes no row and is not in `expected`; progress
  counts only assessable clauses; the report lists it as not assessed and the
  banner's three buckets add up.
- Step 7: a second run over an unchanged tender keeps the stored types and ids
  and makes no requirement write; a prompt change re-extracts the stored
  requirements instead of re-reading the file.
- Full suite with `TEST_DATABASE_URL` set: **483 passed** (475 without the
  database).

**Before / after**

Before (step 0, `docs/extraction_audit_before.txt`):

| tender | reqs | unknown | unknown % | <25 chars | looks technical | pages |
| --- | --- | --- | --- | --- | --- | --- |
| GEM_9671792_Valve | 21 | 0 | 0% | 0 | 0 | 0/17 |
| specification_for_gem_... | 31 | 0 | 0% | 0 | 0 | 0/2 |
| Tender_Matching_Intern_Assignment | 0 | 0 | - | 0 | 0 | 0/2 |
| GeM-Bidding-9713870 | 82 | 82 | 100% | 14 | 14 | 0/7 |
| GEM_9935793_Valve | 20 | 20 | 100% | 7 | 6 | 0/7 |
| GEM_9935572_Pump | 61 | 61 | 100% | 12 | 8 | 0/8 |
| GEM_9848272_Valve | 16 | 10 | 62% | 1 | 4 | 0/15 |
| **total** | **231** | **173** | **75%** | **34** | **32** | **0 logged** |

After (the re-ingest ran on a copy of the database, `tender_audit`, so the live
ids and the gold slice stay valid; `docs/extraction_audit_after.txt`):

| tender | reqs | typed | unknown | unknown % | <25 chars | looks technical | pages |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GEM_9671792_Valve (rate limited) | 35 | 35 | 0 | 0% | 0 | 0 | 2/2 |
| specification_for_gem_... (rate limited) | 35 | 35 | 0 | 0% | 0 | 0 | 2/2 |
| Tender_Matching_Intern_Assignment | 0 | 0 | 0 | - | 0 | 0 | 0/2 |
| GeM-Bidding-9713870 | 336 | 26 | 310 | 92% | 0 | 16 | 22/49 |
| GEM_9935793_Valve (incomplete) | 2 | 1 | 1 | 50% | 0 | 1 | 2/2 |
| GEM_9935572_Pump | 90 | 2 | 88 | 98% | 0 | 5 | 8/8 |
| GEM_9848272_Valve | 230 | 71 | 159 | 69% | 9 | 20 | 22/22 |
| **total** | **728** | **170** | **558** | **77%** | **9** | **42** | **6 of 7 logged** |

What the numbers say, and what they do not:

- **The fragments are gone**: 34 unknown clauses under 25 characters became 9,
  and the reader now reads 728 clauses where the old one read 231. The drop is
  not "fewer clauses": it is 3x more clauses with a quarter of the fragments.
- **Pages are accounted for**: 0 of 7 tenders had a ledger before, 6 of 7 do
  now. The seventh is the PRD PDF, which has no attachments.
- **Typed clauses went from 58 to 170**, and the unknown clauses that still
  look technical fell from 18% of the unknown mass to 7.5% (32 of 173, then
  42 of 558).
- **The unknown share barely moved (75% to 77%)**, and that is the honest
  headline: the remaining unknown mass is no longer fragments or legal prose.
  Of 558 unknown clauses, 369 name no BOQ item and 188 are on non-technical
  pages (one is neither). The reader now reads far more of a multi-item
  tender's specification than the old table reader did, and most of what it
  reads cannot be attached to an item, so it is recorded and not assessed
  (FR-T9).
- **Two tenders are still rate limited**: the provider's limit is 200,000
  tokens *per day*, and the day was spent. `GEM_9671792_Valve` read 2 of its
  pages and the specification PDF read both, but their extractions stopped.
  Their rows above are therefore a partial read, not a final one.
- `GeM-Bidding-9713870` reads 22 of a declared 49 pages, which is not the
  reader: two of its attachments share the path `raw/slafds`, so the collector
  wrote one over the other and the 49-page document is not on disk. The
  ledger is complete for the 22-page document it did read.

**What the after-audit found**

- **A skipped stage was not a no-op** (fixed, D-93). Every pass that had
  nothing to do re-read the file and wrote raw clauses over the model's
  output, replacing every type with `unknown` and giving every requirement a
  new `uuid4`. The first after-audit run measured that bug, not the pipeline.
- **`GEM_9935793_Valve` reads 0 BOQ items**, and that is correct: all seven of
  its attachments are classified `spec`, so it has no bill of quantities. The
  new `incomplete` status says so instead of calling the tender done.
- **A two-column datasheet without ruling lines is read as one 2,066-character
  clause** (`GEM_9935793_Valve`, page 1). The lines are `Label  Value` pairs
  with no terminal punctuation, so the prose joiner merges the whole page, and
  the re-typer then mines "15 bar" out of it as `diameter = 15 bar`. This is a
  reader regression from step 2, not fixed yet.

**Left open**

- The retrieval baseline (recall@3 / recall@20) waits on the gold labels.
- The three filter steps (category visibility, document-kind filter,
  product-link filter) are not started, in that order.
- The label/value split for datasheets with no ruling lines, and the
  re-typer's guard against a clause that is a whole page.
- The collector writes two attachments with the same URL-derived name to one
  path (`raw/slafds`), so one document is lost.
- The two rate-limited tenders need a day with tokens left before their
  extraction can finish.
- `backend/data/catalogue.zip` (458 MB) is still untracked and deliberately not
  committed.

---

## Phase 17 - match-side fixes (started 2026-10-29)

The tender-side filter steps are held. This phase works on the match side:
what the verifier trusts, what the comparison can evaluate, how a retrieval
query is built, and what a chunk is embedded with.

### Step 1 - entity validity and citation normalisation (D-94)

- `services/textnorm.py`: one `normalise_text` / `strip_control` used by the
  reader and the verifier.
- `services/verifier.py`: `check_entity` takes document metadata and the
  expected product, never the passage. `check_citation` normalises the quote
  and the page with `normalise_text`.
- `services/retrieval.py`: a `Candidate` carries `model_or_family` and
  `document_title`; both searches select them.
- `services/matching.py`: passes the document metadata and the run's
  manufacturer/model into `verify`.

*Measured:* 480 unit tests pass, 8 skip without a database. The gold-slice
CIRCOR row P01 ("the passage does not mention CIRCOR") is the failure this
step removes; it cannot be re-measured until the database is running.

**Open:** the Docker daemon is down, so the database-backed suite and any
re-measurement wait on it.

### Step 2 - pressure class, material aliases, no-evaluator review (D-95)

- `data/attribute_vocabulary.json`: `pressure_class` attribute and value
  shape; `pressure_class_pattern`; the carbon-steel alias group.
- `comparison/vocabulary.py`, `comparison/attributes.py`: load and expose the
  new pattern.
- `services/normalizer.py`: read `Class 150` / `PN16` as a pressure class.
- `comparison/registry.py`: `evaluate_ordered_categorical`, the
  attribute-specific evaluator map, and `ComparisonRequest.attribute`.
- `services/matching.py`: resolve the model's attribute wording to the
  canonical term and pass it to the comparison.

*Measured:* 489 unit tests pass, 8 skip without a database. The gold-slice
P02 and P03 cases are covered by tests; re-measuring them needs the database.

### Step 3 - retrieval query from the item, and atomizer context (D-96)

- `services/matching.py`: `build_query`, and `item_title` /
  `requirement_attribute` on `match_requirement`; `search` gets the composed
  query.
- `services/runs.py`: `_assess` passes the item's name and the requirement's
  stored attribute.
- `services/atomizer.py`: a `context` on every atom and `retrieval_text`;
  sentence, paragraph, "and"-obligation and comma-obligation splits; a piece
  with two clause markers is compound.

*Measured:* 496 unit tests pass, 8 skip without a database.

### Step 4 - chunk enrichment and the valve-type filter (D-97)

- `services/embeddings.py`: `embedding_text` and `_product_label`;
  `embed_corpus` joins the document, manufacturer and product and embeds the
  enriched text.
- `data/categories.json`: `types` under `valves` (gate, globe, check, ball,
  butterfly, control, needle, plug, diaphragm, pinch, safety/relief).
- `services/categories.py`: `ProductType`, `category_types`, `infer_type`,
  `mentioned_types`.
- `services/retrieval.py`: `_type_filter` and `product_type` on `search`.
- `services/matching.py`: infers the type from the item title and passes it.

*Measured:* 510 unit tests pass, 8 skip without a database. The corpus has not
been re-embedded: Docker is down, and the old vectors stay until
`embeddings --force` runs.

### Step 5 - retrieval recall@k with attributed misses (D-98)

- `eval/recall.py`: `RecallLabel`/`Ranked`/`RecallReport`, the four cause
  buckets, `DatabaseRanking` (the product's `build_query`, `infer_type` and
  `search`), a text and JSON report, and a CLI that refuses an unlabelled
  file.
- `eval/tests/test_recall.py`: 13 synthetic tests - refusals, recall@3/@20
  arithmetic, each cause, stem matching.
- `eval/README.md`: "Retrieval recall@k" section.
- `Makefile`: `recall` target.

*Measured:* 510 backend unit tests pass (8 skip without a database); eval 30
pass; collect 14 pass. The recall figure itself is **not measured yet**: the
21-pair gold slice has no `gold_document`/`gold_page` values, and the harness
refuses it (`error: ... none of the 21 row(s) names the gold document and
page, so there is nothing to measure`, exit 1).

### Step 6 - pressure-class policy, document type, reason code (D-99 to D-101)

- `core/config.py`: `comparison_pressure_class_higher_covers_lower` (default
  `false`) and `ComparisonPolicy.pressure_class_higher_covers_lower`, mapped in
  `Settings.comparison`.
- `comparison/registry.py`: `evaluate_ordered_categorical` is Full only on
  equality, or on a higher class when the policy says so. A higher class is
  otherwise Partial at `partial_score_max` with review; a lower class stays
  Missing.
- `data/categories.json`: `families` per valve type (globe: `Fisher globe
  valves`; ball: `Series 7/8`, `Worcester controls`, `VM-4700-S`; control:
  `D-Series`, `Control valves`).
- `services/categories.py`: `ProductType.families`, family-aware `infer_type`,
  `document_type`.
- `services/retrieval.py`: `Candidate.document_type` / `type_unconfirmed`;
  `_type_filter` uses the chunk's own type first, then the document's declared
  type, and marks the candidate `type_unconfirmed` when neither names one.
- `services/matching.py`: `type_unconfirmed` sets `applicability =
  family_level` and a review reason; `MatchResult.reason_code`.
- `repositories/schema.py`, `alembic/versions/0011_result_reason_code.py`:
  `result.reason_code` (VARCHAR(64), CHECK constraint).
- `services/findings.py`, `repositories/runs.py`, `services/report.py`: write,
  read and expose the code in the JSON report, review queue and CSV.
- Tests: pressure-class default/opt-in, family placement, silent-chunk
  dropping, unknown-type family-level, unmapped-attribute vs silent corpus.

*Measured:* 519 backend unit tests pass (8 skip without a database). With
`TEST_DATABASE_URL` pointed at a disposable `tender_it`: 527 pass, including
migration `0011`. Migration `0011_result_reason_code` applied to `tender_test`;
corpus unchanged (7 tenders, 70 documents, 20,892 chunks, 1,368 results).

### Step 7 - proposed gold slice for items 10 and 11 (awaiting confirmation)

- `eval/gold/proposed-GEM_9671792-items-10-11.csv`: six proposals, one per
  requirement x candidate document, for tender `GEM_9671792_Valve`
  (`9752351d...`). Every row is `label_state=proposed`, `confirmed=no`, and
  carries the verbatim `gold_quote`, `gold_page`, `true_status` and a
  `proposal_reason`. Quotes were checked as exact substrings of the named PDF
  page before the file was written.
- `eval/recall.py`: `load_labels` now reads `label_state` / `confirmed`. A file
  that declares those columns measures only confirmed rows; proposals are
  counted as *unconfirmed* and a file of proposals alone is refused. A file
  with neither column keeps the old behaviour.
- `eval/tests/test_recall.py`: 2 new tests (a proposal is skipped, a file of
  proposals is refused); 32 eval tests pass.
- `eval/README.md`: the recall section documents the confirmation columns.

*Proposed:* Delval Series 7/8 - items 10 and 11 `missing_capability` (flanged /
butt weld only; 122 mm not stated). Flowserve Worcester Series 44/59 - item 10
`partial_match` (class 600 vs 150, plus F2F/cast unstated), item 11
`partial_match` (F2F/cast unstated). Val-Matic QuadroSphere - items 10 and 11
`missing_capability` (no screwed/socket ends, forged not cast, three-piece and
122 mm not stated).

*Measured:* `recall.py --gold .../proposed-...csv` exits 1 with "none of the 6
row(s) is confirmed", as intended. Recall is **not** measured yet: it waits on
the reviewer confirming the six rows.

### Step 8 - recall@k on the confirmed item 10/11 slice

- `eval/gold/confirmed-GEM_9671792-items-10-11.csv`: the six proposals, now
  `label_state=confirmed`, `confirmed=yes`. Nothing else changed.
- `eval/gold/recall-GEM_9671792-items-10-11.json`: the measured report.

*Measured (local `tender_test`, 20,892 embedded chunks):*

| k | recall | hits |
| --- | --- | --- |
| 3 | 33.3% | 2/6 |
| 20 | 66.7% | 4/6 |

Misses by cause: `corpus_gap` 0, `chunking` 0, `retrieval` 2, `verifier` 0.
Both misses are the Delval Series 7/8 bulletin, page 2, for items 10 and 11:
the page is indexed but the product's search does not put it in the top 20.
The four hits are Worcester item 10, Worcester item 11, and Val-Matic items 10
and 11.

The Delval document is found: page 1 is rank 1 for both items. Page 2 is the
specification page (rating, WCB, face-to-face) and does not make the fused
top 20. The query's distinctive terms for these items - "screwed end",
"socket weld", "122 mm" - are absent from the whole bulletin, so ranking is
carried by the generic terms, which page 1 matches more strongly. Both
searches fetch 50 candidates each and the fusion returns the top 20, so page 2
is either below 50 in a modality or loses on RRF.


### Step 9 - short-document expansion and alias query expansion (D-102)

- `core/config.py`: `retrieval_sibling_page_limit` (default 3).
- `comparison/vocabulary.py`: `AttributeSpec.query_expansion`, read from the
  vocabulary file.
- `data/attribute_vocabulary.json`: new `connection_type` categorical
  attribute with the names `end connection`, `body style`, `ends`,
  `connections`, `connection`, `end`, marked `query_expansion: true`.
- `repositories/aliases.py`: `AliasRepo.approved_aliases(threshold)` groups the
  approved, confident aliases by canonical term.
- `services/retrieval.py`: `expand_query` widens a query with the alias
  table's other names for an expandable attribute; `expand_siblings` reads a
  document of at most `retrieval_sibling_page_limit` pages whole when one of
  its pages is retrieved, inserting siblings in page order after the hit and
  trimming to the same budget. The fused order is not re-scored.
- Tests: query expansion adds the other names and leaves an unrelated query
  alone; only documents of at most three pages are eligible; siblings are
  inserted in page order; the candidate budget is kept.

*Measured:* `tests/test_retrieval.py` 19 pass; vocabulary, resolution,
comparison and normalizer tests pass; full unit suite exits 0.

### Step 10 - listed-set exclusion is Missing, silence stays No evidence (D-103)

- `data/attribute_vocabulary.json`: `connection_type` now also names
  `screwed end`, `socket weld end(s)`, `butt weld end(s)` and `flanged end(s)`,
  so a requirement's own wording supplies the value to look for.
- `services/verifier.py`: `ListedSet` and `listed_set_exclusion`. A categorical
  attribute followed by `:` or `=` and a list of at least two short values is
  read as an availability list; when the required value is not in it, the
  finding is `MISSING_CAPABILITY` with the verbatim line as its quote.
- `services/matching.py`: when extraction finds no claim, the listed-set
  finding is used only if the citation layer confirms the quote on the page;
  otherwise the result stays `NO_EVIDENCE_FOUND`.
- Tests: verifier tests for the Delval body-style list and for a silent page;
  matching tests for the Missing and No-evidence statuses end to end.

*Measured:* the verifier finds `connection_type` / `screwed end` and
`connection_type` / `socket weld ends` against Delval page 2, quoting
`Body Style: Flanged end / Butt weld end`. `test_verifier.py` and
`test_matching.py` pass (43); full unit suite exits 0.

### Step 11 - atomic gold rows and two recalls (D-104)

- `eval/gold/proposed-GEM_9671792-items-10-11-atomic.csv`: 22 proposal rows,
  one per requirement x manufacturer x attribute (valve type, nominal size,
  pressure class, face to face) for Delval Series 7/8, Worcester Series 44 and
  Val-Matic QuadroSphere. Every row is `label_state=proposed`, `confirmed=no`,
  and carries `required_value`, `documented_value`, page and a verbatim quote.
- `eval/recall.py`: `RecallRow.document_hit_at`, `RecallReport.document_hits` /
  `document_recall`, and `render` prints document recall and page recall as
  two lines per k. The JSON payload gains `document_recall` / `document_hits`.
  Cause attribution is unchanged and stays page-level.
- `eval/tests/test_atomic_gold.py`: the rows are proposals only, unique per
  requirement/manufacturer/attribute, use a known status, and every quote is
  an exact substring of the named page (skipped when the corpus PDFs, which
  are not in git, are absent).
- `eval/tests/test_recall.py`: a document found on the wrong page is document
  recall 100% / page recall 0%, and is still a `retrieval` miss.

*Measured:* 22 atomic rows written; every quote verified as an exact
substring of its page. `test_atomic_gold.py` + `test_recall.py` pass (22).

*Open:* the atomic rows are proposals - they are not measured until a reviewer
confirms them, and `eval/recall.py` refuses a file of proposals (D-98). The
confirmed slice is untouched.

### Step 12 - the atomizer in production, env-driven LLM, resumable limits (D-105)

- `services/atomizer.py`: `ATOMIZER_VERSION` and `atomize_records`. A prose
  clause is split into atoms before the model reads it; each atom keeps the
  parent's item, page, section, span, page tag, confidence and review flag,
  and is marked `prose`. Table rows are left alone; an already atomic clause
  is returned unchanged.
- `services/pipeline.py`: the requirements stage calls `atomize_records`
  after fragments are dropped; `_atomizer_version()` is part of the
  requirements stage key. The reader's `extraction_method` now reaches
  `extract_requirements` and travels through the LLM cache (`_as_dict` /
  `_as_record`), so a prose clause is not stored as a table row.
- `services/extractor.py`: the docstring now says where the deterministic
  fallback lives - the clause is already atomized when the model is asked, so
  a failed read leaves a flagged atom rather than nothing.
- Tests: a compound prose clause is split with its provenance; a table row is
  not split; an atomic clause is unchanged; a review flag survives the split;
  a compound prose clause produces one requirement and one model call per
  atom in the pipeline; a rate limit mid-atoms keeps the unread atoms and
  marks the stage `rate_limited`; `LLM_BASE_URL`, `LLM_API_KEY` and
  `LLM_MODEL` drive the client, and a missing key is a loud error.

*Measured:* `test_atomizer.py`, `test_pipeline.py` and `test_config.py` pass
(58); full unit suite exits 0. The rate-limit path was already covered at the
pipeline, run and audit levels; the new test proves the atomized tail survives
it.

### Step 13 - a fixed-seed audit sheet, and the after-state recall (D-106)

- `eval/audit_sheet.py`: asks `match_requirement` for every requirement x
  manufacturer pair, writes one row per pair with the published status,
  verification outcome, applicability, confidence, review flag, reason code,
  score, document, page and quote, and samples it with
  `random.Random(seed)`. Rows are ordered by (item, requirement, manufacturer)
  before the sample, so the database's order cannot change the sheet.
  `audit_ok`, `correct_status` and `notes` are written empty. A rate limit is
  re-raised as `RateLimited` and the answered pairs stay in a checkpoint, so a
  rerun asks only the missing pairs. The candidate cap is written on every row
  and in the provenance file.
- `eval/tests/test_audit_sheet.py`: same seed over the same rows picks the
  same sample regardless of input order; a different seed can pick a different
  set; a sample larger than the rows keeps every row; a sample below one is
  refused; the reviewer's columns are empty and the pair ids are `P01...`;
  a rate limit at the third pair keeps the first two and the rerun asks only
  the two that were missing.
- `eval/README.md`: how to run the export, what the two empty columns are, and
  why the cap is recorded.
- `docs/DECISIONS.md`: D-106.

*Measured (audit sheet):* `eval/audit/GEM_9671792-items-10-11-audit.csv`, 15
rows sampled from 20 published results (seed `20261030`, cap 3,
`openai/gpt-oss-20b`, 10 manufacturers with documents). Reviewer columns
empty on all 15. The provenance file records the seed, cap, model, embedding
backend and manufacturer list. The 20-row population is in the checkpoint.
Statuses in the sample: 13 `no_evidence_found`, 2 `missing_capability`.

What the sample already shows, before a reviewer grades it:

- **Delval and Val-Matic are still `no_evidence_found` at cap 3.** Both
  confirmed rows are `missing_capability`, and the listing that says so is on
  page 2 of the Delval bulletin (page 1 was retrieved). Page recall@20 is
  100%, so the page is reachable; it is the cap of 3 that hides it. This is
  the cap-3 gap, not a retrieval failure.
- **Emerson Fisher gives `missing_capability` on unrelated control-valve
  documents** (`d103554x012.pdf`, a Fisher ED/EWD valve bulletin) for both
  items. It is flagged for review and carries `applicability=family_level`,
  which is the D-100 rule doing its job, but the manufacturer is a control
  valve maker answering a ball-valve item.
- **Unrelated manufacturers return motor and pump documents** (bharat_bijlee
  motors, sulzer pumps, Xylem pumps). They are `no_evidence_found`, so the
  status is right, but the rows show the category filter does not exclude a
  manufacturer whose corpus is a different product family.

*Measured (recall, before -> after):* the same six confirmed pairs, the same
`DatabaseRanking` path as the product.

| cut-off | document recall | page recall |
| --- | --- | --- |
| @3 | 66.7% -> 66.7% (4/6) | 33.3% -> 33.3% (2/6) |
| @20 | 66.7% -> **100%** (6/6) | 66.7% -> **100%** (6/6) |

The two misses before were both Delval item 10/11: the page was indexed but
not in the top 20. After the query expansion and sibling read, both are in the
top 20 and no miss remains (`corpus_gap`, `chunking`, `retrieval`, `verifier`
all 0). The @3 numbers did not move, which is why the cap matters: the
pipeline that reads three candidates still cannot see the Delval listing.

*Open:* the sheet is not a measurement until a reviewer fills it, so status
accuracy, the false-confident rate and citation correctness are still
unmeasured on confirmed rows. The Delval cap-3 gap and the unrelated-corpus
rows are the two things the sample points at; neither is fixed here.

### Step 14 - read a short document whole, and stop a different product answering (D-107)

Two findings from the step-13 audit sheet, fixed at retrieval.

- `services/retrieval.py`: `_merge_siblings` no longer trims away the pages of
  a short document it just expanded. The cap removes the lowest ranked
  candidates outside the document; a document of three pages or fewer is one
  unit. `_category_scope_filter` drops a document whose own name or declared
  model positively names another product category, with the same silence rule
  as the type filter. `_type_filter` now lets the document's declared type
  decide for the whole document, instead of keeping a page that happens to
  name the wanted type.
- `services/categories.py`: `_term_pattern` normalises the term as well as the
  text, so a hyphenated family ("Series 7/8", "VM-4700-S", "D-Series") matches
  a title that writes it another way - before this, every such family in the
  data file was dead. `mentioned_categories` names the categories a document
  declares, including a file name that runs its words together.
- `data/categories.json`: the Fisher control-valve families `ED/EWD` and
  `ET/EWT` are declared under `control`, so those documents say what they are.
- Tests: a family matches however it is punctuated; `mentioned_categories`
  tells silent from different and reads a run-together file name; a page in a
  document of another type is dropped; a document that names another category
  is dropped; a document that names none is kept; a short document is not
  split by a small cap.

*Measured (retrieval, cap 3, items 10 and 11, six confirmed pairs):*

| cut-off | document recall | page recall |
| --- | --- | --- |
| @3 | 66.7% -> **100%** | 33.3% -> 66.7% |
| @20 | 100% -> 100% | 100% -> 100% |

The two Delval rows that were `no_evidence_found` at cap 3 now retrieve the
whole bulletin (pages 1, 2 and 3), and the whole Val-Matic specification.
`Emerson Fisher` returns no candidates for either item: its four documents are
control and globe valve bulletins, and the item is a ball valve. The pump and
motor corpora (Xylem Godwin, Xylem Goulds, sulzer's submersible pumps,
bharat_bijlee's motors) are no longer searched for a valve item.

*Measured (the two false positives, item 10, cap 3):*

- `Emerson Fisher`: 0 candidates, 0 model calls, `no_evidence_found`, flagged
  for review. The false `missing_capability` from the step-13 sheet is gone.
- `delval_flow`: 11 candidates (the whole bulletin, pages 1-3), 11 model calls.
  The status is still `no_evidence_found`, but no longer because the page was
  missed: the comparison stopped at `attribute_not_mapped`.

*Open, and now diagnosed:*

- **Delval's status is still not the gold status.** The clause is a run-on
  ("Three piece ball valves 2" Screwed end ASME class 150 Cast carbon steel
  Face to face distance 122mm") that the atomizer does not split, so the
  normalizer has no comparable attribute or value and the comparison cannot
  run. Reading the page was necessary but not sufficient.
- **The deterministic listed-set fallback is not safe on a dense page.** The
  Delval page 2 is one chunk carrying the whole specification. Run directly,
  `listed_set_exclusion` reads that chunk as a list of `pressure_class` values
  and returns a `missing_capability` with the entire page as its quote. It did
  not fire in the run because the model returned a fact for that chunk, so the
  fallback was not reached - which is luck, not a guard. It needs line-level
  text (the page is chunked as one line) before it can be trusted there.
- The remaining page recall@3 gap is the Flowserve Worcester bulletin, which is
  16 pages and so is not read whole; its page 4 quote is found at @20 but not
  at @3. That is the candidate cap, not a page-ordering bug, and the product
  default cap is 20.
- The audit sheet in `eval/audit/` is the pre-fix sample; re-exporting it at
  the same seed is a separate, LLM-costed step.

---

## Phase 18 - the flow a reviewer can actually run (started 2026-11-01)

The assignment PDF was read against the codebase, end to end, with the question
"can a person who has only this repository get from a tender PDF to a report?".
The verdicts and the guards were mostly right; what was broken was reachability.
Four paths stopped short: a machine with no model key produced a run that never
left `queued`; a fresh clone had no corpus and no way to get one; the tender
already in the database had no page ledger; and a document the collector could
not fetch had no way in except the filesystem. This phase opens those four,
then strengthens the corpus itself. No LLM call was made in this phase.

### Step 1 - a run with nobody to ask is refused at the request (D-108)

- `services/runs.py`: `needs_model(requirements)` decides whether the tender
  has to ask anything at all, and only then does `create_run` call
  `config_error()`. A refusal carries the reason in the message; the 409 "the
  tender has not been read" path is unchanged. `LLMClient()` is constructed
  inside `execute_run`'s guarded block, so a run that loses its key between the
  request and the worker lands `failed` with that reason.
- `services/e2e.py` and the tests keep the same split: a tender of nothing but
  commercial lines is answerable with no key.

### Step 2 - the wait is read from the response (D-109)

- `services/llm.py`: `Retry-After` (seconds or an HTTP date, header name
  matched case-insensitively), a JSON body field at any depth
  (`retry_after`/`retryAfter`/`retry_delay`/`retryDelay`), or prose ("try
  again in 24.9s") - a bare number is seconds, a suffix says what it counts.
  No provider's exception class is imported. `config_error()` names no
  provider.
- `services/runs.py`: `llm_base_url` is in the config snapshot, because the
  same model name served from a different endpoint is a different answer.

### Step 3 - a budget the run stops at, and resumes past (D-110)

- `core/config.py`: `llm_call_budget` and `llm_token_budget` (0 is no
  ceiling), and both in the snapshot.
- `services/llm.py`: `_check_budget` before every call, against the client's
  own counters, raising `LLMBudgetExceeded` with what was already spent.
- `repositories/runs.py`: `answered(run_id)` - the requirement x manufacturer
  pairs the run stores.
- `services/runs.py`: the loop skips answered pairs; `LLMBudgetExceeded` joins
  `LLMRateLimited` in the handler that commits what it found, labels the run
  `rate_limited` and names why, so the next pass pays only for the rest.

### Step 4 - the corpus comes back on a machine that only has the manifests (D-111, D-113)

- `services/corpusrestore.py` (new) and `make corpus-restore`: fetches only
  addresses a manifest already records, refuses to write a file whose hash
  differs from the manifest, re-asks robots.txt, and reports an address-less
  row as unrecorded. `--dry-run` reports without fetching.
- `core/config.py`: `corpus_max_bytes` (200 MB), separate from the upload cap.
- `core/fetching.py`: `check_url`/`fetch` take `from_document`. The host
  allowlist is a rule about where an address came from - it gates a link read
  out of a tender, not an address a person recorded and hashed. The private
  address, redirect, size, time and robots checks apply to both paths.
- `services/ingestion.py`: "no corpus found" is a warning that names the two
  commands that would put it back, and `main` prints the same on stderr when
  the directory holds no document.
- The default `fetch_allowed_domains` was left exactly as it was: widening it
  would have loosened the fence for untrusted links **and** changed
  `stage_keys(fetch_config=...)`, invalidating the stored tender's stages and
  forcing a paid re-extraction.

### Step 5 - tender to report in one command (D-112)

- `services/e2e.py` (new) and `make e2e TENDER=<pdf|dir|tender_id> [ARGS=--check]`:
  corpus -> embedding -> tender -> matching -> report, each step checking its
  own precondition and the command stopping at the first it cannot meet, with
  the reason and the command that fixes it. `skipped` and `blocked` are
  different answers.
- `api/routes.py`: `GET /api/preflight`, with `can_run`, `why_not` and
  `next_steps`. It downloads nothing and calls no model.

### Step 6 - a page that cannot be read says why (D-114)

- `services/docparser.py`: `tesseract_missing()` and `ocr_page_with_reason`;
  `ocr_page` keeps its old shape as the thin wrapper.
- `services/tenderingest.py`: `_ocr` returns the reason, so the ledger's
  `skip_reason` distinguishes "this machine has no OCR program" from "the OCR
  failed on this page" from "this page holds nothing to read".
- `backend/Dockerfile`: installs `tesseract-ocr` and `tesseract-ocr-eng` -
  Tesseract is a program the image carries, not a package `pip` installs.

### Step 7 - the page ledger is backfilled for the tender already read (D-115)

- `services/pipeline.py`: `_ledger_is_empty`, and the requirements-skipped
  branch re-reads the pages locally and writes the ledger. No model call: the
  pages come from the parser on disk.
- `repositories/tender.py`: `list_pages` orders by page.
- `tests/test_ledger_db.py` (new, integration): every page state round-trips
  including the failed page's reason and its attachment; a second read
  replaces rather than appends; an unread tender has an empty ledger; writing
  no pages leaves none.

### Step 8 - a document the fence refused is supplied in the UI (D-116)

- `frontend/src/api.ts`, `types.ts`: `getAttachments` and `uploadAttachment`
  for the endpoints that already existed and nothing surfaced.
- `frontend/src/components/UploadPage.tsx`: an attachments panel under the
  tender - name, address, hash, kind, status, size, pages - with the
  `failed` and `restricted` rows marked as ones to supply and a file picker
  that posts them. The notice says to re-upload the tender PDF to have the
  document read with it, and that the stages which do not depend on it are not
  run twice.
- `frontend/src/styles.css`: the list, the warning tag and the supply block.

### Step 9 - the corpus is less generic

- `collect/sources/`: nine new source files - ABB, AVK, Bharat Bijlee, Bray,
  Crompton, Duncan Engineering, Ebara, Intervalve Poonawalla, KSB (pumps and
  valves) - 51 entries, every one a real address that was fetched, with its
  note naming what the document is. Distributor copies (Lenntech, Puffer,
  VVF) carry `"secondary": true`, which is what ranks them below a canonical
  one in `services/evidence.py`.
- The run collected 40 new documents and failed on none.
- `backend/data/catalogue/manifest.csv`: 14 hand-added rows had no address.
  The address was recovered for 7 of them - valmatic's drawings, sulzer's
  E00543 and bharat_bijlee's six - and recorded only where the live host
  serves bytes whose sha256 equals the file on disk. The other 20 blanks stay
  blank: `sulzer/salesprogram_e00543.pdf` is served as a different revision
  now, and a row that vouches for a file the host no longer has would make the
  report's page citations point at something else.
- Grundfos and Wilo are reported gaps, not gaps in the effort: Grundfos'
  robots.txt and CDN block the PDF paths, and Wilo's catalogue is served
  through a form rather than a direct address.

### Step 10 - a wildcard robots.txt rule is honoured, and the corpus was re-audited (D-117)

Writing step 9 down meant checking that every document in it was collected the
way the project says documents are collected. It was not.

- `app/core/robots.py` (new): the matcher - `*` is any sequence, `$` anchors
  the end, an unanchored rule decides by prefix, the longest matching path wins,
  an Allow of the same length wins the tie, and the group is the one whose agent
  token appears in the user agent with `*` as the fallback.
  `urllib.robotparser` ignores a rule carrying a wildcard, tested against
  `*.pdf`, `/*.pdf` and `/*.pdf$`: none of them blocked anything.
- `core/fetching.py` and `collect/collect.py` both use it, so the collector and
  the fence give one answer to "did this host allow this". The collector imports
  the app's matcher rather than carrying a second copy of the rule.
- `tests/test_robots.py` (new): 13 tests, including the gap pinned from the
  other side - the standard library's parser still allows the document this one
  refuses.
- *Audited:* the corpus's 101 recorded addresses against their live robots.txt.
  Four are refused by the corrected rule, all four KSB catalogues fetched from
  `www.ksb.com`, whose robots.txt says `Disallow: *.pdf` - the rule
  `fetch_allowed_domains` already cites as the reason KSB is off the tender-link
  allowlist. The two KSB booklets held from distributor hosts (lenntech.com,
  puffer.com) pass those hosts' own rules and stay, marked secondary.
- `make corpus-restore` now refuses the four, so a fresh clone reports them as
  gaps instead of fetching them again.

**Withdrawn, with the user's agreement:** the four files were on disk and in the
database, so a report could have cited documents the host does not allow
fetching. They were removed - `raw/dow-*.pdf`, 1,025 chunks and 4 document rows
(0 facts depended on them) - and their manifest rows now say what a row says
when a host has refused the document: `status: restricted`, the
`restriction_note`, the URL kept so a person can see what is missing and why,
and no file claimed. A manifest row is the record of an attempt, not a promise
that the bytes are here. Deleting them cannot be undone by re-fetching, because
the fence now refuses them, which is the point.

### Step 11 - the corpus counts reconcile, and the key is bounded before it is used

Three counts from the same corpus looked like three corpora: `make ingest` said
128, `corpus-restore --dry-run` said 117, the database said 101. They are one
corpus seen three ways, and the difference is now written down where each number
is printed:

- **117** is the files on disk: 47 under `manufacturers/*/raw/`, 70 under
  `catalogue/`. The four `restricted` KSB rows are not files, so they are not
  counted here.
- **128** was the manifest *entries* naming those files: 16 of the 101 documents
  are named more than once - 5 under both `manufacturers/bharat-bijlee/raw/` and
  `catalogue/bharat_bijlee/`, and 11 under `manufacturers/<m>/raw/` *and* twice
  in the catalogue, once from `catalogue/manifest.csv` and once from the
  `catalogue/<m>/manifest.json` written beside it. That is 27 entries over the
  101 files they name. `ingest_corpus` walked all of them and reported the walk,
  not the corpus - now fixed (D-118): it keeps the hashes it has already seen in
  the run and reads each file once, so the command prints 101.
- **101** is the distinct content hashes among the 117, which is what
  ingestion dedups on and what `document` holds - 101 rows, 101 hashes, none
  with a null path.
- Of the 117, **97 carry an address** a fresh clone could re-fetch, over 81
  distinct addresses; the remaining 20 (`bharat_bijlee` 4, `sulzer` 8,
  `valmatic` 3, `wilo` 5) are blanks in `catalogue/manifest.csv` and stay
  blanks, because the hosts now serve different revisions (step 9).

`make e2e TENDER=<id> --check` was re-run after the withdrawal and answers
`corpus 101 document(s), 29688 chunk(s) from 20 maker(s); 117 file(s) on disk`,
`model configured`, `ocr not available here`. The file count counts distinct
paths for the same reason (D-118): it read 128 while it counted listings, and a
preflight that over-counts the work on disk is a preflight that tells a reviewer
to ingest what is already ingested.

*Later the same day:* the `ocr not available here` line was an install that was
not on `PATH`, not a missing program. Tesseract 5.4.0 was already at
`C:\Program Files\Tesseract-OCR` with `eng.traineddata`, and `.env` already set
`TESSDATA_PREFIX`; `shutil.which` simply could not see it. Adding the directory
to the user `PATH` makes `tesseract_missing()` answer `None` and the line
disappear from `--check`. No code changed. What this does **not** do is re-read
the 10 documents already stored as *incomplete*: they are unchanged by hash, so
`make ingest` skips them, and the CLI exposes no `--force` - see the open items.

The first ingest after the change reported `metadata refreshed: 11` - those are
the thrice-listed documents, whose stored path and url moved from the last
listing's values to the first one's - and the run after it reported 0, so the
corpus settles rather than oscillating.

The key side: `LLM_CALL_BUDGET=300` is set in `.env` (the knob was documented in
`.env.example` and unread here until now), so the first real run is bounded
before it starts. `.env` now carries the note that `LLM_API_KEY` and
`LLM_BASE_URL` are a pair, and `--check` / `/api/preflight` name the model and
host they would call - `configured: openai/gpt-oss-20b at
https://api.groq.com/openai/v1` - so a key swapped without the URL is seen
before a call is paid for. `_matching` in `make e2e` reported a `rate_limited`
run as a plain failure; it now names the results the run kept and says the same
command continues, and `test_e2e.py` pins both wordings. No call was made in
this step: the budget is set, the run is not.

### Step 12 - the numbers that were written twice (D-119)

A sweep of the repo for literals that a deployment would have to change in code,
run against the rule `AGENTS.md` already states (config and thresholds come from
the environment). Seven closed; one class deliberately left alone.

- **The fetch timeout had two answers.** `robots_allows()` used `timeout=30`
  while `fetch_timeout_seconds` says 60, so how long a site is waited for
  depended on which code path asked it. It now reads the setting.
- **The user agent had no home.** The same string was written in
  `core/fetching.py` and `collect/collect.py`; both now read
  `fetch_user_agent`, which is what makes "did this host let this agent in"
  answerable once.
- **The collector had no size cap while the restore does.** `make collect` would
  store a document `make corpus-restore` refuses to bring back, because the
  former streamed without limit and the latter checks `corpus_max_bytes`. The
  collector now pre-checks `Content-Length` and stops mid-stream at the same
  cap, reporting `too_large` rather than writing the bytes.
- **The document-type vocabulary was the one vocabulary with its path in code**
  (`attribute_vocabulary_path` and `category_vocabulary_path` both had a
  setting); it got `doc_types_path`, and `collect/collect.py` now imports
  `get_settings` instead of re-deriving the data directory, the timeout and the
  manifest paths.
- **The embedding batch size was 32 in two files** - the corpus walk and the
  encoder - so a change to one silently split a batch. `embedding_batch_size`
  is read by both; `embed_corpus`'s unused `batch_size=` parameter went with it.
- **The health endpoint said `environment="local"`** from a string in the route,
  on any machine, including a container. That is `app_environment` now.
- **A stored row's version came from a literal beside the signature** while the
  stage key and the cache came from `settings.schema_version` - the combination
  that writes rows no resume path reads. `to_stored` defaults both versions to
  their single sources and `StoredRequirement` no longer carries defaults for
  them, so a caller has to say which shape it wrote.

Left as constants, on purpose: the pure geometry inside the page parsers - a
table's column tolerance, a run's pixel bounds - is not a deployment's choice,
and a setting for it is a value nobody would tune. The tender-*reading*
thresholds (`tenderingest.py`'s minimum text length, image share and fragment
rules, `matching.py`'s high-match cut-off, `retrieval.py`'s default limits) are
the same class of number with a different answer: they change what a report
says. They are listed under Open, not changed here.

*Measured:*

- Step 12: `test_config.py` 9, `test_health.py` 4, `test_pipeline.py` 32,
  `test_embeddings.py` 9, `test_llm.py` 23 → **77 collected, 76 passed, 1
  skipped**; `collect/test_collect.py` 15 passed (the new cap test among them),
  `test_fetching.py` + `test_corpusrestore.py` + `test_robots.py` 61 passed. Two
  of the new tests pin the sweep itself: the health payload reports the
  configured environment, and a row written by `to_stored` carries the versions
  the run's own cache key is built from. `.env.example` was diffed against
  `Settings.model_fields` - every setting is now documented, and
  `pipeline_concurrency`, which no code read, was removed rather than
  documented as a knob that does nothing.
- Step 11: `test_ingestion.py` 10 passed (the new dedup test among them) and
  `test_e2e.py` 21 passed, 1 skipped (the stopped-run and endpoint-name tests
  among them); the whole corpus was re-ingested twice and the second pass wrote
  nothing, and no model call was made.
- Targeted backend unit tests (the eight files this phase touched -
  `test_llm`, `test_runs`, `test_e2e`, `test_corpusrestore`, `test_fetching`,
  `test_docparser`, `test_pipeline`, `test_tenderingest`): **178 passed,
  1 skipped.** `test_robots.py` 13 passed; `test_fetching`, `test_corpusrestore`
  and `test_tendercollect` together 40 passed; `collect/test_collect.py` 14
  passed. The full suite was not run, by instruction.
- `test_ledger_db.py`: 4 integration tests pass against the running database.
- Frontend: `UploadPage.test.tsx` 10/10, `tsc --noEmit` clean, `vite build`
  clean.
- Corpus ingest of the new collection: **35 documents read, 9821 chunks written,
  97 unchanged, 0 failed**, and 10 documents labelled *incomplete* because some
  of their pages carry no text layer - `bbl_craneduty.pdf` 15 pages,
  `dow-product-portfolio-data.pdf` 64. This machine has no Tesseract, so D-114's
  reason is what those pages record: install the UB-Mannheim build, or run the
  stack with `make docker-up`, whose image carries it.
- The database after this phase: 105 documents, 30713 chunks, 99 products.
  `make embed` added the 9821 vectors the ingest wrote and failed on none, so
  every chunk in the corpus has one.
- After the withdrawal: **101 documents and 29,688 chunks**, `make ingest`
  reads the tree clean (0 new, 101 unchanged, 0 failed) and
  `make corpus-restore --dry-run` counts 117 documents in place with none
  failing. Nothing was missing from the corpus; the numbers answered different
  questions, and step 11 reconciles them.
- `make e2e TENDER=<id> ARGS=--check` on the stored GeM-Bidding-9713870 tender
  answers, without a call or a download: `corpus 101 document(s), 29688
  chunk(s) from 20 maker(s); 117 file(s) on disk`, `model configured`, and no
  `ocr` line since Tesseract became reachable on `PATH`. The one thing between
  this machine and a report is the run itself.

**Open:**

- **Nothing has been measured end to end with a model.** `make e2e` is
  verified up to the step that needs a key: `--check` and the preflight answer
  correctly with no configuration, and the refusal names what is missing. The
  first real report still has to be produced by the user's corporate key.
- The stored tender's `tender_page` rows exist only after the pipeline is run
  on it again; the backfill is unit-tested and the writer is proven against the
  database, but the stored tender has not been re-run in this phase.
- Of the 117 documents on disk, **97 have an address a fresh clone can restore
  and 20 do not** - 4 under `bharat_bijlee`, 8 under `sulzer`, 3 under
  `valmatic`, 5 under `wilo`. They need a person to record an address, not a
  guess, and `corpus-restore` names them as gaps when it finds them.
- The corpus has no KSB document fetched from `www.ksb.com` at all, and the two
  that remain are distributor copies (Lenntech, Puffer) marked secondary. If
  KSB coverage matters for a tender, the way to get it is a source the host
  allows - a distributor, or a `manual` entry a person supplies - not a fetch
  the fence refuses.
- **Tesseract is on this machine and now on `PATH`**, so a page with no text
  layer can be read rather than recorded as `failed`. The 10 corpus documents
  ingested as *incomplete* are still incomplete, because they are unchanged by
  hash and `make ingest` skips them: `ingest_corpus` takes `force=True` but the
  CLI never passes it, so there is no supported way to re-parse a document that
  did not change. Exposing `--force` and re-running is what turns those 10 into
  `ocr` pages - no model call, 79 image-only pages.
- `eval/audit/` is still the pre-fix sample; re-exporting it is LLM-costed.
- **The thresholds that change what a report says are still literals.**
  `matching.py`'s 0.99 high-match cut-off, `tenderingest.py`'s minimum text
  length (40, where `INGEST_MIN_CHARS_FOR_TEXT` says 10 for the same question),
  its image-share and fragment rules, `tendercollect.py`'s copy of the same
  0.5, and `retrieval.py`'s two default limits, which shadow
  `MATCH_MAX_CANDIDATES` when a caller does not pass one. Each decides a status
  or a recall, so each is a deployment's choice and belongs in config.
- **The container does not read `.env`.** The two empty-string model defaults
  are gone (Step 14), and what is left is the rest of the same gap:
  `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-tender}` still contradicts
  `.env.example`'s `change-me`, and the image carries no `.env`, so of the knobs
  now documented, only the handful the compose file names reach the app.
- **The frontend restates numbers the backend owns.** `ReportPage.tsx` writes
  "mandatory ×2, optional ×1" in prose while the report payload carries no
  weights, and `POLL_MS = 1000` is declared twice. The prose is the kind of
  thing that stays true in the UI after `SCORING_WEIGHT_MANDATORY` changes.

### Step 13 - a read no process will finish is reconciled at startup (D-120)

Written while step 12 (single-sourced settings) was uncommitted work by another
agent; this step does not depend on it, and the numbers below are in commit
order rather than step order.

The database held one dead row: tender `9d62cf12` (a 2-page GeM specification)
sitting at `processing` since 2026-10-28, carrying the rate-limit message of the
attempt before it. Its stages told the whole story - attachments, items,
requirements and resolution `done`, extraction `rate_limited` - and nothing read
them. The requirements stream stayed open because `processing` means "still
being read", and the UI spun because `phaseOf()` maps every other state to
`reading` and shows `tender.error` only in the failed and rate-limited panels.

- `services/tenderrecovery.py` (new): `final_state(stages, requirements)` is the
  decision, a pure function of the stage rows - `rate_limited` if a stage
  stopped at the limit, `failed` if one failed, `done` if every stage settled
  and requirements were stored, and `None` otherwise. `reconcile(session)` finds
  the `queued`/`processing` rows and records that answer, with the offending
  stage's own `detail` as the reason. It does not commit: the caller owns the
  transaction.
- `main.py`: a lifespan that calls `reconcile` once at startup, and logs rather
  than raises if the database is unreachable, because refusing to start turns a
  degraded service into no service.
- `repositories/tender.py`: `set_status` writes `error` together with the
  status, so starting a read clears the previous attempt's message; new
  `tenders_in_states()`; `count_requirements` returns an int (it returned the
  empty result list when a tender had none, so a comparison to zero raised).
- `repositories/runs.py`: the same `set_status` fix, which had the same hole.

*Measured:*

- `test_tenderrecovery.py` 11 passed - six on the decision without a database,
  five against a real one (the rate-limited repair, an in-flight read left
  alone, a finished read marked done, and both `set_status` cases). The file is
  80 tests with `test_health`, `test_tenderupload`, `test_audit`, `test_runs`
  and `test_ledger_db`: 80 passed, 0 skipped with `TEST_DATABASE_URL` set.
- The dead row itself, repaired by running `reconcile` against `tender_test`:
  `{'was': processing, 'now': 'rate_limited'}`, and
  `tenders_in_states(queued, processing)` is now empty. The error is kept,
  because it is the true cause.

**Open:**

- A tender left `queued` with no stage at all is not repaired: nothing
  distinguishes a lost task from one about to start. It needs a heartbeat, and
  a guess would be worse than the wait.
- `UploadPage` still hides `tender.error` while `phase === 'reading'`. The
  repair fixes the row, not the silence: a read that is genuinely still running
  has no reason to show, and one that stopped now lands in a state the UI
  already renders.

### Step 14 - the unreachable rule and the unread knob (D-121)

The whole tree, not the last diff, read through for code that costs a reader to
find and no reader to notice. Four answers were written twice with one copy out
of reach; all four now have one home.

- **"Between 10 and 20" was never compared.** `matching.py` built the request
  with both ranges `None`, and `evaluate_numeric` failed its own
  *needs a number on both sides* gate before the range branch below it. The pair
  is read back with the grammar that parsed the clause
  (`normalizer.parse_range()` - one pattern list, not a second in the
  comparison) and threaded at the extraction site and the cached-fact site. A
  requirement that states a pair no longer needs a single `required` number to
  reach the comparison.
- **A `between` clause with no pair stopped being blamed on the product.** That
  path returned `missing capability` - a statement that the documents state a
  failing value - when what happened is that we read no pair. It now falls
  through to *no evidence found* with `operator_not_comparable` and goes to
  review (AGENTS #4).
- **The pair is read from the clause, not from the joined text.** The old
  `raw.partition("-")` in `normalize()` turned "between -10 and 20 degC" into
  `('', '10-20')` and dropped a warning; the range now keeps its sign.
- **`SCORING_FULL_SCORE` and `SCORING_MISSING_SCORE` do what they say.** Both
  were in `.env.example`, in `ScoringPolicy` and in the policy a run snapshots,
  and no code read either: every evaluator wrote its own `1.0`/`0.0`. The
  declared score is applied once, in `registry.evaluate()`, and a partial keeps
  the band's graded score.
- **The container keeps its model names.** `LLM_MODEL: ${LLM_MODEL:-}` and
  `EMBEDDING_MODEL: ${EMBEDDING_MODEL:-}` set an empty string when the host had
  no value, which outranked the configured default inside the container. They
  are pass-through now, so unset means "use what the backend is configured
  with". `docker compose config` resolves the real names.
- **Two dead names gone.** `services/facts.py`'s second `PROMPT_VERSION` (no
  cache key or caller ever read it; `extractor.py`'s is the live one) and
  `normalizer.py`'s unused range regex, whose comment described optional
  wording. `collect/collect.py` now calls the app's `doc_types_path()` instead
  of re-deriving configured-or-default.
- **The example file is checked, not remembered.** `test_config.py` compares
  `.env.example` against `Settings.model_fields` in both directions, with the
  seven compose/script/Tesseract names listed as deliberately not settings.

*Measured:*

- `test_normalizer.py`, `test_comparison.py`, `test_matching.py`,
  `test_config.py` → **91 passed**. Six of them are new: a range requirement
  against a single value (full match inside, missing outside), a `between`
  clause with no pair answered as *no evidence found* and not as a capability
  failure, the declared full/missing scores arriving with a changed environment,
  a range clause reaching the registry from a run end to end, and the pair read
  back from "10 to 20"/"-10 and 20".
- `test_resolution.py` + `test_evidence.py` + `test_facts.py` +
  `test_pipeline.py` + `test_scoring.py` + `test_report.py` +
  `test_no_hardcoding.py` + `test_runs.py` → **122 passed**, and
  `collect/test_collect.py` → **15 passed**. No model call was made.
- One of the two scoring settings, `scoring_partial_default`, is still read by
  nobody - a partial's score comes from the comparison band. Recorded under Open.

**Open (added):**

- `scoring_partial_default` (and `ScoringPolicy.partial_default`) is a knob with
  no reader. Either PRD 8's flat partial score replaces the band, or the setting
  and its `.env.example` line go.
- The deletions the same review found - the FR-25 consolidation layer
  (`services/evidence.py`, `EVIDENCE_PRIORITY`, `VerificationOutcome.CONFLICTING`
  inside a database check constraint) and `schemas/models.py`'s unified record -
  are held for the two named decisions, not done here: both are cited by
  `AGENTS.md`, and `CONFLICTING` cannot leave without a migration.

### Step 15 - the last unread knob, and the module nothing called (D-122)

Two names that promised something and had no reader. Neither could change a
result, which is why neither was noticed.

- `scoring_partial_default` / `ScoringPolicy.partial_default` deleted from
  `config.py`, `.env.example` and `docs/CONSTANTS.md`. D-121 had wired the full
  and missing scores into `registry.evaluate()` and listed this one under
  *Left:*; a partial's score has come from the comparison band
  (`comparison_partial_score_min/max`) all along, so there was nothing to wire.
- `services/tenderslice.py` (198 lines) and `tests/test_tenderslice.py`
  deleted. `rg tenderslice` over the whole tree returned exactly one hit - the
  test that imported it. No caller, no Makefile target, no doc reference.
- `README.md` no longer documents `make eval ... GOLD=eval/gold/tender-a.jsonl`.
  That file has never existed. Not repointed at
  `eval/gold/synthetic.example.jsonl`: scoring against synthetic labels prints
  a real-looking accuracy number for labels that were invented.

*Measured:* `test_config`, `test_scoring`, `test_runs` and `test_health` pass
(54). Full suite run before the commit.

**Open:** the folder was not reorganised because it looked crowded. The file
count was checked against the import graph first - 32 modules, 31 with a caller
- and only the one without a caller was removed. Grouping the rest is the next
step, done as a move and not as a rewrite.

### Step 16 - the services folder grouped by stage (D-123)

31 modules in one flat list became six packages. This is a move, not a rewrite:
no module's content changed apart from its import lines.

    tender/       upload collect ingest parse classify pipeline
    requirement/  atomize retype normalize resolve extract
    corpus/       ingest run seed restore audit
    match/        match retrieve verify evidence score findings category facts
    output/       report runs
    cli/          e2e recover

`llm`, `embeddings` and `textnorm` stay at the top: every layer imports them, so
no stage owns them.

*How it was done:* `git mv` per file, then one regex pass over the tree for
`app.services.<old>`, then the `from app.services import a, b` form, then the
Makefile's `-m app.services.*` targets. The first attempt did the replacements
one module at a time and produced `app.services.corpus.run.restore` - `corpus`
matched inside the already-rewritten `corpus.restore`. It also rewrote the tree
before the moves, so the moved modules kept their old imports. Both are fixed in
the script that was actually used: a single alternation pass, and the file list
recomputed after the moves.

*Measured:*

- 28 files moved, 59 files rewritten. Inside a moved file only the import lines
  changed, plus two `Path(__file__).resolve().parents[2]` constants
  (`match/evidence.py`, `match/category.py`) that needed `parents[3]` once the
  file sat one package deeper. The suite caught that immediately - every
  category and doc-type test failed - which is the case a dry run cannot see.
- 47 distinct `app.services.*` references in the tree, all of them importing a
  real module.
- Six test files patched the module object they imported and still called it by
  its old name (`retrieval`, `matching`, `ingestion`, `tenderingest`,
  `tenderupload`, `corpusrestore`). The bindings now use the new name; in
  `test_corpusrestore.py` the module is `restore_module`, because the file also
  imports a function named `restore`.
- Full suite: 645 tests, 0 failures, 0 errors, 0 skipped.

**Open:** `DECISIONS.md` and `WORKLOG.md` before this step name modules by their
old path. Only the one line that pointed at
`app.services.verifier.check_citation` was updated, because it is a pointer to
code; the rest are a record of what was true then. `services/__init__.py` holds
the old-to-new table for a reader following an older decision.

**Left:** `backend/tests/` still has one file per old module name
(`test_atomizer.py`, `test_docparser.py`, `test_matching.py`, ...). Their
contents now import from the new path; the file names were not renamed, because
a test file is named for the job it checks and renaming twenty of them adds
churn to a commit whose whole point is that nothing moved but paths.

---

## Phase 19 - the screens read like the artifact they produce (started 2026-11-02)

The backend and its tests were audited for three phases; the frontend was not.
Read on its own, it was two things: a visual default that could have been
attached to any admin tool, and two affordances a reviewer needs and did not
have - a way back from a report, and a way to stop a page that asks the API once
a second. This phase changes `frontend/src` only. No backend file, no route, no
request shape, no Docker or nginx config, and no number on screen was touched.

### Step 17 - the case-file restyle, back and stop (D-124)

- `styles.css` was rewritten around a token set taken from the work itself: a
  cool ledger paper, ink, one seal navy, and the five PRD 4 verdict colours.
  Three type roles - serif for headings and the evidence quote, system sans for
  body, mono uppercase for labels, page numbers, run ids and document names.
  No webfont, because the bundle is served static through nginx and the demo
  machine may have no network.
- One signature device: the verdict rail, a 3px left edge in the status colour,
  reused on a report row, a rank card, a notice, the drawer, its quote and its
  title, and in miniature in the brand mark. It is drawn from `--rail` and
  `:has()` selectors, so it needed no new DOM and no new field. The mandatory
  gate flips its card crimson with `.rank-card:has(.flag)`, which puts
  "fails mandatory" ahead of the score in the reading order.
- `App.tsx`: each view is entered as a history entry, so the browser Back button
  returns to the upload from a report; the topbar gains a sticky "Back to the
  tender" while a run is open, and an SVG brand mark.
- `components/Trail.tsx` (new): Tender, Match, Report, with the current step
  `aria-current="step"`. Only "Tender" is a button - "Match" is not a screen a
  reviewer can go back to on purpose, so making it clickable would have been a
  broken promise.
- `RunPage.tsx` and `UploadPage.tsx`: "Stop watching" closes the page's own poll
  or EventSource and leaves the last payload on screen, with "Watch it again" /
  "Follow it again" to resume. There is no cancel route in the API and the
  contract was out of scope, so the copy names the thing that stops: the page,
  not the run.
- `EvidenceDrawer.tsx`: `drawer-${result.status}` on the aside, which is how the
  rail gets its colour without a prop per status.
- Motion: a staggered rise on the stack, a breathing dot on a pill that is still
  in flight, a sheen along the progress fill, a press scale on the controls - all
  inside the existing `prefers-reduced-motion` guard.

*Checked before writing:* how a breadcrumb should behave (hierarchy first, an
escape hatch second, complementing Back rather than replacing it) and the polish
list a design engineer expects - concentric radii, a header that says it is
floating, tabular numbers, balanced headings, interruptible motion.

**Left:** no visual-regression harness; the rails and the type roles were
verified by computed style in the browser, which a future edit cannot re-check on
its own. The run page still shows one percentage because that is all the API
reports.

### Step 18 - three backend states the screens did not speak (D-125)

Every route the frontend calls was read against the handler that answers it.
The shapes match - request fields, response fields, SSE event names, the
attachment types, the CORS and nginx wiring. Three states did not exist in the
UI:

- a run ending `rate_limited` was polled forever, because the run page's
  terminal set was `{done, failed}` while the API serves its report. Now
  `{done, failed, rate_limited}`.
- a tender ending `incomplete` left the upload page spinning "Reading the
  tender…" with no run offered and no way out, because anything that was not
  done, failed or rate-limited was read as still reading. It is its own phase,
  quoting the stored reason and naming the one action that works.
- four status words had no label and no colour, and one rule styled a status
  (`running`) that no enum contains. The in-flight list now matches `RunStatus`.

Two tests added, one per dead end. 29 frontend tests pass; build and lint clean.

**Left:** three report fields the API returns and the app drops - `not_assessed`,
`unknown_requirements` and the per-result `reason_code` - and the attachment
panel's advice to re-upload a tender that is already `done`, which the API
ignores. Each needs a decision about which side changes, so none was taken here.

---

## Phase 20 - two things that would have wasted a paid run (started 2026-11-02)

Before spending the company API budget on a real end-to-end pass, the backend
was audited with zero model calls: migrations at head `0011_result_reason_code`,
`/api/health` and `/api/preflight` both 200 with `can_run: true`, 101 corpus
documents and 29,688 chunks all embedded locally, 20 manufacturers and 99
products, 651 tests green. Two defects would have made the run either
misleading or unaffordable, and both were fixed before anything was spent.

### Step 19 - an attachment keeps its own name; a run can name its makers (D-126, D-127)

- `collect._filename_for` now reads the `fileDownloadPath` query parameter GeM
  puts the real name in, and `collect_tender` keeps two links that resolve to
  one name by prefixing the later one with its index. The cached-copy path no
  longer re-freezes a document under the old manifest's name, so a re-collect
  actually writes the corrected names.
- `--manufacturers a,b` on `make e2e`, threaded to `create_run`. Omitted, the
  whole corpus runs as before.

*Measured:* `2bc8f917` had four attachment rows over three files - the 24-page
SLA overwritten by the 17-page ATC; `acdd1135`, a `done` tender, had four rows
over one file. The two colliding GeM links now resolve to
`NewIP_..._CPS2HMDAM1.pdf` and `ATC_..._CPS2HMDAM1.pdf`. The three gold
manufacturers cost 63 pairs and 1,498 candidate passages instead of 420 pairs.

**Left:** the atomizer does not split the two confirmed gold clauses
(`6ffe6648`, `5c07614c`) into atoms - each stays one run-on clause with no
attribute, operator or value, so it cannot reach `missing_capability` until the
parser is worked on. `resolve_roles` still prefers `boq-pdf` over `kind: spec`,
so a tender's SLA and ATC are collected but not read. `_cached_fact` can never
hit inside a run (`_assess` passes no `model`), so every re-run pays full price.
`tender_page` is empty project-wide and self-heals on the next ingest of each
tender. Run `5a9d4ecc` has been `retrieving` since 27 Oct; `reconcile` covers
tenders, not runs.

### Step 20 - the review flag says what it is, and a filled sheet gets graded (D-128)

- Every `review_reason` was read. One path could flag a row with no reason: the
  verifier's `reasons` holds only failed layers, while `review_required` is also
  true when a layer passes unsure - family-level evidence. Added
  `VerificationReport.review_reasons` (failed and unsure) and built the flag from
  it, so the queue never prints "No reason recorded". The rest of the reasons
  name their cause; none is a bare "needs review".
- The review queue and the evidence drawer each carry one line: a flag says where
  to look, not what to decide, and this tool records no decision.
- `eval/audit_score.py` grades a filled sheet - `audit_ok`, `correct_status`,
  `notes` - joined on `pair`, refusing a row nobody graded, a repeated id and an
  unknown status. Reports n audited, accuracy among audited, false-confident rate,
  flagged-but-correct, citation correctness, and that a sample of published rows
  measures precision, not what was missed. Writes `eval/results/`, never the
  sheet. `make eval-audit SHEET=...`; `eval/README.md` and `README.md` describe it.

*Verified:* targeted files only - `tests/test_verifier.py`, `test_matching.py`,
`test_evidence.py` (59 passed), `eval/tests/test_audit_score.py` with
`test_harness.py` (24 passed, 7 of them new, over a synthetic sheet written by the
real exporter), and the 8 `ReportPage` frontend tests. The real
`eval/audit/GEM_9671792-items-10-11-audit.csv` is refused with its 15 unfilled
pair ids and no file written, which is the intended answer while nobody has
filled it.

### Step 21 - a re-read of an already-matched tender crashed (D-129)

The report was concrete: re-uploading `9d62cf12` raised
`ForeignKeyViolation` on `result_requirement_id_fkey`, and the tender stayed at
`processing`. Reproduced against `tender_test`: 31 requirements, one
`rate_limited` run with 7 results.

- `replace_items` and `replace_requirements` now call `_forget_results` first.
  Both delete the tender's requirement rows, and the FK refuses while a result
  still points at one - the items stage deletes first, so that is where the
  re-upload failed. The results go with the rows they answered, and the count is
  logged.
- `audit_tender` rolls the session back when the pipeline raises. Without it the
  caller's `set_status` runs on an aborted transaction, fails, and the tender
  never reaches a terminal state.

*Verified:* three tests - a requirements re-read, an items re-read, and the audit
rollback. On the real tender, inside a rolled-back transaction: 7 results
removed, 31 requirements deleted, no error; after the rollback, 31 requirements
and 7 results still there.

**Left:** requirement ids are `uuid4`, so a re-read re-keys every requirement and
the confirmed gold labels (keyed on `requirement_id`) stop resolving. Stable ids
from item + page + clause text are the real fix, and that is an ingestion change.
The staged run itself is still blocked on the provider: the key in `.env` is a
free Groq tier at 8,000 TPM, which is ~3 calls a minute for this prompt, and
`execute_run` refuses to resume a `rate_limited` run even though its loop already
skips the pairs it has (`answered`). Both need a decision before the paid pass.

### Step 22 - the second attempt continues the first (D-130)

The paid pass is blocked on the provider's per-minute limit, and on a free tier
every stop is expensive: the first attempt on `2bc8f917` spent 9 calls and about
20k tokens, stored nothing, and the next attempt would have paid for it again.

- `execute_run` no longer returns early for a `rate_limited` run. The loop below
  it already skipped the pairs the run had answered.
- `create_run` hands back the newest stopped run for the same tender and
  manufacturer set when `_answers_alike` says the new attempt would ask the same
  questions. The spend limits are excluded from that comparison; the model, the
  candidate cap and everything else are not.

*Verified:* 4 tests changed or added - the resume test now asserts the stored
pair is not re-asked and the run ends `done`, plus the create-run reuse, the
fresh-run case, and `_answers_alike` for a changed budget versus a changed
model or cap.

**Left:** a resumed run keeps the `started_at` of its first attempt, and the call
and token counts are the client's counters for the process, not per run, so a
report cannot state what a resumed run spent in total. Both need a column; a
number reconstructed from logs would be a guess.

### Step 23 - the run says where its seconds went (D-131)

*Date: 2026-11-02. The first step of the speed-up plan, and the instrument the
rest of it is measured with.*

`run.metrics` (JSONB, migration `0012`) holds `stage_seconds` and the four spend
figures. The matcher times `cached_fact`, `retrieve`, `extract`, `page_read`,
`verify` and `compare` per pair through one `stage()` context manager; the loop
times `store`, `summarise` and its own wall clock. A resumed run adds to the row
instead of replacing it, and the clock starts where the last attempt stopped.

Deliberately **not** in `config_snapshot`: that dictionary is what D-130 compares
to decide whether a stopped run may be continued, and a measurement in it would
make every second attempt look like a new configuration and pay for the pairs
again.

Closed the second half of Step 22's **Left** - the counts are per run and stored,
so a report states what a run that stopped twice actually spent. The first half
(`started_at` of the first attempt) is still open and unchanged.

**Files:** `repositories/schema.py`, `alembic/versions/0012_run_metrics.py`,
`repositories/runs.py` (`get`, `set_metrics`), `services/match/match.py`
(`stage`, `merge_stages`, `MatchResult.stage_seconds`), `services/output/runs.py`
(`RunProgress.stage_seconds`, `_record_costs`, `_apply_costs`, `progress`),
`services/output/report.py` (`metrics`, `_cost_table`).

**Verified:** 7 tests added across `test_runs.py`, `test_matching.py`,
`test_report.py`; 103 pass in those files plus the run API. No verdict, score or
flag reads a timing, so no expected-status test moved.

**Left:** `make migrate` has to be applied before the next run writes metrics.
The frontend does not show the timings yet, and the `e2e` command prints the
counts it is handed rather than the stored ones.

### Step 24 - one pass works a run, and no transaction waits on the provider (D-132)

*Date: 2026-11-02. P0 of the priority order the user set after the architecture
proposal was judged.*

Two defects the proposal's review turned up, both about paying twice:

- the transaction the retrieval query opened stayed idle for the ~27 s the
  provider took to answer, because a pair commits only once its result is
  written. `_match_candidate` now releases it before the call, once per passage
  read;
- two callers of `POST /api/runs` can be handed the same resumable run (D-130)
  and the route schedules the work for both, so one run could be worked by two
  passes at once. `result` has no key on a pair, so the pairs were answered
  twice and stored twice. `RunRepo.claim` makes the run row's state the lock:
  the update from `queued`/`rate_limited` to `retrieving` lands once, and the
  caller that lost is told the stored state and the costs recorded so far.

Refused in the same review, and recorded so it is not proposed again: dropping
sibling pages from the extraction prompt (there are none - the prompt is one
passage plus a 1,737-character instruction, and siblings are separate
candidates for documents of three pages or fewer, which D-102 and D-107 added on
purpose); comparing values before the passage is read; and collapsing the five
statuses plus `verification_outcome`, `applicability` and `reason_code` into four.

**Files:** `services/match/match.py` (`_match_candidate`), `repositories/runs.py`
(`claim`), `services/output/runs.py` (`execute_run`).

**Verified:** 3 tests added; 71 pass in `test_matching.py` and `test_runs.py` and
the full unit suite passes. No verdict, score or flag changed, so no
expected-status test moved.

**Left:** P1 next - the candidate-level fact cache. `_cached_fact` needs a
product, which is only known after retrieval, and nothing on the run path ever
writes a fact (`store_fact` is reached only from `match --store`), so FR-21's
cache never hits during a run. A run left `retrieving` by a dead process is still
not resumable, unchanged from before.
