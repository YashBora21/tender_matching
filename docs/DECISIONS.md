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
*Resolved:* migration 0009 fixes the column at `vector(384)` for the local
`BAAI/bge-small-en-v1.5` model and creates the HNSW index — see D-68.

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

---

**D-28 — The collector runs offline and the corpus is a file, not a table.**
*Decision:* `collect/collect.py` downloads only what a source config lists,
writes the files under `backend/data/manufacturers/<slug>/raw/` and records
each one in that manufacturer's `manifest.json`. The manifest is the hand-off
to the ingestion phase, which turns it into `document` rows.
*Why:* PRD §2 makes runtime scraping a non-goal: the corpus is collected
offline and frozen. A manifest that names the source URL, the SHA-256 and the
retrieval date is what makes a result reproducible and auditable, and it is
the artifact FR-34 records as the corpus hash.
*Alternatives:* Writing straight into the database from the collector —
rejected, it would make the corpus a function of a live database and lose
the file-level record of what was fetched. Running the collector as a service
— rejected, the PRD rules it out.

---

**D-29 — A restriction is recorded, never worked around.**
*Decision:* A `robots.txt` that disallows the path stops the fetch and the
document is recorded with status `restricted` and a `restriction_note`. A
`robots.txt` that cannot be read is *not* a restriction: a 5xx is a broken
site, not a policy, and the fetch proceeds with the failure recorded as a
note. A 401 or 403 on `robots.txt` is a policy and is treated as disallow-all.
*Why:* FR-4 says restrictions are logged, not bypassed. The wrong side of
this error is silent: refusing to fetch on a 500 would mean one bad day at a
host emptied the corpus with no error anywhere, and no document would be
missing from the report — it would just be absent.
*Alternatives:* Treating every unreadable `robots.txt` as disallow-all —
rejected, a transient 500 would empty the corpus. Ignoring `robots.txt`
entirely — rejected, that is the bypass FR-4 rules out.

---

**D-30 — A document type outside the vocabulary is not collected.**
*Decision:* An entry whose `doc_type` is not in `backend/data/doc_types.json`
is skipped and the run reports it as a failure.
*Why:* The ingestion phase maps a `doc_type` to a document class, and the
class decides the document's priority when two documents disagree. A
document with an unmapped type would land in the corpus as a document nobody
can classify, and the classification rules are exactly what a reviewer checks
when two documents disagree. The vocabulary is a data file so adding a type
is not a code change.
*Alternatives:* Collecting it and letting the ingestion phase deal with it —
rejected, the failure would surface much later and further from its cause.

---

**D-31 — A distributor document never overrides a manufacturer document.**
*Decision:* `Evidence` carries an `is_secondary` flag. In consolidation, the
sort key is `(class rank, secondary flag)`: a manufacturer's own document
outranks a distributor's *of the same class*, and a distributor's document
does not outrank a higher class. Two distributor documents that disagree are
still a conflict, not a silent winner.
*Why:* FR-7 says a distributor or reseller document is marked secondary and
never overrides a manufacturer document. The collector recorded the flag but
nothing read it, so the rule was not actually enforced anywhere: the
conflict resolver picked on document class alone, and a distributor's
datasheet would have won against the manufacturer's own datasheet.
*Alternatives:* Dropping distributor documents entirely — rejected, FR-7
marks them rather than excludes them, and they are legitimate evidence when
the manufacturer has said nothing. Giving secondary documents their own
priority class — rejected, it would make a distributor's datasheet outrank a
manufacturer's compliance schedule, which is the opposite of the rule.

---

**D-32 — pdfplumber reads the tables, PyMuPDF decides which pages have them.**
*Decision:* PyMuPDF's `find_tables()` gates the pages; pdfplumber extracts
the tables on the gated pages only.
*Why:* pdfplumber reads a ruled table's cells far better than PyMuPDF, which
drops the spaces inside a cell ("Maxoperatingpressure"). But pdfplumber spent
26 seconds on one brochure page hunting for lines that were not there, and
found nothing. PyMuPDF's finder is fast (~1s a page) and correct about
*whether* a table is there. The two together are faster and better than
either alone.
*Alternatives:* pdfplumber for everything — rejected, 26 seconds a page on
pages with no table, and the corpus is mostly pages without tables. PyMuPDF
for everything — rejected, it mangles cells.

---

**D-33 — A running header is found by position, not by comparison.**
*Decision:* A line is a header or footer when it sits in the top or bottom
band of the page and the same line (ignoring its digits) appears on most
pages. It is dropped from every page but the first.
*Why:* A manufacturer's header ("F-EDD-035_REV.0 Date 30.07.2020 Page 1 of 2
Technical Bulletin") repeats on every page and says nothing about the valve.
Comparing the *text* looked obvious and was wrong: the header carries the
page number, so it is never literally the same line, and the digit-stripped
comparison then also matched body lines that merely enumerated something.
Comparing the *text* to other pages also matched repeated content, which is
the one thing a corpus must not drop. Position is the signal: a header is at
the top, and a body line is not.
*Alternatives:* Dropping any line that repeats on most pages — rejected, it
drops repeated specifications, which are exactly what a corpus is for.
Dropping lines by font size — rejected, the header was set in a *larger*
size than the body in the bulletins that were measured.

---

**D-34 — A document on disk that no manifest lists is still a document.**
*Decision:* The ingester reads the manifest, and then also picks up every
corpus file the manifest does not list, hashing it and recording it with the
note "not in the manifest".
*Why:* The corpus is the set of files that exist. Leaving the unlisted ones
out would make the corpus quietly incomplete - and a manifest that lists 25
of 59 files is a manifest that is behind, not a corpus that is 25 files.
*Alternatives:* Only ingesting what the manifest lists — rejected, a document
the reviewer can see in the folder but the system cannot see is a gap that
shows up as a missing capability in the report, which is the one thing the
system must never do.

---

**D-35 — The attribute name is resolved through the alias table.**
*Decision:* The LLM extracts the attribute name; the alias table maps it to a
canonical term; the canonical term's value type selects the evaluator. A
name that does not resolve goes to review.
*Why:* FR-20 wants an editable alias table and FR-27 wants the evaluator
chosen by value type, and the two are the same decision: what makes an
attribute comparable is the *shape of its value*, not the product category.
Resolving through a table means a new attribute is a row, not a code change,
and an unknown attribute is a review rather than a guess (FR-29).
*Alternatives:* Keeping the fixed vocabulary as the only mapping — rejected,
it is what made the vocabulary a hardcoded valve-and-pump list. Choosing the
evaluator from the requirement's wording — rejected, the same requirement can
be stated in many ways and the evaluator must not depend on the phrasing.

---

**D-36 — One vocabulary of requirement words, one of boolean words.**
*Decision:* The wording that marks a clause as carrying its own
specification, and the words that state a yes/no, live in
`attribute_vocabulary.json` and are read by the atomizer, the normalizer, the
evaluators and the document parser.
*Why:* There were four copies of the requirement words and three of the
boolean words, and they did not agree: a word could be mandatory to the
atomizer and informative to the normalizer, and truthy to the evaluator and
falsy to the normalizer. A duplicated word list is a list that will drift.
*Alternatives:* Leaving each module with its own list — rejected, that is how
they came to disagree.

---

**D-37 — The document-type vocabulary is one file.**
*Decision:* The document classes, the filename markers that give a document
its class, the file extensions and the extension-to-class defaults live in
`backend/data/doc_types.json`, and the collector and the evidence layer both
read it.
*Why:* There were three document-type vocabularies that did not agree:
`doc_types.json`, the `DocumentClass` enum and the filename marker list. A
manifest entry with `doc_type: "catalogue"` was collected by one and could
not be classified by the other, so a collected document could have no class
at all.
*Alternatives:* Deriving the enum from the file — rejected, the class is a
state the code branches on and a StrEnum is the honest expression of that.
Keeping the markers in Python — rejected, that is the copy that drifted.

---

**D-38 — A tender is fetched through a fence.**
*Decision:* `app/core/fetching.py` fetches a document only when the host is on
`FETCH_ALLOWED_DOMAINS`, the host does not resolve to a private address, the
scheme is http or https, the size is within `FETCH_MAX_BYTES` and the time is
within `FETCH_TIMEOUT_SECONDS`. A redirect is refused, not followed.
*Why:* A tender PDF is a document that says where its requirements are, and
the collector follows what it says. That makes the collector a fetcher of
URLs it did not choose, so the URLs it will follow have to be bounded. The
private-address check is the one that matters most: "http://127.0.0.1/" and
"http://169.254.169.254/" are documents too, and a link in a document is not
trusted. A redirect is a fresh URL and gets a fresh decision, so the
simplest way to hold that line is not to follow one.
*Alternatives:* Checking the host against the allowlist only — rejected, an
allowlisted host can still resolve to a private address, and a name can say
anything. Following redirects and checking each hop — rejected, it is more
code for the same guarantee: refusing the redirect is a guarantee too.

---

**D-39 — A document that cannot be fetched is failed, and can be uploaded.**
*Decision:* A failed fetch is recorded in the manifest with the reason, and
`POST /api/tenders/{id}/attachments` accepts the file instead. An upload
replaces the failed record for the same URL.
*Why:* "The tender has no requirements" and "the tender's requirements could
not be fetched" are different facts, and only one of them is a fact about the
tender. Collapsing them turns a broken link into a missing capability, which
is the one thing the system must never report. Replacing rather than adding
keeps the report's list of attachments to one row per document.
*Alternatives:* Marking the tender incomplete and moving on — rejected, the
reviewer already has the file and the system should be able to take it. A
second record for the upload — rejected, the report would list the same
document twice.

---

**D-40 — A frozen attachment is the cache.**
*Decision:* A document already on disk with the hash the manifest records is
not fetched again. A changed document is fetched, and its previous hash is
kept.
*Why:* The frozen copy and its hash are the same thing: if the file on disk
still hashes to what the manifest says, the document has not changed, and
fetching it again costs a round trip to learn nothing. This is the same rule
the corpus collector follows (FR-5).
*Alternatives:* Always fetching and comparing — rejected, it spends a request
on every document on every run to learn what the file's hash already says.

---

**D-41 — The LLM is reached over an OpenAI-compatible API.**
*Decision:* The provider is a base URL, not a library: `LLM_BASE_URL` and
`EMBEDDING_BASE_URL`. The client is `app/services/llm.py`.
*Why:* The LLM and the embedding model are served by different providers
today — Groq for the LLM, a local Ollama for the embedding model — and the
HuggingFace router can serve either once the account has credits. A library
per provider would be three clients; one OpenAI-compatible client is one.
*Alternatives:* A library per provider — rejected, it is more code for the
same calls, and the providers are converging on the same API shape.

---

**D-42 — gpt-oss is a reasoning model, and the answer is not the reasoning.**
*Decision:* The client reads `message.content` and keeps `message.reasoning`
apart, and asks for at least 512 tokens.
*Why:* gpt-oss spends its tokens on reasoning first. A 32-token budget
returned an empty answer with 30 reasoning tokens spent: the model had not
reached the answer yet. The reasoning is not a requirement and is never
treated as one.
*Alternatives:* Reading the reasoning when the content is empty — rejected,
the reasoning is the model thinking aloud, not a statement the tender made.

---

**D-43 — The English half of a line is the requirement.**
*Decision:* `english_half()` takes the text after the last run of Devanagari.
A line whose characters no font could have produced is flagged as mangled
and counted, not read as an absent requirement.
*Why:* The tenders are bilingual, and the Devanagari is sometimes mangled by
the PDF's encoding ("बड ववरण/Bid Details"). Reading the Devanagari would
give the model noise it cannot read, and a mangled line that was counted as
"no requirement" would look exactly like a tender that did not ask for
anything.
*Alternatives:* Reading the whole line — rejected, the Devanagari half is
noise for the model and a source of false readings. Dropping a mangled line
silently — rejected, a line that cannot be read is a gap, and a gap has to
be visible.

---

**D-44 — The pipeline is keyed by what it read, not by what it is.**
*Decision:* Every stage has a key: a hash of the inputs it read and the key of
the stage before it. A stage whose key has not changed is not run, and
neither are the stages after it. The checkpoints live in `extraction_batch`,
which the PRD gives for exactly this.
*Why:* A corpus is re-read often and a tender is rarely different. Without a
key, every run re-downloads and re-extracts everything, which costs money
and wears out the provider's rate limit. The key makes the run incremental by
construction rather than by a flag someone has to remember to set.
*Alternatives:* A "skip if the file hash matches" rule per stage — rejected,
it cannot express "the items changed but the specification did not", which is
the common case. A run id compared to the last run — rejected, it says
whether *something* changed, not *what*.

---

**D-45 — The item is identified by its tender and its number within it.**
*Decision:* `tender_item`'s key is `(tender_id, item_id)`, and a requirement
points at that pair.
*Why:* Two tenders both have an item "1". A single-column key either
collides or has to be a concatenation, and a concatenation hides the fact
that the two things are different. The PRD's table list has `tender_item`
with `item_id` and `tender_id` as separate columns, and the requirement
table has both, so the pair is what the PRD's own shape means.
*Alternatives:* A concatenated string key ("tender:1") — rejected, it is the
pair wearing a disguise, and the database cannot enforce it.

---

**D-46 — An LLM answer is cached by its input and by everything that read
it.**
*Decision:* The cache key is the hash of the input plus the model, the prompt
version and the schema version. Every stored requirement records all four.
*Why:* A cached answer is only an answer to the question that was asked. If
the prompt changes, the same question is a different question, and reusing
the old answer would put a requirement in the report that no prompt ever
produced. The PRD says it twice: the cache key is "document hash, extractor
version and prompt/schema version" (FR-12) and every report records the
model, prompt and schema versions (FR-34).
*Alternatives:* Caching on the input alone — rejected, a prompt change
would be invisible. Caching on the tender — rejected, the same line in two
tenders would be read twice.

---

**D-47 — A scanned page is one whose images cover it.**
*Decision:* A document is scanned when its images cover more than half of a
page. The amount of text on the page is not the test.
*Why:* A scanner writes whatever the page had on it — a file number, a date,
a name — so a scanned page often carries a couple of hundred characters. The
tender corpus proved it: a scanned technical specification with 214
characters on every page was classified as a readable specification, read as
if it had a table, and produced no requirements at all.
*Alternatives:* The amount of text — rejected, as above. The file name —
rejected, the name says where the file came from, not what is in it.

---

**D-48 — One resolution of the roles, shared by the stages.**
*Decision:* `resolve_roles` decides which attachment is the bill of
quantities and which is the specification, once, and both stages use it.
*Why:* The two stages read different files and must agree on what is what.
Resolving the roles twice let a scanned document be the bill of quantities
in one stage and nothing in the other, and the tender was then reported as
having no items when it had them.
*Alternatives:* Each stage looking for what it needs — rejected, that is how
the two stages came to disagree.

---

**D-49 — A failed tender is reported, and the audit carries on.**
*Decision:* Each tender runs in a savepoint, so its failure is rolled back
alone, and the audit reports the failure and moves to the next tender.
*Why:* A tender that fails aborts the transaction, and every tender after it
is then reported as broken too. One unreadable PDF turned 24 tenders into 24
failures, and the report said nothing about the real cause.
*Alternatives:* One transaction for the whole corpus — rejected, a single
bad tender costs the whole run. A transaction per tender without a
savepoint — rejected, the abort still poisons the session.

---

**D-50 — The two searches are merged by rank, not by score.**
*Decision:* Reciprocal rank fusion: a chunk's score is the sum of what each
search gives it for its position. The keyword search is OR over the words,
with the common words left out; the exact tokens are boosted.
*Why:* A requirement names several things at once - "Body shall be SS316
with pressure rating 16 bar" - and no datasheet sentence has all of them,
so a search that ANDs the words finds nothing at all. And the two searches'
scores are not comparable: a full-text rank and a cosine distance are
different scales, and a fixed weighting between them would be a guess.
Only the order is used, so nothing has to be calibrated.
*Alternatives:* One search only — rejected, the PRD asks for both, and
keyword alone misses "Face to Face: ASME B16.10" for a requirement that
says "face to face distance". A weighted sum of the raw scores — rejected,
the scales are not comparable and the weights would be a guess.

---

**D-51 — The exact tokens are worth more than the common words.**
*Decision:* The words that identify a product - a model number, a standard
code, a material grade, a size - are boosted over the full-text rank. A
token is looked for in three forms: as it stands ("SS316"), with its parts
joined ("150mm"), and, when it carries a number, the number alone ("150").
*Why:* A requirement that names "SS316" wants the datasheet that says
"SS316", not the many that say "stainless". The full-text rank cannot tell
them apart, because "stainless" is the common word and "ss316" is the one
that identifies the product. The number alone matters because a datasheet
table writes the value in the cell and the unit in the column header, so
"267 mm" never appears in the text and "267" does.
*Alternatives:* Ranking by the full-text score alone — rejected, as above.
Matching the joined form only — rejected, a corpus writes both ways.

---

**D-52 — The embedding model is the local one.**
*Superseded by D-68: the model is still local, but it is loaded through
`sentence-transformers` rather than served by Ollama.*
*Decision:* The corpus is embedded with the local Ollama's
`nomic-embed-text` (768 dimensions), which is served without an account.
*Why:* The HuggingFace router needs a token *and* credits: the account
answers 402, and the anonymous path answers with a web page rather than
JSON. The local model needs neither, and it is the same shape of service
(the embedding is a vector, the dimension is a property of the model).
*Alternatives:* The HuggingFace model — rejected, it cannot be reached
without credits. A hosted embedding provider — rejected, it needs an
account and an API key for a computation the local machine does for free.

---

**D-53 — A rate-limited call waits as long as the provider says.**
*Decision:* The retry delay is read from the error when the provider gives
one ("Please try again in 24.9s"), and the fixed backoff stands when it
does not.
*Why:* The provider's limit is per minute and it knows when the limit
resets. A fixed five second backoff retried into an exhausted limit, and
every retry was another call against the same limit.
*Alternatives:* A longer fixed backoff — rejected, the wait is then either
too long for a fast limit or too short for a slow one. Retrying without a
wait — rejected, that is what made it fail.

---

**D-54 — The ingestion closes its transaction before it reads a document.**
*Decision:* Each document is a unit of work on its own: the transaction is
closed before the document is parsed, and the document is committed as soon
as it is written.
*Why:* A big catalogue takes minutes to read, and the server kills a
connection that sits in a transaction that long. The first run over the
new manufacturers died on the last document with
`IdleInTransactionSessionTimeout`, after half an hour of parsing that
would have been thrown away.
*Alternatives:* Reading every document first and writing them all at the
end — rejected, the whole corpus is then held in memory and one timeout
loses all of it. Parsing inside the transaction — rejected, that is the
defect.

---

**D-55 — A NAT64 address is judged by the IPv4 it carries.**
*Decision:* An IPv6 address in the well-known NAT64 prefix
(`64:ff9b::/96`) is reduced to the IPv4 address in its last 32 bits, and
that address is checked against the private-address rules. The NAT64
prefix itself is not treated as private.
*Why:* On an IPv4-only network with DNS64, a public name legitimately
resolves into that prefix. Python 3.13 classes the prefix as reserved, so
the fence refused `gem.gov.in` even though the IPv4 inside was public.
Refusing the prefix would refuse every document on such a network.
*Alternatives:* Dropping the reserved check — rejected, it also covers
addresses that must stay refused. Allowing every NAT64 address — rejected,
`64:ff9b::c0a8:101` carries `192.168.1.1` and must not pass.

---

**D-56 — Ingesting the corpus seeds the products in the same run.**
*Decision:* `make ingest` seeds the canonical attributes and the aliases,
ingests the documents, then seeds the product each document is about and
removes the family-level products nothing points at any more.
*Why:* A document without a product is a passage that names no product:
retrieval cannot say what the evidence is for and the fact cache can never
hit. The seeding had no caller at all, so `document.product_id` stayed
null on a fresh database. Keeping it as a separate command makes it a step
someone has to remember, and forgetting it fails quietly.
*Alternatives:* A `make seed` target — rejected, the step that must follow
ingestion is not a separate job. Seeding during the migration — rejected,
a migration cannot read the corpus and must stay reversible.

---

**D-57 — A relative `DATA_DIR` is resolved against the repository root.**
*Decision:* When settings load, a data directory that is not absolute is
joined to the repository root; an absolute one is used as given.
*Why:* `.env` says `DATA_DIR=backend/data`. Read as a plain relative path
it depends on the working directory: the suite run from `backend/` looked
in `backend/backend/data`, found no corpus, and the three tests that inspect
the corpus skipped themselves instead of running. A check that skips is a
check that is not there, and the same fault would make a deployed ingestion
report zero documents instead of failing.
*Alternatives:* Writing an absolute path into `.env` — rejected, it names
one machine's checkout and breaks on every other one. Requiring the process
to start at the repository root — rejected, that is the defect, and a path
should not mean two things depending on where it was started.


---

**D-58 — The category is inferred per bill-of-quantities item, from a data file.**
*Decision:* The words of an item decide its category, not the tender's own
category and not one category for the whole tender. The vocabulary is
`backend/data/categories.json`; the most specific matching term wins, a
trailing plural is the same term, and an item that names no category — or
two categories equally specifically — is `unknown` and flagged for review.
*Why:* FR-16 and FR-T9: a requirement is only ever compared against models in
the category of the item it belongs to, and one tender carries a gate valve, a
pump and a box of spares. Copying the tender's category onto every item
compares a pump requirement with a valve catalogue; asking the LLM is the one
thing the architecture forbids; and picking one of two equally-named
categories is a guess the verifier cannot catch.
*Alternatives:* The tender's category, kept as metadata only — rejected, it is
the coarse hint the PRD warns about. A category list in code — rejected, FR-6
says adding a category is adding data. Stemming every word — rejected, it is
more machinery than an optional plural, and it misfires on words like "gas".

---

**D-59 — A document's `category` is its product category, not its manufacturer.**
*Decision:* The collector's source configs and the manufacturer manifests
declare `valves` or `pumps`, the same vocabulary a tender item is classified
in. The manufacturer is already a column; its slug is not a category.
*Why:* The retrieval filter compares a document's category with the item's.
While the manifests wrote the manufacturer's slug there, every item either
matched nothing or everything, and the filter could only be wrong.
*Alternatives:* A separate `product_category` column — rejected, `category`
already means exactly this and a second name for one fact invites drift.

---

**D-60 — Retrieval excludes only documents that declare a different known category.**
*Decision:* A search in category C adds `category IS NULL OR category NOT IN
(every known category except C)`. A document that declares nothing, or
declares a value that is not a category at all, is kept.
*Why:* Equality alone drops every document whose category is null or was
written by an older manifest, so the corpus goes silently empty and every
requirement comes back *no evidence found*. Absence of a category is not a
statement that the document is about something else (PRD 4). The filter now
fails open on metadata the system cannot interpret, and the run flags items
whose own category is unknown.
*Alternatives:* Backfilling the column and filtering on equality — rejected,
it makes an old corpus a second-class corpus and does not help a deployment
that has not re-ingested. Failing closed — rejected, it turns a metadata gap
into a missing capability.

---

**D-61 — Re-ingestion refreshes the metadata of an unchanged document.**
*Decision:* When a document's hash is already stored, its text is not parsed
again but `category`, `doc_type`, `path`, `url` and `model_or_family` are
brought up to date from the manifest, and the count is reported as
`metadata_refreshed`.
*Why:* The manifest is edited after collection — a category corrected, a model
named more precisely — and those are exactly the fields retrieval filters on.
Skipping the document entirely left the correction invisible, which is the
same failure as D-60 from the other side.
*Alternatives:* Re-parsing every document whose metadata changed — rejected,
minutes of parsing to change no chunk. A manual SQL update — rejected, the
next ingestion would leave it stale again.

---

**D-62 — Run states are `queued → retrieving → scoring → done | failed`.**
*Decision:* A run moves through these states, and `failed` carries the
failure's message in `run.error`. `parsing` and `verifying` are not used:
the tender is parsed before a run exists, and verification happens inside each
requirement's match, so there is no separate pass to show.
*Why:* The PRD lists the states but a state that is never entered is a state a
polling client waits for forever. Two states that would each last one function
call add nothing a caller can act on. The failure message is part of the
state: `failed` without a reason is a run someone has to guess about.
*Alternatives:* Entering every PRD state for completeness — rejected, it
advertises progress that is not observable. Storing the exception on the
result rows — rejected, the run failed before those rows exist.

---

**D-63 — A report is built from stored rows and the run's own config snapshot.**
*Decision:* `build_report` reads the run, the tender, its page ledger and the
stored results, and takes the scoring policy, model and prompt versions from
`run.config_snapshot`. The JSON report is canonical; HTML and CSV are renderings
of the same dict. The report endpoints serve it only when the run is `done` or
`failed`; a live run is `409` with its progress in the error detail. CSV cells
that begin `=`, `+`, `-` or `@` are prefixed with `'`, and every document-derived
value is HTML-escaped.
*Why:* FR-34: a report that does not name the rules, models and corpus it was
produced with cannot be compared with another one, and re-reading the settings
could describe a run with rules that did not exist when it ran. Rendering from
one dict means the three outputs cannot disagree. A report of a live run shows
half the requirements, which reads as manufacturers that answered nothing. A
tender is untrusted data: a quote is shown as text and never executed as a
spreadsheet formula or as markup (PRD 10).
*Alternatives:* A report recomputed from current settings — rejected, it is a
different report. Serving a partial report with a banner — rejected for the
JSON and CSV endpoints, where no banner exists; the stored results are still
reachable through `GET /api/runs/{id}` while it works.

---

**D-64 — An uploaded tender is frozen first, read in the background, and its
own state is stored.**
*Decision:* `POST /api/tenders` writes the file under
`DATA_DIR/tenders/<tender_id>/` with a manifest in the same shape
`make tenders` produces, finds or creates the tender by its sha256, answers
`202` with the id and status, and schedules `ingest` in the background.
`ingest` moves `tender.status` to `processing`, runs collection and the
pipeline, and finishes on `done` or `failed` with the reason in `tender.error`.
`GET /api/tenders/{id}/requirements` answers `409` while the tender is `queued`
or `processing`. A duplicate upload is the same tender (FR-12); it is read
again only when the earlier read failed, because a `queued` or `processing`
tender already has a reader.
*Why:* Reading a 200-page tender is minutes of parsing and extraction, so
holding the upload request open would turn a slow tender into a timeout that
looks like a rejected file. Freezing before parsing means a crash cannot lose
the bytes the caller sent, and the manifest keeps an uploaded tender
indistinguishable from a collected one to runs and reports. The status is not
decoration: without it, "no requirements" covers both a document that states
none and a document nobody has read — the absence-of-evidence error PRD 4
forbids. A failed read is recorded with its reason, never as zero requirements.
*Alternatives:* Parsing inside the request — rejected, it times out on exactly
the tenders the feature exists for. A temporary directory followed by a copy —
rejected, two homes for one file with no benefit. A separate ingestion table —
rejected, one lifecycle per tender belongs on the tender row. Returning an
empty list while reading — rejected, it asserts a fact nobody has established.

---

**D-65 — The frontend renders the report it is given, and decides nothing
itself.**
*Decision:* `frontend/` is a Vite + React + TypeScript app with two views, no
router and no UI kit: upload (drop a PDF, poll the tender until `done` or
`failed`, choose manufacturers, start a run) and run/report (poll the run,
fetch the JSON report once the run is terminal, then rank, filter and open
evidence from that payload). Every status, score, count and explanation on
screen comes from the report: labels are vocabulary words, filters select rows,
and nothing recomputes a compatibility, a coverage or a status. The report is
requested only after the API says the run has stopped. The evidence drawer
links to `document.url`, which `RunRepo.results` now returns alongside the
quote and page; when a document has no URL the drawer says so rather than
inventing one, and the page number is shown as text, not appended to the link
as a fragment.
*Why:* The report is built from stored rows and the run's own config snapshot
(D-63); a client that recomputed a ranking from the same rows would be a second
implementation of the scoring rules and could disagree with the HTML and CSV
exports of the same run. Showing only stored values keeps every claim tied to
its verified quote. Linking to a stored document URL is honest; a `#page=12`
fragment is not, because the viewer may ignore it and the link would then claim
a location nobody checked. A partial or incompletely ingested report is
labelled with the API's own numbers instead of being hidden or read as a
missing capability (PRD 4).
*Alternatives:* A client-side summary recomputed from the result rows —
rejected, two scoring implementations and no way to tell which one is right.
react-router — rejected, two screens and a run id are not a route tree. A UI
kit — rejected, nothing here needs one and it would add a dependency for
tables and buttons. Fetching the report while the run is live and refreshing
it — rejected, a half-finished run reads as manufacturers that answered
nothing. Deep-linking to the quoted page — rejected, an unverified jump reads
as a verified citation. A past-runs screen — deferred, not rejected: it needs
a `GET /api/runs` list the API does not have yet.

---

**D-66 — The evaluation harness scores a stored report offline, and refuses
what it cannot verify.**
*Decision:* `eval/harness.py` joins a report JSON (`make report … FORMAT=json`)
with a JSONL gold file on `(tender sha256, requirement_id, manufacturer)` and
computes status accuracy, the confusion matrix, per-status precision/recall/F1,
the false-confident rate, coverage, review rate and citation correctness. It
runs with no database, API or model. A quote counts as verified only if
`app.services.verifier.check_citation` finds it on the cited page; the corpus
is indexed from the manifests, and a document that is not on disk is reported
as unavailable and excluded from the citation denominator. A label whose pair
is absent from the report stops the run and lists the keys; a prediction with
no label is reported as unlabelled, never as correct. A `full_match`,
`partial_match` or `missing_capability` label must carry its document, page and
quote; a `no_evidence_found` or `unsupported_requirement` label must carry a
note saying where the labeller looked or why it is not verifiable. Unsupported
clauses are excluded from the metrics and reported apart.
*Why:* A measurement that needs a live stack is neither reproducible nor
runnable in review, and an evaluation that re-ran the pipeline could disagree
with the report a bid manager actually saw. Grading citations with the same
function that gates publication means the evaluation cannot be more forgiving
than the product. Counting a missing prediction as correct, or dropping it,
would inflate accuracy in exactly the direction that hides a broken run. A
label without a quote is an opinion, and "the corpus is silent" is a claim that
has to say where it looked.
*Alternatives:* Recomputing the predictions inside the harness — rejected, it
needs a database and an LLM and measures a different report. Trusting the
report's own `verification_outcome` as the citation result — rejected, that is
grading the system with its own homework. Counting missing pairs as passes —
rejected, it rewards a run that stored nothing. Shipping hand-written labels
without a model run — rejected: it would be inventing the evidence the labels
are supposed to check, which this project does not do.

---

**D-67 — Two images, and the API address is baked into the frontend at build
time.**
*Decision:* PRD 14 ships as two images: `backend/Dockerfile` (Python, runs
`alembic upgrade head` then uvicorn) and `frontend/Dockerfile` (Node build,
nginx serves `dist` on port 80). The frontend takes `VITE_API_URL` as a **build
argument**, and the compose stack publishes it on 3000; `nginx.conf` serves
`index.html` for unknown paths so client-side routes survive a refresh, and
answers `/healthz` for the healthcheck. `docker-compose.yml` gains the frontend
service behind a healthy backend, and `backend/.dockerignore` keeps the corpus,
tests and virtualenv out of the build context. The README carries the exact
build, pull and shared-network `docker run` commands, and says plainly that a
pull line only works after someone pushes the image, because nothing is
published from this repository.
*Why:* Vite substitutes `import.meta.env.VITE_API_URL` when it builds, so a
runtime environment variable on the nginx container would change nothing; the
address has to be the one the *browser* uses, which is why `http://backend:8000`
is wrong even though the backend can reach it. Rebuilding the frontend when the
address changes is the honest cost of a static bundle, and stating it prevents a
stack that silently calls the wrong host. Shipping the corpus in the backend
build context would add hundreds of megabytes to every build and still leave a
reviewer without a corpus, so it stays a mount.
*Alternatives:* A Node server proxying `/api` to the backend at runtime —
rejected for now: it adds a process and a runtime config file to solve a problem
that a rebuild solves. Baking the API URL into the image and calling it
"configurable" — rejected, it is only configurable at build time. Publishing the
images from here — not possible without credentials, and a README command for an
image that does not exist would be a lie.

---

**D-68 — Embeddings are computed locally by `sentence-transformers`.**
*Decision:* `EMBEDDING_BACKEND=sentence-transformers` is the default. The
backend loads `BAAI/bge-small-en-v1.5` in its own process and encodes with
`normalize_embeddings=True`; the vectors are 384-dimensional. `embed()` keeps an
`http` backend for an OpenAI-compatible endpoint (a local Ollama is one).
Migration `0009_embedding_384` drops the old HNSW index, clears the 768-dim
vectors, changes `chunk.embedding` to `vector(384)` and rebuilds the index;
`make embed --force` re-embeds the corpus. This supersedes D-52.
*Why:* The HuggingFace token authenticates (whoami answers 200, fine-grained,
`isPro:false`) but the account has no inference credits: `pipeline/feature-extraction`
answers 402, and the OpenAI-shaped `/v1/embeddings` route is 404, so HF is not
usable without a purchase. Public sentence-transformers weights need no account
and no credits, and the library removes the second service that Ollama required.
bge is trained for cosine similarity, so normalising makes the operator's
assumption explicit rather than implied. Batches are filled shortest-first:
the model pads every text to the longest in its batch, and on this corpus that
alone moved the run from 2.8 to 15.8 chunks/s (20892 chunks: 124 -> 22
minutes), while the vectors are unchanged because encode is per text.
*Alternatives:* HuggingFace Inference Providers — rejected, 402 with no credits.
Keeping Ollama as the default — rejected, it works but needs a daemon running and
the library is the same computation in-process. Reusing the 768-dim
`nomic-embed-text` vectors — rejected: vectors from two models are not
comparable, so the migration clears them and the corpus is re-embedded.

---

**D-69 — A stage that stopped at the provider's limit is recorded as
`rate_limited`, with the reason, at all three levels (defects D8 and D9).**
*Decision:* `TenderStatus.RATE_LIMITED` is a fifth tender state, next to
`queued`, `processing`, `done` and `failed`. `audit_tender` writes it on the
tender row when the extraction stage stopped at the limit, `tenderupload._read`
returns it to the upload route instead of `done`, the corpus report exports
`rate_limited_detail`, and `extraction_batch` gains a `detail` column
(migration `0010_stage_detail`) that `save_stage` fills from the stage's own
reason. A run already had `RunStatus.RATE_LIMITED` from D5; this closes the same
hole for the read.
*Why:* Before this the run said `rate_limited` and the tender said `done`, with
the provider's message only in a log line, so the next command — a report, an
export, a resumed run — could not tell a tender that was read in full from one
whose reading stopped after eleven items. That is the absence-of-evidence trap
(AGENTS #3, #4) at the level of the whole tender: a stage that did not finish is
not a stage that found nothing. `done` also made the upload route refuse to
schedule a re-read, because `done` never is rescheduled; the new state is
resumable by construction. The detail column exists because "rate limited" alone
does not say for how long, and the provider does say ("try again in 24.9s").
*Alternatives:* Reusing `failed` — rejected: a limit is not a failure of the
tender, and the requirements already read would be labelled as a failed read.
Leaving the state at `done` and reporting the limit only in the run — rejected,
it is the read that was cut short. Storing the retry hint only in `tender.error`
— rejected, the stage is what a resumed run reads, and the row it reads is
`extraction_batch`.

---

**D-70 — Logs go to stderr (defect D7).**
*Decision:* `configure_logging` attaches its handler to `sys.stderr`, not
`sys.stdout`. `backend/tests/test_logging.py` asserts the stream.
*Why:* `corpus.py --json` and `ingestion.py` print their result to stdout for a
caller to parse; a log record interleaved with that JSON makes the output
unparseable, and the failure looks like corrupt data rather than a log line.
stdout stays the program's result, stderr stays the operator's record.
*Alternatives:* Logging to a file — rejected, it hides the record from the
operator running the command. Filtering the JSON through a tolerant parser —
rejected, it moves the work to every caller.

---

**D-71 — No test drops `public`, and no test reads the whole corpus (defect
D11).**
*Decision:* `test_migrations.py` creates a schema of its own
(`migration_test_<hex>`), points alembic at a connection whose `search_path`
puts that schema first, asserts on it, and drops only that schema. The
ingestion integration tests scope every read to the rows they wrote (by
document id, hash or name) instead of counting the whole table.
*Why:* As written, the migration test ran `DROP SCHEMA public CASCADE` against
whatever `TEST_DATABASE_URL` named, and the ingestion tests asserted
`count(*) == 2` on `chunk`. Together they deleted a corpus of 9 manufacturers,
70 documents and 20,892 vectors from the run database while the run was being
prepared — the tests passed and the evidence a report would be built from was
gone. A test that needs an empty database has to make one, not take one.
*Alternatives:* A container per test session — better, and heavier than this
phase needs; the isolated schema gives the same guarantee for the tables alembic
creates. Running the tests only against a throwaway database — rejected, the
defect is that the test *can* be pointed at a real one, and a mistake in an
environment variable is exactly how it happened.

---

**D-72 — The embedding model stays `bge-small-en-v1.5` at 384 dimensions
(English corpus); BGE-M3 is measured and deferred.**
*Decision:* No change to D-68. `BAAI/bge-m3` (multilingual, 1024 dimensions,
MIT, 2.27 GB) is not adopted now. Hindi work stays as it is: a line whose
script the English half cannot represent is kept and flagged unreadable, never
matched (D-6, D-63).
*Why:* Measured on this machine, on the same 128 corpus texts: BGE-M3 encodes
2.08 chunks/s at batch 16, `bge-small-en-v1.5` 28.2 chunks/s at batch 32. On the
real chunk distribution (mean 216 characters, p90 446, sampled through
`parse_document`) BGE-M3 managed 7.0 chunks/s against bge-small's 147.0 on the
shortest deciles, i.e. roughly a 20x gap in the same direction, plus 90 s of
model load and a re-embedding of the whole corpus. The corpus contains no
Devanagari: 81 PDFs were scanned and none carries a single Devanagari
codepoint, so the bilingual model would buy nothing today. Adopting it would
also mean a new 1024-dimension migration, a second re-embed and 2.27 GB of
weights on a machine with 4.7 GB free on `C:`.
*Alternatives:* Adopting BGE-M3 now for the Hindi tenders to come — rejected
for this phase: it costs hours of re-embedding to change nothing measurable on
this corpus, and the honest gap (Hindi lines are flagged, not matched) is
already reported. Keeping it as a second backend behind config — rejected, no
caller asks for it yet. `bge-large-en-v1.5` — rejected, the same 384→1024
migration cost for English only.
*Revisit when:* a tender with Hindi requirements is in scope, or a corpus
document with Devanagari text is collected. The measurement is reproducible:
`phase12_bge_m3_bench.py` in the phase's temp directory loads the model from
`D:\hf-cache` and times it on real corpus text.

---

**D-73 — A claim that fails verification is *no evidence found*, and goes to
review (defect D14).**
*Decision:* `_match_candidate` returns `no_evidence_found` with
`review_required=True` whenever the four-layer verifier does not pass. The
quote, capability text and verification outcome are still stored, so the
reviewer sees what was claimed and why it was not accepted.
*Why:* The old code returned `missing_capability` for a failed citation,
entity or semantic layer. "The quote is not on the page" or "the passage is
about another product" says nothing about the manufacturer, so publishing it
as a missing capability states something the evidence does not support — the
core rule in the PRD (4) and AGENTS #4. A failed verification is a failure of
*our* check, not a finding about the product.
*Alternatives:* Keeping `missing_capability` but flagging it for review —
rejected: the report's status counts, the compatibility percentage and the
"fails mandatory" flag all read the status, so a flagged missing is still a
missing in every number a buyer sees.

---

**D-74 — A stage that stopped at the provider's limit is read again on the
next run (defect D15).**
*Decision:* `run_pipeline` compares stage *keys* (what the stage read) and
stage *states* (whether it finished). A stage whose previous state is
`rate_limited` or `failed` runs again even when its key is unchanged
(`TenderRepo.stage_statuses`).
*Why:* An upload that hit the limit reschedules the ingestion with the same
files, so the hash is the same and a key-only check skipped the extraction and
marked the tender done — turning a half-read tender into a complete-looking
one. The key says what was read; only the state says whether the reading
finished.
*Alternatives:* Passing `force=True` from the upload route — rejected: force
would re-read every stage, spending calls on the ones that did finish, and it
would depend on the caller remembering to pass it.

---

**D-75 — A report over a tender whose extraction stopped short is labelled
incomplete ingestion (defect D16).**
*Decision:* `build_report` reads the `extraction` stage. If it is not `done`
or `skipped`, the report sets `tender.extraction.complete=false`, sets
`incomplete_ingestion=true` at the top level, and the HTML banner says the
extraction of requirements stopped short and that this is not a final
compatibility report. The API and the report page show the same line.
*Why:* The page ledger and the extraction are two different readings of the
same tender: every page can parse while the requirement extraction stops at a
rate limit. A run over such a tender covers only the requirements read before
the stop, and a reader who saw a compatibility percentage would take it for
the whole tender. Matching is still allowed — blocking it would have made this
phase's own run impossible — but its output can never pass as final.
*Alternatives:* Refusing to start a run until ingestion completes — rejected
for now: a tender that can never finish (a page that will not parse) would then
never be measurable, and the honest label already prevents the report from
being read as final. Revisit if a buyer-facing workflow needs hard gating.

---

**D-76 — The stored requirement operator reaches the comparison (defect
D17).**
*Decision:* `runs._assess` passes `requirement["operator"]` to the matcher;
`TenderRepo.list_requirements` selects the column.
*Why:* It passed `None`, so a stored "minimum 1500 RPM" was compared as text
rather than as `gte`. The comparison is the deterministic half of the
pipeline: dropping the operator changes the answer, silently, for every
numeric requirement. 13 of this tender's 21 requirements carry an operator.
*Alternatives:* Inferring the operator from the requirement text at match
time — rejected: the extraction already decided it, and re-inferring it in a
second place is a second answer to the same question.

---

**D-77 — The run's config snapshot names the embedding backend (defect D18).**
*Decision:* `config_snapshot` gains `embedding_backend`. It already named the
model and the dimension.
*Why:* The local backend normalises the vectors; the HTTP backend returns
whatever the server sends. Normalisation decides the cosine order, which
decides which passages are read, so two runs with the same model name and
different backends are not the same run. A snapshot that cannot say which one
produced the results cannot support reproducing them.
*Alternatives:* Recording the normalisation flag instead — rejected: it is a
property of the backend, not a separate choice; naming the backend is the
smallest true statement.

---

**D-78 — How many passages one requirement is read against is config, and is
in the snapshot (defect D19, part 1).**
*Decision:* `MATCH_MAX_CANDIDATES` (default 20) replaces the `limit=20` literal
in `match_requirement`; the snapshot records the value actually used.
*Why:* Every candidate is an LLM call. At 8,000 tokens a minute a full
21 x 20 search is hours of wall clock and a large share of the day's tokens,
so the cap has to be a deliberate, recorded choice — a smaller cap finds less
evidence, and the report must be able to say so. The Phase 12 run used 3.
*Alternatives:* Leaving 20 and letting the run stop at the provider's limit —
rejected: it would produce a `rate_limited` run rather than a measured one.

---

**D-79 — A verified full match ends the search for that requirement (defect
D19, part 2).**
*Decision:* The candidate loop breaks as soon as the best result so far is
`full_match`. A weaker result keeps looking.
*Why:* No further passage can improve on a verified full match, so reading the
rest only costs calls. The reported `candidates` count still says how many
passages retrieval offered, so the cap is not hidden by the early exit.
*Alternatives:* Stopping on any verified result — rejected: a partial match
does not beat a full one, and another passage may hold it.

---

**D-80 — A report names a document from the path or address it has (defect
D20).**
*Decision:* `RunRepo.results` selects `document.path` instead of a
`document.filename` column that does not exist, and the report's document name
is the last segment of the stored path, falling back to the last segment of
the URL, and `None` when the document has neither.
*Why:* Every real report raised `AttributeError: filename` while building its
query, so `GET /api/runs/{id}/report` could never have served a report. The
unit tests hid it because they used fake repositories that return the rows
the test wanted; a query built from the real schema is the only thing that
finds a wrong column name. Found on the first real report of Phase 12.
*Alternatives:* Adding a `filename` column and a migration — rejected: the
path already holds the name, and a second copy of it can drift from the file
on disk. Reading the name from `url` only — rejected: a locally collected
document has a path and no URL.

---

**D-81 — App work runs against a local Docker Postgres while Neon is
re-embedded.**
*Decision:* `DATABASE_URL` in `.env` names the local container
(`postgresql+psycopg://tender:tender@localhost:55432/tender_test`). The Neon
line stays in the file, commented, with the condition for switching back
written next to it.
*Why:* Neon is the deployment database, but its re-embed (D-68, migration
`0009_embedding_384`) stopped part-way at 3,904 of 20,891 chunks, and its rows
predate the current code (`tender_page` empty, `document.product_id` null). No
tender could be uploaded, read or streamed against it until the model had
re-encoded the corpus, which is hours of local CPU. The local container already
holds the same corpus fully embedded (20,892/20,891 chunks), so the app can be
exercised today. Nothing about the corpus is deleted: Neon is left as it is to
be resumed, not rebuilt.
*Switch back when:* `python -m app.services.embeddings` run against Neon reports
every chunk embedded, and the ingestion has been re-run for `document.product_id`
and the page ledger. Then uncomment the Neon line and comment the local one.
*Alternatives:* Waiting for the Neon re-embed before touching the UI — rejected:
it blocks work that does not depend on the deployment database. Pointing the
tests at Neon — rejected outright by D-71.

---

**D-82 — A tender being read is streamed to the UI, and a run refuses a
tender that is not fully read.**
*Decision:* The extraction publishes what it has read, and two routes serve it.
`pipeline._extract_all` calls `on_read(read_so_far)` after every requirement;
`run_pipeline` turns that into a write of the whole set with
`repo.replace_requirements` **and a commit**, throttled by
`STREAM_INTERVAL_SECONDS` (default 1.0). `GET /api/tenders/{id}/requirements`
answers with the rows stored right now plus `partial: true` while the status is
`queued`/`processing` (it used to answer 409).
`GET /api/tenders/{id}/requirements/stream` is a `text/event-stream` of the same
payload: `snapshot` on connect, `requirements` on each change, `end` carrying
the final payload. The UI subscribes with `EventSource` and falls back to the
1 s poll when there is no `EventSource`, when a frame cannot be parsed, or when
the stream errors before the read finishes. `POST /api/runs` refuses a tender
whose status is `queued`, `processing` or `failed` with a `ConflictError`;
`rate_limited` is still allowed, because that read did finish as far as it went
and its report is labelled incomplete (D-75).
*Why:* Reading a tender is minutes of model calls, and until this change the
extractor wrote nothing until the last requirement, so the UI could only show a
spinner: `GET /requirements` answered 409 for the whole read. Writing a
complete snapshot per interval keeps the table consistent (no half-written
requirement, no duplicated rows on resume) while making the work visible. The
run guard follows from the same fact: while the list is partial, a run would
answer "no evidence found" for requirements nobody has read yet, which is the
absence-of-evidence error PRD 4 and AGENTS #4 forbid.
*Alternatives:* Streaming the extractor's in-memory state over a websocket —
rejected: a second copy of the truth, lost when the connection drops, and it
cannot answer a client that reconnects. Writing every requirement immediately —
rejected: one write and commit per requirement, and no bound on the extra
traffic. Polling only — rejected: the UI already polls, but the point of the
change was to push, and the endpoint costs one generator. Refusing a run while
the *page ledger* is incomplete — rejected: that would block every tender with
one unparseable page, and the report already carries the `incomplete_ingestion`
label (D-75).


---

**D-83 - A batch of embeddings is written with one statement, because the
round trip is the cost, not the encoding.**

*Decision:* `embeddings._write_embeddings` stores a whole batch with one
`UPDATE ... FROM (SELECT unnest(:ids::uuid[]), unnest(:vectors::text[])::vector)`,
instead of one `UPDATE` per chunk. The vector is serialised with `repr` per
float, which round-trips exactly; the stored text form was compared to the input
and is identical.

*Why:* Measured against Neon from this machine, one round trip is 300 ms. The
old loop issued 32 of them per batch and committed once: 9.55 s per batch of 32,
which capped the job at 2.8 chunks/s while the RTX 3050 encoded the same batch
in about 30 ms (1,011 chunks/s steady state). The local database hid this
completely, because a round trip there costs almost nothing. After the change
the same job ran at ~24 chunks/s, 8.5 times faster; the remaining time is the
one commit per batch and the fetch, not the model.

*Alternatives:* One transaction per chunk - rejected: more round trips, not
fewer. `executemany` - rejected: psycopg sends it as a client-side loop, so it
is the same 32 round trips. A larger `BATCH_SIZE` - rejected: it would also
raise peak memory on the CPU path, and the gain is now small next to the commit.
A throwaway script instead of the service - rejected: Neon is the deployment
database, so the same wall would be hit again on the next ingestion there.

---

**D-84 - The migration test tells alembic where its version table is.**

*Decision:* `alembic/env.py` passes `version_table_schema` from the config to
`context.configure` when it is set, and `test_migrations` sets it to the schema
it migrates.

*Why:* The test migrates a schema of its own with `search_path=<test>,public`
(public is needed so the `vector` type resolves). Alembic then found
`public.alembic_version` at head and ran no migration at all, so the test
asserted on an empty schema. It only failed when `TEST_DATABASE_URL` pointed at
a database that had already been migrated, which is exactly what that variable
is documented to point at; the default run skips it, so the defect was hidden.

*Alternatives:* Drop the `public` search path - rejected: `vector` would stop
resolving. Delete or stamp the public version table - rejected: that is the
application's own history, and D11 says a test must not touch it.

---

**D-85 - The corpus is English; the Devanagari check is closed, and `eng` OCR
is a stated limit, not a silent one.**

*Decision:* Keep `INGEST_OCR_LANGUAGE=eng` and keep BGE-M3 parked (D-72). The
twelve PDFs with no text layer (6 DelVal, 6 Valmatic) were re-OCRed outside the
pipeline with `hin+eng` to test the blind spot. All twelve produced Devanagari
codepoints, and every one of them is noise.

*Why:* The original claim ("no Devanagari in the corpus") could not hold for
those twelve, because `eng` cannot emit a Devanagari codepoint even when the
page is Hindi. Re-OCR with Hindi data gives 1,023 Devanagari letters and 364
Devanagari digits (26% of all Devanagari characters) over 12 English engineering
drawings, in 781 fragments of which 667 are one or two characters long; the
longest runs are matra spam (`प्स्स्स्ल्लल`, `तततततत्त्`) and no Hindi word
appears in a sentence. Tesseract maps dimension ticks, arrows and hatching onto
Devanagari glyphs when it is given the language. So the corpus is English, and a
Hindi tender would need `hin.traineddata` installed, the twelve (and any other
scanned pages) re-OCRed with `hin+eng`, and their chunks re-embedded.

*Alternatives:* OCR everything with `hin+eng` now - rejected: it would degrade
the English text on 70 documents to fix nothing, and `hin.traineddata` is not
installed next to Tesseract. Declare the corpus clean without the check -
rejected: that is the blind spot this closes.

---

**D-86 - A comparison that cannot run is *no evidence found* with a reason code,
never *unsupported* (defect D16).**

*Decision:* All twelve "cannot compare" returns in `comparison/registry.py`
return `no_evidence_found` instead of `unsupported_requirement`, and carry a
`ReasonCode`: `attribute_not_mapped` (no value type, so no evaluator),
`value_missing` (one side had nothing to compare), `grade_not_recognised` (a
material the vocabulary does not resolve), `unit_not_comparable` (units do not
convert, or conversion failed), `operator_not_comparable` (an operator the
registry cannot apply). `Evaluation.code` is the carrier. The matcher sets
`verification_outcome = not_checked` and prefixes `review_reason` with
`<code>: <reason>`. No migration: the structural fact lives in
`verification_outcome`, which already exists, and the code is a string prefix on
`review_reason`.

*Why:* PRD 4 reserves Unsupported for a clause no document could answer -
commercial, legal, financial, warranty, vague - and `runs._unsupported()`
already decides that at requirement-type level (FR-14). `class` resolves to no
attribute, `cast carbon steel` is not a grade the vocabulary knows, and a
numeric clause with a value on one side only are all technical, verifiable
clauses that a document could have answered. Publishing them as Unsupported
says the tender asked something unanswerable, when the truth is that our
comparison did not run. That is the same error D-73 fixed for a failed
verifier: a failed check is a failure of *ours*, not a finding about the
product (PRD 4, AGENTS #4). `not_checked` is the verifier vocabulary's own word
for "this was never evaluated", so the reviewer can tell an unrun comparison
from a claim that was read and rejected.

*Alternatives:* Add a new `RequirementStatus` member for "could not compare" -
rejected: the PRD fixes the five statuses, and a sixth would change every status
count and the compatibility percentage in the report. Keep `unsupported` and
only change the reason text - rejected: the status is what the report, the
score and the "fails mandatory" flag read; a better sentence under the wrong
status is still the wrong status. Add a `reason_code` column - rejected: it
would need a migration to record a fact `verification_outcome` already records,
and the code is diagnostic detail, not a business outcome.

---

**D-87 - A page is triaged locally, and a non-technical page is recorded but
never sent to the model.**

*Decision:* `services/pageclassify.py` scores every page against four keyword
families (commercial/legal, safety/HSE, boilerplate, technical) and tags it
`technical`, `commercial_legal`, `safety_hse`, `boilerplate` or `mixed`. The
tag is stored in `tender_page.tags` (JSONB, no migration). Clauses on a page
tagged `commercial_legal`, `safety_hse` or `boilerplate` are recorded with type
`unknown` and `review_reason = "page classified as <tag>"`, and the model is
not called for them. `mixed` and `technical` pages are read.

*Why:* 422 of 422 `unsupported_requirement` results came from `unknown`
requirements, and the corpus was not the cause. The pages that produced them
were legal prose and safety instructions, which no manufacturer document could
ever answer; asking the model about them spends a call on a provider limited to
8,000 tokens per minute and produces a result that says nothing about the
product. Tagging them keeps the content visible (the clause is still recorded,
with the reason) without turning it into an assessment.

*Alternatives:* Send every page and let the model say "unknown" - rejected:
that is the behaviour being fixed, and it costs a call per line. Drop
non-technical pages entirely - rejected: the ledger must account for every
page, and the reviewer has to be able to see that the page was read and why
it produced no requirement. Ask the model to classify the page first -
rejected: it doubles the calls and makes a deterministic decision depend on
the provider.

---

**D-88 - A page is read as a table only when its grid is a real table, and
prose is joined into clauses before it is split.**

*Decision:* `read_specification` returns a `SpecificationRead` (rows plus one
`PageRead` per page) and decides per page, not per document:

- **table** - a grid is a real table when it has at least two columns, at
  least 80% of its rows share the modal width, and at least 30% of its cells
  hold text when a header (or a header carried from the previous page) proves
  the grid, 60% when nothing does. The page is read as a table only if the
  accepted tables hold at least half of the page's characters.
- **text** - every other page with a text layer. Wrapped lines are joined
  back into paragraphs (a line that does not end a sentence continues the
  one before it), then split on clause numbers (`5.2`, `24.3.1`) and
  sentence boundaries. An all-capitals line that states no obligation and no
  limit is a heading: it becomes the section of the clauses beneath it and is
  never emitted as a requirement.
- **ocr** - a page whose text layer is shorter than 40 characters, or whose
  pictures cover at least half its area (a scan with a typed letterhead over
  the page image). OCR is used when it returns anything.

`ExtractionMethod.TEXT = "text"` is new; the column is a `VARCHAR` with no
check constraint, so no migration is needed. `READER_VERSION` moves to 3.
`assign_to_items` keeps the row's extraction method and page tag, and a
tender with exactly one item takes the clauses of a prose specification,
because that is the only item they can belong to.

*Why:* every requirement in the corpus had `extraction_method = 'table'`,
including legal prose, and the fragments (`"5.2"`, `"o r violate its
provisions."`) were table cells that pdfplumber had guessed at. A guessed
grid is not a table: one column, ragged rows, mostly empty. Reading prose as
text and splitting it on clauses is what the document actually says.

*Alternatives:* Keep one path and filter the fragments afterwards - rejected:
the fragments are a symptom; the cause is that a paragraph was cut at every
line break. Use OCR on every page - rejected: OCR is ~4.5 s a page and a text
layer is exact. Trust the largest table on the page - rejected: on the BHEL
specification the largest grid is a sparse page furniture grid with 25% of
its cells filled.





