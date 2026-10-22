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
