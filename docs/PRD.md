# PRD: Tender Requirement Matching System

**Author:** Yash | **Status:** Draft v1 | **Time box:** 2 weeks | **Stack:** Python/FastAPI, React, PostgreSQL + pgvector, Docker

## 1. Overview

A GeM tender can list hundreds of technical requirements. Before bidding, someone must check whether a manufacturer's products can meet each one by digging through catalogues and datasheets. This is slow and error-prone.

This system takes a tender PDF and a curated corpus of manufacturer documents, evaluates every technical requirement against each manufacturer, and produces a compatibility report where **every decision is backed by a verified citation** (document, page, quote).

**Core principle:** the LLM extracts and reads; deterministic code decides; a verifier gates every claim. When unsure, the system says "not enough evidence" instead of guessing.

## 2. Goals and non-goals

### Goals

- G1. Per-requirement status, score and evidence for each manufacturer.
- G2. Tender-level summary: compatibility %, ranking, status counts and lists.
- G3. Every claim cites document, page and supporting text, and the quote is verified to exist.
- G4. Run on a new tender with no code changes.
- G5. Honest confidence handling: a human-review flag instead of confident guesses.
- G6. Machine-readable (JSON) and bid-manager-readable (HTML/React) outputs.

### Non-goals (v1)

- Runtime web scraping or live document discovery (collection is offline and frozen).
- Multi-tenant auth, user accounts, billing.
- Distributed workers or message queues.
- Automatic bid submission or price comparison.

## 3. Users

| User | Need |
| --- | --- |
| Bid manager | Readable report, ranking, what to review manually |
| Technical reviewer | Evidence trail to confirm or overturn each decision |
| Evaluator (assignment reviewers) | Run on 2 tenders, check output against cited pages |
| Developer (Yash) | Add a category by adding a URL list, not code |

## 4. Definitions

These are the status definitions the write-up must state.

| Status | Definition |
| --- | --- |
| **Full match** | Documented capability meets or exceeds the requirement (operator, unit and conditions all satisfied) for the applicable model, with a verified quote. |
| **Partial match** | Capability is close but not fully meeting the requirement (e.g. IP67 vs IP68), **or** the requirement is met only with a documented optional feature or condition, **or** only some parts of a compound requirement are met. |
| **Missing capability** | Documents state the capability exists with a value that fails the requirement (1200 RPM max vs 1500 RPM required), or explicitly state it is not offered. Evidence exists and is negative. |
| **Unsupported requirement** | The requirement cannot be verified technically: commercial, legal, financial, warranty-type or too vague ("good quality"). Excluded from the score, listed separately. |
| **No evidence found** | Requirement is technical and verifiable, but the corpus is silent. Neither pass nor fail. Flagged for review and reported as coverage. |

**Key distinction:** absence of evidence is not evidence of absence. No evidence found never counts as Missing capability.

**Result fields:** every result carries three separate things: `status` (the business result above), `verification_outcome` (passed, failed, family\_level, conflicting, not\_checked) and `review_required` with a `review_reason`. Uncertainty is recorded in the last two, not forced into one status. A verified value below the requirement is a capability failure (Missing). A missing specification is never a capability failure. Evidence that covers a product family but is not confirmed for the specific model is uncertain applicability, not an automatic Partial: it sets `applicability` to family\_level, caps the score and flags review. A failed verification is never published as a confirmed match.

## 5. Functional requirements

### 5.1 Data collection (offline)

- FR-1. `collect.py` reads a config of `{manufacturer, url, doc_type, model_or_family}` entries and downloads files via HTTP.
- FR-2. Each download is recorded in `manifest.json`: source URL, retrieval date, SHA-256 hash, doc type, models/family covered, notes (e.g. "gated", "scanned", "manual download").
- FR-3. Manually obtained documents (JS sites, gated catalogues) can be added to the manifest with a note.
- FR-4. Collector respects robots.txt and site terms; restrictions are logged in the manifest, not bypassed.
- FR-5. Re-running the collector skips unchanged files (hash match) and flags changed ones for re-ingestion.
- FR-6. Adding a new category means adding a URL list; no code changes.
- FR-7. Distributor/reseller documents are marked secondary and never override manufacturer documents.

### 5.2 Document ingestion

- FR-8. Parse PDF/DOCX with PyMuPDF and pdfplumber, preserving page numbers and tables (tables kept as markdown).
- FR-9. If a page has no extractable text, fall back to OCR and tag the chunk `ocr=true` with lower confidence.
- FR-10. Chunks carry metadata: document ID, page, section heading, table flag, manufacturer, model/family, doc version/date.
- FR-11. Each chunk is indexed for keyword (PostgreSQL full-text search) and semantic search (pgvector embeddings).

### 5.3 Tender ingestion (large, mixed-format tenders)

- FR-12. Upload a tender PDF; record SHA-256, page count and processing version. Ingestion is idempotent: same hash plus same extractor/prompt/schema versions reuses stored results.
- FR-13. **Requirement atomizer:** split compound lines into atomic requirements ("SS316 body, IP68, 1500 RPM" becomes three), each linked to its source span. Table rows that are already atomic are not split again.
- FR-14. **Type classifier:** label each clause `technical`, `certification`, `commercial`, `eligibility`, `informational` or `unknown`. Only `technical` and `certification` go to matching; commercial and eligibility go to Unsupported with their source cited; `unknown` goes to review.
- FR-15. **Normalizer:** parse into `{attribute, operator, value, unit, range, mandatory}`. Handle min/max/≥/≤/range/tolerance words with `pint` for unit conversion.
- FR-16. Category is inferred per BOQ item, not once per tender.

### 5.3.1 Large-tender pipeline (100-300 pages)

A tender is never sent to the LLM as one prompt. It is inspected locally, segmented, and extracted in bounded batches, with every page accounted for.

- FR-T1. **Page triage (no LLM).** Tag each page with one or more of `text`, `table`, `scanned`, `boilerplate-candidate`; a page can carry several tags (prose plus table).
- FR-T2. **Page ledger.** Every page ends as `parsed`, `skipped` (with reason) or `ocr`. A fourth state, failed, covers extraction or OCR that did not complete and needs retry or review. A failed page never counts as processed: the tender may yield a partial report, but it is labelled incomplete ingestion while any page is unresolved. Page completeness is separate from requirement-extraction completeness (a parsed page can still hide a missed requirement). Extraction failures are never read as missing capabilities.
- FR-T3. **Selective OCR** only for pages with no or poor extractable text; OCR output is tagged `ocr=true` and lowers confidence.
- FR-T4. **Section segmentation** by headings and clause numbers (Technical Specs, BOQ, ATC, GTC, Annexures), with page boundaries as fallback.
- FR-T5. **No blanket boilerplate skipping.** Repeated GTC/boilerplate is deduplicated by hash but keeps its source references and is still clause-classified (FR-14), because buyer-added ATC and general conditions can carry mandatory certificates, warranty or eligibility terms. Content is excluded only when the exclusion policy explicitly allows it.
- FR-T6. **Table pipeline.** Extract tables with pdfplumber (PyMuPDF as comparison), detect headers, handle merged cells, stitch tables continued across pages by repeated headers. Each row becomes a candidate requirement carrying its column context (item no., parameter, value, unit) and a row ID.
- FR-T7. **Clause pipeline.** Chunk by clause; child bullets inherit the parent clause ("3.2 Materials: a) body SS316 b) trim 13Cr"). The LLM returns strict JSON validated by Pydantic, with source-span references.
- FR-T8. **Unified requirement record.** Both pipelines emit the same schema (below), then share one validation and atomization stage.
- FR-T9. **Item identity.** Assign a stable `item_id` per BOQ item (gate valve, pump, strainer) and carry it through extraction, retrieval, comparison and reporting. Requirements are matched only against models of their own item's category. Ambiguous item assignment is flagged, never guessed.
- FR-T10. **Cross-references.** Resolve internal annexures ("as per Annexure B") into the requirement set; treat cited standards (e.g. IS 1239) as certification requirements; flag unavailable external documents instead of dropping them.
- FR-T11. **Bounded batches and checkpoints.** Process section by section in small batches with retries; persist each completed batch so a failed run resumes instead of restarting.
- FR-T12. **Caching.** Cache keys combine document hash, extractor version and prompt/schema version.
- FR-T13. **Deduplication** of requirements repeated within one item or across pages, keeping all source references. Identical clauses attached to different BOQ items are never merged: share the provenance, but keep a separate item-specific assessment.
- FR-T14. **Recall safety net.** After extraction: map requirements back to source spans; scan technical text for requirement signals (shall, must, min, max, ≥, numbers with units, standard codes); subtract spans already covered; flag the remainder as `possible missed requirement` for review. This is a review trigger, not proof of a miss and not a completeness guarantee.
- FR-T15. **Progress states** shown in the UI (for example "parsing page 45/180").
- FR-T16. **Tool policy.** Start with PyMuPDF + pdfplumber + selective OCR. Add Docling only if it measurably improves extraction on the team's own tenders.

**Unified requirement record (illustrative):**

```python
class Requirement(BaseModel):
    requirement_id: str
    tender_id: str
    item_id: str | None
    source_page: int
    source_section: str | None
    parent_clause: str | None
    source_text: str
    source_span: tuple[int, int] | None   # offsets, or table row ID
    extraction_method: str                # table | prose | ocr
    requirement_type: str                 # technical | certification | commercial | eligibility | informational | unknown
    attribute: str | None
    operator: str | None
    value: str | None
    unit: str | None
    extraction_confidence: float | None
    review_required: bool
    review_reason: str | None
    parent_requirement_id: str | None     # set on atomized sub-requirements
    source_table_id: str | None
    source_row_id: str | None             # original page kept even if a table spans pages
    extraction_version: str
    schema_version: str
```

**Data model additions:**

| Table | Key fields |
| --- | --- |
| `tender_page` | tender\_id, page, tags, extraction\_method, quality\_warnings, ledger\_status, skip\_reason |
| `tender_table` | tender\_id, page\_range, headers, rows, continued\_from |
| `tender_item` | item\_id, tender\_id, name, category, boq\_ref |
| `extraction_batch` | tender\_id, section, status, attempts, extractor\_version, prompt\_version, output\_hash |

**Evaluation additions** (reported separately from status accuracy):

| Metric | Question it answers |
| --- | --- |
| Extraction precision | How many extracted requirements are genuine? |
| Extraction recall | How many genuine requirements were extracted? |
| Item-assignment accuracy | Is each requirement under the correct BOQ item? |
| Page-ledger completeness | Is every page parsed, skipped with reason, or OCRed? |
| Processing time and OCR rate | How long, and how many pages needed OCR, on long tenders? |

Label the genuine requirements on 2-3 tenders by hand to compute these; a wrong status on a never-extracted requirement is otherwise invisible.

**First step before building:** run a triage script over the 10-15 tenders to count text vs scanned pages, locate spec tables and see how each BOQ is laid out, then choose tools from that evidence. If time is short, cut cross-reference resolution and parallel batching first; keep triage, the table/prose split, item identity, clause classification and the recall check.

### 5.4 Retrieval

- FR-17. For each atomic requirement, filter by category, manufacturer and model, then run keyword and semantic search in parallel.
- FR-18. Merge and rerank candidates (reciprocal rank fusion, optional cross-encoder reranker).
- FR-19. Exact tokens (model numbers, standards like IS/ISO/API, grades like SS316/CF8M) get a keyword boost.
- FR-20. An alias table maps synonyms (SS316 / AISI 316 / CF8M / 1.4408); LLM-assisted mapping proposes new aliases with a confidence threshold, and low-confidence ones go to review.
- FR-21. The structured fact cache is checked first; retrieval and extraction run only for missing facts.

### 5.5 Evidence extraction and verification

- FR-22. LLM extracts a candidate fact `{attribute, value, unit, condition, model, page, quote}` from the retrieved passages. Output is strict JSON validated by Pydantic.
- FR-23. **Four-layer verifier:**
  1. **Citation validity:** the quote exists verbatim (whitespace-normalized) on the cited page.
  2. **Entity validity:** the passage refers to the manufacturer and the specific model/family being evaluated.
  3. **Semantic validity:** the passage actually supports the claimed attribute and value.
  4. **Comparison validity:** units, operator and conditions are consistent and convertible.
- FR-24. Any verification failure yields `needs_review` with the failure reason; the claim is never published as a match.
- FR-25. Conflicting facts across documents are detected; the newer document wins only if it applies to the same model/market, otherwise flag conflict for review.
- FR-26. Verified facts are saved with provenance (doc ID, hash, page, quote, extraction model/prompt version).

### 5.6 Comparison engine

- FR-27. A **comparison strategy registry** maps attribute types to evaluators: numeric (with direction), range, categorical/enum, boolean, certification/standard, material.
- FR-28. Evaluators are deterministic and return `{status, score, reason}`; no LLM decides pass/fail.
- FR-29. Unknown attribute or evaluator results in `needs_review` (store the fact, don't guess).
- FR-30. Compound requirements are scored by sub-requirement; any failing mandatory sub-requirement caps the result at Partial or Missing as configured.

### 5.7 Scoring and reporting

- FR-31. Per-requirement row: requirement, manufacturer capability, status, score, evidence (doc, page, quote), confidence, review flag.
- FR-32. Per-manufacturer summary: compatibility %, coverage %, ranking, status counts and lists.
- FR-33. Output: JSON (canonical) and HTML (bid-manager readable), plus a CSV export.
- FR-34. Every report records config: scoring policy version, model versions, prompt versions, corpus manifest hash.

### 5.8 Evaluation harness

- FR-35. `eval/` holds gold labels (hand-labelled requirement, manufacturer, expected status, expected page).
- FR-36. Harness computes status accuracy, citation correctness, coverage, review rate and a confusion matrix; run via one command.

## 6. Architecture

```
[manifest + collect.py] -> [local corpus]
                              |
   [Tender PDF] -> Tender parser -> Atomizer -> Type gate -> Normalizer
                              |                               |
               Doc parser (+OCR) -> chunks -> PostgreSQL (FTS + pgvector + facts)
                                                              |
   Requirement -> Hybrid retrieval -> Rerank -> LLM extract -> 4-layer verifier
                                                              |
                          Comparison registry -> Deterministic engine
                                                              |
                     Status + score + evidence -> Report (JSON/HTML) -> API -> React UI
```

### 6.1 Database decision: PostgreSQL + pgvector

| Option | Pros | Cons |
| --- | --- | --- |
| SQLite (FTS5 + embeddings table) | One file, no extra container, fits the "two images" rule, ample for a few thousand chunks | No built-in ANN index; brute-force cosine in numpy is fine at this scale |
| PostgreSQL + pgvector | Production scale, concurrent writes, native hybrid search | Extra container (official image, no custom build) and a compose file; heavier README. Accepted for native hybrid search and headroom |

**Decision:** PostgreSQL with pgvector for structured data, full-text search and vector search, behind a repository interface (`DocumentRepo`, `FactRepo`, `RunRepo`). The official pgvector/postgres image supplies the database, so only two custom images are published; the README documents the commands (or a compose file) that start all three containers. Original PDFs stay on disk with hashes and metadata in PostgreSQL. SQLite remains the fallback if setup friction proves too high.

### 6.2 Repository layout

```
backend/app/
  api/            # FastAPI routes (tenders, runs, reports, health)
  services/       # atomizer, normalizer, retrieval, extractor, verifier, scoring
  comparison/     # registry + evaluators (numeric, range, enum, bool, cert)
  repositories/   # PostgreSQL access (swappable)
  schemas/        # Pydantic models
  core/           # config, logging, errors
backend/data/manufacturers/<mfr>/{raw/, manifest.json}
frontend/         # React + Vite
eval/             # gold labels + harness
collect/          # collect.py + source configs
```

## 7. Data model

| Table | Key fields |
| --- | --- |
| `manufacturer` | id, name, official\_domain, notes |
| `product` | id, manufacturer\_id, model, family, category |
| `document` | id, manufacturer\_id, url, sha256, doc\_type, version\_date, retrieved\_at, path, is\_secondary, restriction\_note |
| `chunk` | id, document\_id, page, section, is\_table, ocr, text, embedding |
| `fact` | id, product\_id, attribute\_id, original\_name, value, unit, value\_type, operator, conditions, document\_id, page, quote, verification\_status, extractor\_version |
| `alias` | canonical\_term, alias, source (manual/llm), confidence, approved |
| `tender` | id, filename, category, uploaded\_at |
| `requirement` | id, tender\_id, item\_id, parent\_requirement\_id, source\_table\_id, source\_row\_id, source\_page, text, type, attribute, operator, value, unit, mandatory, extraction\_version, schema\_version, review\_reason |
| `run` | id, tender\_id, started\_at, status, config\_snapshot, corpus\_hash |
| `result` | id, run\_id, requirement\_id, manufacturer\_id, status, verification\_outcome, applicability, score, capability\_text, evidence\_ref, confidence, review\_required, review\_reason |

## 8. Scoring policy

**Per-requirement score:** Full = 1.0, Partial = graded 0.25-0.75 by closeness (default 0.5), Missing = 0, Unsupported = excluded, No evidence = excluded from compatibility and reported as coverage. Family-level evidence is scored at most 0.5 (configurable) and flagged for review.

**Compatibility %** = Σ(score × weight) / Σ(weight) over verifiable requirements that have evidence. Mandatory requirements get weight 2, optional 1 (configurable).

**Coverage %** = requirements with evidence / total verifiable requirements.

**Ranking:** sort by compatibility %, tie-break by coverage %, then fewer review flags. Always show compatibility and coverage side by side so a high score on thin evidence is visible.

**Mandatory failure rule:** if any mandatory requirement is Missing, the manufacturer is flagged "fails mandatory" regardless of score, and is never presented as bid-compliant however high its compatibility % is.

## 9. Confidence and review flag

A result is flagged for human review when any of these is true:

- Verifier layer 1-4 failed or was borderline.
- Retrieval score below threshold.
- Evidence came from OCR text.
- Evidence is family-level rather than model-level.
- Two documents conflict.
- Attribute or evaluator is unknown.
- Alias mapping confidence below threshold.
- Unit conversion was ambiguous.

Confidence is a composite (retrieval score, verifier pass count, source quality) bucketed into High / Medium / Low; the rule-based flags override it.

## 10. Hard cases and handling

| Case | Handling |
| --- | --- |
| Units, min/max/≥ direction | `pint` plus operator parser; direction-aware numeric evaluator |
| Several requirements on one line | Atomizer splits, sub-results aggregate to parent |
| Commercial/legal/vague | Type gate to Unsupported, listed separately |
| Catalogue silent | No evidence found, review flag, coverage metric |
| Scanned PDFs | OCR fallback, lower confidence, review flag |
| Table-heavy docs | Table-preserving chunks as markdown |
| Whole-range catalogues | Family-level evidence sets applicability to family\_level, caps the score and flags review; it is not scored as a confirmed Partial |
| Conflicting/outdated docs | Hash/version tracking, conflict detection, prefer newest applicable, else review |
| Prompt injection in documents | Document text treated as data only; strict JSON output; quote must verify |

## 11. API (FastAPI)

| Endpoint | Purpose |
| --- | --- |
| `POST /api/tenders` | Upload tender PDF, returns tender\_id |
| `GET /api/tenders/{id}/requirements` | Parsed atomic requirements |
| `POST /api/runs` | Start matching run (tender\_id, manufacturers, options) |
| `GET /api/runs/{id}` | Status and progress |
| `GET /api/runs/{id}/report` | Full JSON report |
| `GET /api/runs/{id}/report.html` | Readable report |
| `GET /api/runs/{id}/export.csv` | CSV export |
| `GET /api/manufacturers` | Corpus coverage and manifest summary |
| `GET /api/health` | Health check |

Runs execute via FastAPI `BackgroundTasks` with explicit states: `queued, parsing, retrieving, verifying, scoring, done, failed`. Errors return structured JSON with a code and message.

## 12. Frontend (React)

1. **Upload page:** drag-and-drop tender, choose manufacturers, Run.
2. **Progress view:** live run state.
3. **Report view:**
   - Ranking card with compatibility %, coverage % and how it is calculated.
   - Status counts with click-through lists.
   - Requirement table with filters (status, manufacturer, needs review).
   - Row drawer showing quote, document, page and a link to the source page.
4. **Past runs:** list and reopen reports (stretch).
5. **Review queue:** flagged items with reason (stretch).

## 13. Non-functional requirements

- **Reproducibility:** same tender plus same corpus plus same config yields the same deterministic results; LLM outputs cached by prompt hash.
- **Traceability:** every result links to a stored evidence record.
- **Performance:** a 100-requirement tender across 4 manufacturers completes in under about 10 minutes (target, to be measured).
- **Reliability:** retries with backoff on LLM calls, timeouts, graceful degradation to `needs_review` on failure, never a silent drop.
- **Security:** validate upload type and size, store files outside the web root, keep API keys in env vars, treat document text as untrusted.
- **Config:** all thresholds, weights, models and paths come from env/config, not code.

## 14. Deployment

- **Two public Docker Hub images:** `<user>/tender-backend` and `<user>/tender-frontend`.
- Backend ports 8000; frontend 3000 (or 80) with `VITE_API_URL` pointing to the backend.
- Backend env vars: `LLM_API_KEY`, `LLM_MODEL`, `EMBEDDING_MODEL`, `DATA_DIR`, `DATABASE_URL`, `CORS_ORIGINS`.
- The backend image bundles the frozen corpus (or mounts it via a volume); on first start it runs migrations and builds the index in PostgreSQL (official pgvector/pgvector:pg16 image started alongside).
- README includes exact `docker pull` / `docker run` commands, a shared network example, example output on one tender, and a one-command eval run.
- Code in a private GitHub repo with reviewers added as collaborators.

## 15. Testing and evaluation

- **Unit tests:** normalizer (units, operators), each evaluator, atomizer edge cases, verifier layers.
- **Integration tests:** tender in, report out on a small fixture corpus.
- **Gold set:** 50-100 hand-labelled requirement/manufacturer pairs across 3+ tenders, including hard cases.
- **Metrics:** status accuracy, per-status precision/recall, citation correctness (quote verified and page correct), coverage, review rate, false-confident rate (wrong status with no flag, the key one).
- **Ablations (nice to have):** with and without verifier, keyword-only vs hybrid.
- **Manual audit:** spot check 20 random cited pages against the PDFs.

## 16. Milestones (2 weeks)

| Days | Deliverable |
| --- | --- |
| 1-3 | Corpus collection for 3-4 manufacturers, manifest, ingestion with page and table chunks, OCR fallback |
| 4-5 | Tender parser, atomizer, type gate, normalizer (`pint`, operators) |
| 6-7 | Comparison registry and evaluators, scoring and report schema |
| 8-9 | Hybrid retrieval, LLM extraction, four-layer verifier, fact cache |
| 10-11 | FastAPI endpoints, React UI, HTML/JSON report |
| 12 | Gold labels and eval harness, run and fix |
| 13 | Docker images, push to Docker Hub, README, example output |
| 14 | Write-up (2 pages), walkthrough rehearsal, buffer |

**Cut order if behind:** past-runs UI, review queue, cross-encoder reranker, LLM alias mapping, ablations.

## 17. Risks

| Risk | Mitigation |
| --- | --- |
| Wrong or incomplete corpus | Per-requirement coverage report, manifest review, targeted manual collection |
| LLM hallucinated quotes | Verbatim quote check is a hard gate |
| Wrong-model evidence | Entity-validity layer, family-level cap at Partial |
| Attribute synonym mismatches | Alias table plus LLM mapping with threshold |
| Scope creep | Cut order above, "explain it in 2 minutes" rule |
| LLM cost/latency | Fact cache, prompt-hash cache, batch retrieval |
| Scanned docs misread | OCR flag, review flag, no confident match from OCR-only evidence |

## 18. Deliverables checklist

- [ ] Manufacturer corpus plus manifest (source, date, hash)
- [ ] Backend (modular FastAPI)
- [ ] Frontend (React)
- [ ] Two public Docker Hub images
- [ ] Private GitHub repo with collaborators added
- [ ] README with pull/run commands, env vars, ports, example output
- [ ] 2-page write-up (approach, assumptions, definitions, failure cases, next week)
- [ ] Test evidence and accuracy numbers
- [ ] 15-minute walkthrough prepared

## 19. Open questions

1. Which 3-4 manufacturers per tender category, and which exact models?
2. Which LLM and embedding models (cost, offline option)?
3. Partial score: fixed 0.5 or graded by closeness?
4. Should mandatory-failure disqualify a manufacturer from the ranking or only flag it?
5. Bundle the corpus in the backend image or mount it as a volume?
