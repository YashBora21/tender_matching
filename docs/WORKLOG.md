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
