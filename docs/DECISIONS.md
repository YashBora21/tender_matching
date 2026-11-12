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
`[runtime-config-redacted]` model and creates the HNSW index — see D-68.

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
`app.services.match.verify.check_citation` finds it on the cited page; the corpus
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
backend loads `[runtime-config-redacted]` in its own process and encodes with
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

---

**D-89 - A fragment is dropped or joined to the clause before it, and never
sent to the model.**

*Decision:* `drop_fragments` runs on the requirement rows after they are
assigned to items and before the extraction stage. A clause is a fragment
when any of these holds:

- it is only a clause number (`5.2`, `24.13.10`, `3.`);
- it holds a run of two or more characters no document prints (`|`, `_`,
  `[`, `]`), or more than 35% of its characters are those;
- it is shorter than 25 characters and names no unit and no specification
  word;
- it has no value, no verb from the requirement vocabulary and no
  specification word;
- it starts in lowercase, which is the tail of a clause whose beginning was
  lost at a page break or in a table cell.

A fragment is joined to the clause before it when that clause is the same
item and page and does **not** end a sentence; otherwise it is dropped. Both
conditions are required: the task's "starts lowercase, or the previous line
has no terminal punctuation" would join `"o r violate its provisions."` onto
whatever finished sentence came before it, which is corruption, not
recovery. A line that is mangled is never dropped: an unreadable line is a
gap, and a gap has to stay visible. The counts are logged and written to the
requirements stage's detail as JSON, so the report can say how much of a
tender was noise.

*Why:* the fragments in the database (`"5.2"`, `"o r violate its
provisions."`, `"[a7__|rpmcassamproncontactoR"`) were sent to the model one
call each and came back as `unknown` requirements, which the report then
counted as unsupported. Dropping them before the call is cheaper and keeps
the unknown count meaning what it says.

*Alternatives:* Drop every line under 25 characters - rejected: `"125 MM
minimum"` is 15 characters and is a requirement. Drop every line with no
verb - rejected: `"Type Flanged ends both side"` is a specification row and
has no verb. Keep the fragments and mark them unknown - rejected: that is
the flood this change exists to stop.

---

**D-90 - The model's `unknown` is a proposal, and a deterministic re-typer
fills it in from the clause's own words.**

*Decision:* `retype_technical` runs on the extracted requirements inside the
extraction stage, before the stage is recorded. It only ever moves a clause
from `unknown` to `technical`; it never overrides a type the model gave and
never decides a match. A clause is re-typed when:

- it names a specification attribute (`size`, `pressure`, `efficiency`, ...)
  from the `ATTRIBUTE_WORDS` table, **and**
- it is not on a page classified `commercial_legal`, `safety_hse` or
  `boilerplate`.

The attribute, operator, value and unit are read from the clause and written
into the fields the model left empty. The operator is `gte` for
"minimum/at least/or more", `lte` for "maximum/at most/or less", `gt`/`lt` for
"greater/less than", `between` for "between", and `eq` when the clause states
a value with no direction. A clause with an attribute but no readable value
is still re-typed and flagged for review, because nothing can be compared
without both sides.

Two guards keep it from inventing a specification:

- a bare number is the attribute's value only when it follows the attribute
  word directly ("Class 150"), never when it is further away ("... as per API
  598" is the standard's number, not a pressure);
- a number written as a range ("10-20 mm") is not one value, and a number
  with a sign keeps the sign only when the sign is a sign and not a range's
  dash ("-20 °C" is a temperature, "10-20" is a range). A range leaves the
  value empty and the clause flagged.

A value with no attribute is not re-typed: "80% of bid quantity" and "300
Mtr." state numbers, and a number on its own is as likely to be a commercial
term as a capability.

*Why:* 173 of the 231 requirements in the local corpus were `unknown`, and
some of them state a limit plainly ("Class 150 (minimum); suitable for 15 bar
(g) working pressure"). The model's type is not a decision the PRD lets it
make (FR-14 is classification, and classification feeds matching); the
re-typer is deterministic, inspectable, and its evidence is in the fields.

*Alternatives:* Re-prompt the model to type again - rejected: it doubles the
calls on a provider with an 8,000 TPM limit and makes a deterministic
decision depend on the provider. Treat every unknown clause with a number as
technical - rejected: it turned "80% of bid quantity" and a safety clause
into technical requirements. Keep every unknown unknown - rejected: a clause
that states a size and a limit is a requirement, and the report would count
it as unparsed.

---

**D-91 - Every page read ends in the ledger, and a tender with nothing read
is `incomplete`, not `done`.**

*Decision:* the requirements stage writes the page ledger itself, before the
extraction runs, from the `PageRead` list the reader returned:

- one row per page, with the triage tag, the ledger status (`parsed`,
  `skipped`, `ocr`, `failed`), the method, and the attachment the page came
  from;
- a failed page's reason goes in `skip_reason`, so a reviewer can see that
  the page was looked at and why it gave nothing. The table has no column for
  the character count, and one is not added for a diagnostic: no migration.

The ledger is written only when the requirements stage ran, so an unchanged
tender is not rewritten. It is written *before* extraction, so a read that
stops at the provider's limit still has a ledger.

`TenderStatus.INCOMPLETE = "incomplete"` is new (`tender.status` is a
`VARCHAR(64)` with no check constraint, so no migration). A tender is
`incomplete` when it was read without failing but gave no page, no item or no
requirement. It is not `failed` (a document that could not be read) and not
`done` (a document that was read). A run against an `incomplete` tender is
refused like a run against a failed one, and a re-upload of the same file
reads it again.

A `SpecificationRead.error` - the file does not exist or cannot be opened -
fails the requirements stage with that reason. It is a document nobody could
look at, not a document that states nothing.

The report's declared page count is now the **specification's**, taken from
the attachment the ledger's pages name, with the wrapper's own count only as
a fallback. The ledger describes the specification; comparing it with the
wrapper's 17 pages reported 15 pages nobody read on a two-page specification.

*Why:* the ledger was never written, so every report said "0 of 17 pages"
and `incomplete_ingestion` was either always true or silently ignored. And
`done` with zero requirements reads as "this tender states nothing", which is
the absence-of-evidence trap (PRD 4).

*Alternatives:* Add `chars` and `error` columns - rejected: the error fits
`skip_reason`, and the character count is a diagnostic nobody reads; a
migration for it is cost without a decision. Keep the wrapper's page count -
rejected: it counts a different document. Treat an empty read as `done` -
rejected: it is the trap the four-state ledger exists to prevent.

---

**D-92 - An `unknown` clause is not assessed, and the report says how much of
the tender that was.**

*Decision:* a run assesses only the requirements that were given a type.
`runs.assessable()` filters out `unknown`, and:

- the match loop writes no `result` row for an unknown clause;
- `RunProgress.expected` and the report's `results.expected` are
  `assessable × manufacturers`, so a run with no assessable clause is complete
  with zero results rather than permanently partial;
- `RunProgress.requirements` stays the tender's total requirement count, so a
  caller can see how many clauses were not assessed.

The report gains:

- `not_assessed`: every unknown requirement with its item, page and the reason
  it was not assessed (the page's classification, a missing item, or nothing
  typed it). It is neither a pass nor a missing capability;
- `unknown_requirements`: `unknown` of `total`, `percent`, and the split
  `non_technical_pages` / `fragments` / `unparsed`;
- a banner line `Requirements typed unknown: N of M (X%)` with that split,
  shown only when N is not zero, and an HTML *Not assessed* section.

The count is over **clauses**, not only stored requirements:

- `M` = stored requirements + fragments dropped before extraction;
- `N` = stored unknown requirements + fragments dropped;
- `non_technical_pages` = stored unknown clauses whose page was classified
  `commercial_legal`, `safety_hse` or `boilerplate` (recorded in
  `review_reason`, matched through `NON_TECHNICAL_REASON`);
- `fragments` = the requirements stage's dropped count, read from its `detail`
  JSON (D-89);
- `unparsed` = the rest of the stored unknown clauses.

The three buckets add up to `N`. A **merged** fragment is not counted: it
became part of the clause before it, and counting it again would count one
clause twice.

*Why:* writing an `unsupported_requirement` row for every clause nothing could
type flooded the result list and the CSV with clauses no document was ever
asked about, and it put a status on them that the PRD reserves for clauses no
document can answer. The run now says "not assessed", which is what happened,
and the report states how large that gap is and why.

*Alternatives:* Keep writing `unsupported_requirement` rows - rejected: it is
a finding about the clause, not about the manufacturer, and 75% of the local
corpus was in that bucket. Drop the unknown rows silently - rejected: that
hides the gap (PRD 4). Count dropped fragments as requirements - rejected:
they are fragments, and the line's `total` is clauses read, which is why the
label names clauses as well as requirements. Exclude non-technical pages from
`N` - rejected: they are stored as unknown and a reader comparing N with the
requirement list must see them.

---

**D-93 - A skipped requirements stage loads what is stored; a run that skipped
both the reading and the extraction writes no requirements.**

*Decision:* `run_pipeline` reads the specification, assigns items, drops
fragments and writes the page ledger only when the requirements stage actually
ran. When its key is unchanged the stage is `skipped` and its output is loaded
from the database instead: `_stored_records()` turns `list_requirements` rows
back into `RequirementRecord`s (type, attribute, operator, value, unit, page,
section, method, confidence, flags). The page tag is recovered from the reason
the clause carries (`NON_TECHNICAL_REASON` prefix), because it is not a column.

`_persist` writes requirements only when at least one of the requirements and
extraction stages ran (`_requirements_rewritten`). A run where both are skipped
saves its checkpoints and nothing else.

`TenderRepo.list_requirements` also returns `source_section`,
`extraction_method` and `confidence`, so the resume path (requirements skipped,
extraction re-run after a rate limit or a prompt change) writes the reader's
fields back unchanged instead of losing them.

*Why:* a stage marked `skipped` was still rebuilding raw clauses from the file
and writing them over the model's output. Every no-op run therefore replaced
the types the extractor read with `unknown` and gave every requirement a new
`uuid4` - the same rows, but not the same requirements. It also silently
invalidated anything holding a requirement id, such as a gold slice. A skipped
stage must be a no-op or it is not skipped.

*Alternatives:* Keep rebuilding but copy the stored types onto the fresh
records - rejected: ids still change, and matching a rebuilt row to its stored
one by position or text is a guess. Skip the write only when the whole run is
skipped but still rebuild the list - rejected: the extraction stage reads that
list, so a resume would re-ask about raw clauses and lose the earlier answers.
Add a `page_tag` column - rejected: the reason already carries the tag, and a
second copy could disagree with it.

---






## D-94 - Entity validity reads the document row, and a quote is normalised like a chunk

`check_entity` no longer searches the passage for the manufacturer, model or
family. It takes the document's metadata (`document.manufacturer_id` ->
`manufacturer.name`, `product.model`, `product.family`,
`document.model_or_family`) and the product the run is answering for, and
compares the two. A document that declares the model is model-level evidence;
a document that declares only a family, or declares no product at all, is
family-level evidence and goes to review. A document that declares a
different manufacturer or a different model fails the layer. A passage that
never repeats its own manufacturer is not a failure: it is still that
manufacturer's document.

`check_citation` and the reader share `app/services/textnorm.py`:
`normalise_text` folds Unicode compatibility forms, turns vertical tab and
form feed into a space, removes the remaining control characters and
collapses whitespace. The reader's `_clean` uses the same `strip_control`, so
a quote and the chunk it came from cannot be normalised differently.

*Why:* the gold slice showed CIRCOR evidence failing entity validity with
"the passage does not mention CIRCOR" on a CIRCOR brochure page. The passage
was never the provenance; the document row is. The citation layer also only
collapsed spaces, so a form feed in the page's text layer could make a
verbatim quote unmatchable.

*Alternatives:* keep the passage check as a fallback when the metadata is
silent - rejected: it re-introduces the exact failure, and a silent document
is family-level evidence, not a pass. Normalise only the quote and not the
page - rejected: the two sides must use one function or they drift.

---

## D-95 - Pressure class is an ordered categorical, and a missing evaluator is review

`pressure_class` is a canonical attribute in the vocabulary file, with a
value-shape pattern (`Class 150`, `PN16`). The registry keeps an
attribute-specific evaluator map beside the value-type registry:
`pressure_class` -> `evaluate_ordered_categorical`. A higher class covers a
lower requirement (Class 300 meets Class 150); a lower class is
`missing_capability`, because the document states a capability with a failing
value. ASME class and PN are different systems and are not converted here:
that comparison needs review.

`get_evaluator` asks for the attribute's own rule first, then the value type.
An attribute with neither returns `no_evidence_found` + `needs_review` with
`ATTRIBUTE_NOT_MAPPED`; it is never `unsupported_requirement`, which PRD 4
reserves for a clause no document could answer.

The carbon-steel material group now holds `cast carbon steel`, `A216`,
`A216 WCB`, `A216 Gr WCB`, `A216 WCC`, `A216 Gr WCC` and their `ASTM`
spellings. They are data in `attribute_vocabulary.json`, not code.

*Why:* the gold slice's P02 ("Flanged Gate valve class 150") came out
`unsupported_requirement` because "class" was not a canonical term, and P03
("cast carbon steel" vs "Carbon Steel, A216 Gr WCC") came out
`GRADE_NOT_RECOGNISED`. Both are technical clauses a document can answer.

*Alternatives:* a new `ORDERED_CATEGORICAL` value type - rejected: the value
type is a database enum, so it needs a migration for a rule that belongs to
one attribute. Treating a lower class as partial - rejected: a lower pressure
rating is a failing value, not a close one.

---

## D-96 - A requirement is retrieved with its item, its attribute and its value

`matching.build_query` composes the retrieval text from the BOQ item's title,
the clause, and the attribute/value/unit the normalizer read. A part is added
once, so a title and a clause that repeat each other do not weight the same
words twice in the keyword rank. `match_requirement` takes `item_title` and
`requirement_attribute`, and `runs._assess` passes `item.name` and
`requirement.attribute`; the composed query is what `search` receives.

The atomizer keeps a `context` on every atom - the section heading plus the
piece the atom was cut from - and `retrieval_text` adds it only when the atom
does not carry a clause marker of its own ("1500 RPM", "Class 150"). It also
splits two sentences on one line and a second obligation joined by "and".

*Why:* the gold slice's clauses are often bare fragments ("Class 150",
"1500 RPM"), and searching for the fragment alone matches every page that
mentions the number. The subject lives in the item, and the attribute lives in
what the normalizer already read; both were being thrown away before the
search. The atomizer is still a module fix, not wired into production
extraction (D-89 note); the query construction is the operational fix.

*Alternatives:* make the extractor rewrite the clause before searching -
rejected: that spends LLM tokens to do a deterministic join, and it would
change the clause the verifier is answering. Prefix every atom with its
context unconditionally - rejected: a self-contained clause then repeats
itself and dilutes its own distinctive words.

---

## D-97 - A chunk is embedded with what it is about, and retrieval knows the valve type

`embeddings.embedding_text` prefixes the text it embeds with a header -
`Document`, `Manufacturer`, `Product`, `Section` - and `embed_corpus` joins
the document, its manufacturer and its product to build it. The product label
prefers the family, then the document's own `model_or_family`, then the model.
The chunk's stored text is unchanged: the header is a retrieval aid, and the
verifier still checks a quote against the page (FR-23).

The valve types are data in `categories.json`: each category may declare
`types`, and `categories.infer_type` reads one from the item's title with the
same longest-term rule as the category. `retrieval._type_filter` drops a chunk
only when it positively names a different type: a chunk that names the wanted
type is kept (a catalogue may cover several), and a chunk that names no type
is kept, because silence is not a different type (PRD 4). An ambiguous item -
"gate valve and ball valve" - is not filtered on a guess (FR-T9).

*Why:* a datasheet table row is a number and its column header. Embedded
alone, "150" is close to every other row of numbers in the corpus, and the
semantic search has no way to know it is a pressure class in a CIRCOR
catalogue. The same gap let a ball-valve brochure answer a gate-valve item,
because both are `valves` and the category filter cannot tell them apart.

*Alternatives:* store the header in the chunk text - rejected: it would become
part of every quote and every citation check. Exclude a chunk that is silent
about its type - rejected: that turns a corpus gap into a missing capability.
Filter on the product model - rejected: a tender rarely names a model, and the
model filter already exists for the case where it does.

*Re-embedding:* chunks embedded before this change keep their old vector until
`python -m app.services.embeddings --force` runs. That is a data operation,
not a schema change.

---

## D-98 - Recall is measured before status, and every miss is attributed

`eval/recall.py` measures retrieval recall@k against gold `document`/`page`
labels. It reuses `matching.build_query`, `categories.infer_type` and
`retrieval.search` - the same code the matcher calls - so the number is the
recall the product gets, not a second, more forgiving implementation. A miss
is put in exactly one bucket:

- `corpus_gap` - the gold document is not in the corpus at all;
- `chunking` - the document is indexed, but no chunk sits on the gold page;
- `retrieval` - the page is indexed and ranked, but outside the top k;
- `verifier` - the gold page was retrieved, and the published status still
  differs from `true_status`.

The buckets are checked in that order: a document that is absent cannot be a
chunking bug, and a page that was never offered cannot be a verifier bug.

The harness refuses a label file with no rows, and a file where no row names
both `gold_document` and `gold_page`; recall over an empty set is not 0%, it
is not a measurement. A row with neither is counted as *unlabelled* and left
out; a row with only one of the two is an error, not a silent drop. The gold
slice of 21 pairs produced so far has no gold columns, so the harness refuses
it with exit 1 rather than publishing a 0%.

*Why:* the status scorer (`eval/harness.py`) can only say a pair was answered
wrongly. It cannot say whether the page was ever in front of the model, and
the two failures need opposite fixes: a `retrieval` miss is a query or filter
problem, a `verifier` miss is an extraction or comparison problem, a
`chunking` miss is the reader, and a `corpus_gap` is the collector.

*Alternatives:* score recall inside the report harness - rejected: the report
is offline by design and carries no ranking, so recall would need a second
run or a replayed search. Compare the run's own cited page to the gold page -
rejected: that measures the pipeline end to end and hides a retrieval miss
behind a verifier miss.

---

---

## D-99 - A higher pressure class is a partial match until a deployment says otherwise

`evaluate_ordered_categorical` used to treat `offered >= required` as a full
match. It now treats only equality as full. A higher class is a
`partial_match` at `comparison_partial_score_max` (0.75) with
`needs_review = True`; a lower class is still `missing_capability`, because
that is a documented capability with a failing value.

`comparison_pressure_class_higher_covers_lower` (default `false`) restores the
old behaviour for a deployment whose buyer accepts a higher class outright.
The setting flows through `Settings.comparison` into
`ComparisonPolicy.pressure_class_higher_covers_lower`, so the run's config
snapshot records which rule it was scored under (FR-34).

*Why:* a pressure class is a rating, not a drop-in equivalence. A Class 300
valve is commonly heavier, longer face-to-face and more expensive than the
Class 150 valve the bill of quantities asked for, and the clause may also fix
face-to-face or weight. Publishing a higher class as a full match decides an
engineering question the evaluator cannot see. Partial + review keeps the
claim and asks the person who can see the whole clause.

*Alternatives:* full match for `>=` (the old rule) - rejected: it hides the
interchangeability question. Missing capability for `>` - rejected: the
capability is documented and does not fail, so calling it missing would be
wrong under PRD 4. Convert ASME class to PN - rejected for the same reason as
D-95: a conversion is a fact about a standard, not a guess.

---

## D-100 - A document's own type places a page that does not repeat it

A catalogue page says "150" under a column header, not "ball valve". The type
filter (D-97) therefore had a hole: a silent chunk from a control-valve
brochure was kept for a gate-valve item because silence was treated as
compatible.

`categories.ProductType` now carries `families`, read from
`data/categories.json` alongside the type terms. `document_type()` runs the
same inference over the document's declared model/family and file name, so a
document and an item are placed in one vocabulary. The valve families shipped
are the ones the corpus actually declares: `Fisher globe valves` for globe,
`Series 7/8`, `Worcester controls` and `VM-4700-S` for ball, `D-Series` and
`Control valves` for control.

`retrieval._type_filter` resolves in this order:

1. the type the chunk itself names - kept if it is the wanted type, dropped if
   it positively names a different one;
2. otherwise the type the document declares - same rule;
3. otherwise neither names a type: the chunk is kept, `type_unconfirmed` is
   set, and `matching` records `applicability = family_level`, caps the score
   and flags review.

*Why:* a page belongs to the document it is in. Dropping a silent page would
turn a corpus gap into a missing capability (PRD 4); keeping it without a
type would let a different product answer the clause. Family-level evidence is
the honest middle: the passage is real, its applicability is not confirmed.

*Alternatives:* drop every silent chunk - rejected: silence is not a
different type. Trust the document title alone - rejected: the stored path is
a file name, and `model_or_family` is the field the corpus seed actually
fills; both are read, and neither is invented. Add a new column - rejected: no
new fact is stored, the type is derived from fields that already exist.

---

## D-101 - A result keeps the reason code that stopped its comparison

`ReasonCode` existed on `Evaluation` (D-86) but was not persisted, so a stored
result could not distinguish "no evaluator for this attribute" from "the
catalogue is silent": both were `no_evidence_found`.

`result.reason_code` is now a nullable VARCHAR(64) with a CHECK constraint
(migration `0011_result_reason_code`), written by `findings.store_result`,
carried by `MatchResult.reason_code`, read by `RunRepo.results`, and exposed
in the JSON report, the review queue and the CSV. NULL means the status is a
finding: the catalogue answered, or it was silent. A non-NULL code means a
limit of ours stopped the comparison - `attribute_not_mapped`,
`value_missing`, `grade_not_recognised`, `unit_not_comparable` or
`operator_not_comparable`.

*Why:* the two cases need opposite fixes. `attribute_not_mapped` is a
vocabulary gap we can close; a silent catalogue is a coverage fact the buyer
must see. Counting them together makes the unknown-attribute rate invisible
and overstates corpus coverage.

*Alternatives:* put the code in `review_reason` text - rejected: a report
would have to parse prose. Reuse `status` - rejected: AGENTS requires status,
`verification_outcome` and `applicability` to stay separate, and a system
limit is not a product finding. A separate table - rejected: the code is one
attribute of one result, and a column is the smallest change.

---

## D-102 - A short document is read whole, and an attribute's names widen the query

`retrieval.search` does two things after the two searches have been fused.
First, `expand_query` reads the alias table's approved names for every
attribute the vocabulary marks `query_expansion` and adds the names the query
does not already carry. `connection_type` is the first such attribute: its
names are `end connection`, `body style`, `ends`, `connections`, `connection`
and `end`, so a requirement that says "screwed end" also searches the wording
a datasheet uses for the same thing. Second, `expand_siblings` finds the
distinct documents among the fused candidates, counts their highest indexed
page, and - for a document with at most `retrieval_sibling_page_limit` pages
(default 3) - inserts every chunk of that document after its first retrieved
chunk, in page order, then trims to the same budget. The fused order of the
original candidates is not re-scored; siblings are added beside the hit they
belong to.

*Why:* the Delval Series 7/8 bulletin is found (page 1 is rank 1 for both
items 10 and 11), but the specification page 2 never enters the fused top 20.
The requirement's distinctive words - "screwed end", "socket weld", "122 mm" -
do not occur anywhere in the bulletin, so page 1 wins on the generic terms.
A three-page bulletin is cheap to read whole, and its specification page is
the one that answers the clause. The alias expansion is the same problem from
the other side: "screwed end" and "body style" are one attribute, and only the
alias table knows that.

*Alternatives:* raise the global `limit` or fetch deeper per modality -
rejected: it changes ranking and cost for every requirement, and the miss is
specific to short documents. Expand every attribute's synonyms - rejected: a
common word such as "pressure" would pull a dozen names into every query and
dilute the distinctive tokens. Add a second retrieval pass - rejected: the
same evidence would be read twice and paid for twice.

*Boundary:* the page count is the highest indexed page, so a document whose
last page carries no chunk is treated as ending at its last chunk. A document
longer than the limit is not expanded: its page-level ranking is left alone.

---

## D-103 - A listed set that excludes the required value is Missing, silence is No evidence

`verifier.listed_set_exclusion` reads a passage for a categorical attribute the
requirement names, takes the values the document lists after the attribute
(`Body Style: Flanged end / Butt weld end`), and returns a `ListedSet` with
`MISSING_CAPABILITY` when the required value is not among them. The quote is
the whole line, so the citation layer still gates it: `matching._match_candidate`
uses the finding only when `check_citation` passes. A passage that never lists
the attribute returns `None`, and the existing path keeps `NO_EVIDENCE_FOUND`.

The check runs when the extractor found no claim. The LLM may correctly say a
passage does not answer the requirement, but "does not answer with the
required value" is not the same as "says nothing": a document that offers
flanged or butt weld ends has stated a failing value, and PRD 4 calls that
Missing capability. The connection synonyms were extended with `screwed end`,
`socket weld ends`, `butt weld end` and `flanged end` so the requirement's own
wording supplies the value to look for.

*Why:* Delval Series 7/8 page 2 lists the body style and excludes screwed and
socket weld ends. Reading that as No evidence would overstate coverage and
hide a real non-compliance behind a retrieval fact.

*Boundary:* only a list of at least two short values after `:` or `=` is read;
a single value or a prose sentence is left to the extractor, so a page that
merely mentions an attribute is not turned into a finding. Only categorical
attributes are checked this way: a material or a number has its own evaluator,
and the token rule would misread alloy aliases.

*Alternatives:* let the comparison registry decide from the extracted value -
rejected: the extractor may return no fact at all for a negative passage, so
the comparison never runs. Treat every unextracted categorical as Missing -
rejected: that is the absence-of-evidence error AGENTS #4 forbids.

---

## D-104 - Atomic gold rows, and document recall apart from page recall

The gold slice for items 10 and 11 is a compound clause: "three piece ball
valves 2" screwed end ASME class 150 cast carbon steel face to face 122mm" is
four independent facts, and one `true_status` for the whole line hides which
fact drove it. `eval/gold/proposed-GEM_9671792-items-10-11-atomic.csv` adds one
row per requirement x manufacturer x attribute - valve type, nominal size,
pressure class and face to face - carrying `required_value`,
`documented_value`, the page, the verbatim quote and the atomic status. Every
row is `label_state=proposed`, `confirmed=no`; nothing is confirmed here. A
quote must be an exact substring of the named page, and
`eval/tests/test_atomic_gold.py` checks that against the real PDF, skipping
when the corpus is absent (the PDFs are not in git).

`eval/recall.py` now reports **document recall** and **page recall** separately.
Document recall asks whether the gold datasheet was offered at all; page
recall asks whether the page that answers the clause was offered. A document
found on the wrong page is a ranking problem inside a document we already
hold, and averaging the two numbers would hide it.

*Why:* the confirmed recall was 33.3% page recall@3 while both Delval misses
were the *right document, wrong page*. One number cannot say that. The atomic
rows are the same argument one level down: the confirmed Delval status is
`missing_capability` because of the end connection, but its valve type, size
and class are full matches, and a reviewer needs to see that.

*Boundary:* a standard reference is not a value. "Face to Face: API 6D,
ASME B16.10" states the dimensional standard, not 122 mm, so the atomic
face-to-face status is `no_evidence_found`, not Missing capability and not
Full. Worcester states no face-to-face dimension anywhere in the document, so
it has no atomic face-to-face row: absence of a quote is not a quote.

*Alternatives:* keep only the compound row - rejected: status accuracy per
attribute is then unmeasurable. Infer 122 mm from API 6D - rejected: the
standard is not on the page and the value would be invented. Report a single
recall number - rejected: it conflates "we do not have the document" with
"we have it but rank the wrong page".

---

## D-105 - Atomize the parsed clause before the model reads it, and stop resumably on a limit

The atomizer now runs in the production requirements stage, after the clauses
are read and fragments are dropped and before the extraction stage. Each atom
becomes a `RequirementRecord` of its own with the parent's item, page, section,
span, page tag and review flag, and `extraction_method=prose`. A table row is
left alone, and an already atomic clause is returned unchanged. `ATOMIZER_VERSION`
is folded into the requirements stage key, so a change to the split
invalidates the stages after it. The reader's `extraction_method` now flows
into `extract_requirements`, into the stored row and through the LLM cache;
before this a prose clause was recorded as a table row and would never have
been split on a rerun.

The LLM target is the environment: `LLM_BASE_URL`, `LLM_API_KEY` and
`LLM_MODEL` are the only source of the client's endpoint, key and model, and
the client refuses to start without a key rather than returning empty
answers. A rate limit is not caught by the extractor: `LLMRateLimited`
propagates to `_extract_all`, which stops at the first one, keeps the atoms it
never asked about, marks the stage `rate_limited`, and leaves the stage
unfinished so the next run reads it again.

*Why:* atomizing after extraction would leave one attribute and one value on
several facts, and there is no honest way to say which atom the value belongs
to. Splitting first is also the order the PRD gives (parser -> atomizer ->
type gate -> normalizer, FR-T8). The rate-limit rule is the AGENTS #3 rule:
a failure to ask is not a document that says nothing, and a tender whose
reading stopped must not be labelled done.

*Alternatives:* atomize the model's answer after extraction - rejected: the
model's single attribute/value cannot be distributed across atoms, and
dropping it would send every atom to review with no value. Ask the model
again per atom after a compound answer - rejected: the same calls, one
round later, and a second failure path. Catch `LLMRateLimited` as an ordinary
extraction failure - rejected: it would turn "we could not ask" into "the
line states nothing" and let a partial read look complete.

*Boundary:* the atom's `source_span` is the parent clause's span, not a
sub-span: the split is inside the clause text, and the clause is the
provenance every atom shares.

---

## D-106 - The audit sheet is a fixed-seed sample, and the export does not grade

The 15-row audit sheet for items 10 and 11 is a random sample of the results
the matcher publishes for those two requirements against every manufacturer
that holds documents. The rows are ordered by (item, requirement,
manufacturer) before `random.Random(seed).sample`, so the same seed over the
same results is the same sheet, and the order the database returned them in
cannot change it. `audit_ok`, `correct_status` and `notes` are written empty:
the export selects rows for a reviewer, it does not judge them. The candidate
cap is written on every row and in the provenance file, because a result read
with a cap of 3 is not the same answer as one read with a cap of 20.

A rate limit stops the export rather than being caught: the pairs already
answered stay in a checkpoint, and a rerun asks only the pairs that are
missing. The export never fills a missing pair with a no-evidence row, because
"we could not ask" is not "the catalogue is silent" (AGENTS #3, #4).

*Why:* PRD 15 asks for a manual spot check of cited pages. A spot check is
only a measurement if the sample is reproducible and the reviewer's columns
are not pre-filled by the thing being checked. The sheet is not a score: it is
the input to one, and the export must not be able to make its own rows look
right.

*Alternatives:* a stratified sample by status - rejected: it over-represents
rare statuses and is no longer a sample of what the system published. A fixed
list of pairs - rejected: it is not random and cannot be reproduced from the
seed. Filling `correct_status` from the gold file - rejected: the reviewer
would be agreeing with the system's own labels, not checking them.

---

## D-107 - A short document is one unit, and a document that names another product is not evidence

Two things the audit sheet showed are fixed at the retrieval layer, not by
changing what a status means.

**A short document is read whole, cap or no cap.** D-102 reads a document of
three pages or fewer whole when one of its pages is retrieved, but the merged
list was then trimmed to the candidate cap, which could cut off the very pages
that had just been added. With a cap of 3, the Delval Series 7/8 bulletin
returned nine chunks of page 1 and dropped page 2 - the page that lists the
value - and the requirement was published as `no_evidence_found` while the
documents say `missing_capability`. The trim now removes the lowest ranked
candidates outside the expanded document and never a page of it: the cap
counts documents, and a short document is one unit. A page that was read and
then thrown away is the page-level version of the confusion AGENTS #4 forbids.

**The document's declared type decides for the whole document.** A chunk that
names the wanted type was kept even when the document declared another one, so
the 355-page Fisher control-valve handbook answered a ball-valve item because
one page mentions ball-valve trim. The document now decides: if it declares a
type and that type is not the item's, the document goes. Silence is still kept
and flagged family-level.

**A document that names another product category is dropped.** The stored
`document.category` column is the first check, and it is empty for a corpus
whose manifest never filled it - sulzer, wilo and bharat_bijlee - so a pump
catalogue answered a valve item. The document's own name and declared model
are now read with the same vocabulary, one level up: a document that names the
item's category is kept, one that names none is kept (silence is not a
different category), and only one that positively names another category and
not this one is dropped. A file name that runs its words together
("submersibledewateringpumps") is matched with the separators removed, because
that is how file names are written.

**A family in the vocabulary now matches however it is punctuated.** Terms were
matched raw against normalised text, so every hyphenated family - "Series 7/8",
"VM-4700-S", "D-Series" - was dead, and the Delval and Val-Matic bulletins fell
through to family-level. Terms are normalised like the text, so the family that
was already in the data file does its job.

*Why:* the audit sheet's two findings were both about evidence that was never
read or should never have been read. Neither is fixed by a new status or a
threshold: the retrieval layer was dropping pages it had already fetched and
keeping documents about something else.

*Alternatives:* raising the candidate cap - rejected: it pays for every
requirement to cover a page-ordering bug, and the cap is a cost control, not a
filter. Dropping every document that does not name the wanted category -
rejected: silence is not a different category (PRD 4) and it would empty the
corpus for manufacturers whose manifests are thin. Matching category terms
against the passage - rejected: a valve datasheet mentions pumps in passing;
the document's own name and declared model are what place it.

---

## D-108 - A run that has nobody to ask is refused at the request

`create_run` asks `needs_model(requirements)` and, only when the answer is
yes, `config_error()`. If the model is not configured the request is refused
with the reason in the message, before a row exists. A tender whose assessable
clauses are all commercial, legal or financial is still creatable with no key
at all, because those are answered from their clause type and no call is made.
`LLMClient()` is built inside the guarded block of `execute_run`, so a run that
loses its key between the request and the worker lands `failed` with that
reason instead of staying where a caller can only poll it.

*Why:* the end-to-end audit found the flow unreachable rather than wrong: on a
machine with no key, POST /api/runs returned 409 with no reason and the created
run never moved off `queued`. A caller cannot tell "not yet" from "never", and
the reviewer's first attempt at the whole flow is the one that has to say what
it needs. Naming no provider in the refusal is deliberate - a hosted, a
self-hosted and a local model are refused for the same reason (D-109).

*Alternatives:* checking at execution only - rejected: that is what produced
the stuck run. Refusing every run when no key is set - rejected: it withholds
a report that needed no model to be honest. Leaving it 409 - rejected: 409 is
for a state conflict, and the tender-not-read guard already owns that answer;
a machine that cannot serve the request is a 400 with a reason.

---

## D-109 - The wait is read from the response, not from a vendor's list

A rate limit is detected from the status code and the wait is taken from
wherever the provider put it: the standard `Retry-After` header (seconds or an
HTTP date, matched case-insensitively because a transport folds header names
and a proxy may not), a field of the JSON error body (`retry_after`,
`retryAfter`, `retry_delay`, `retryDelay`, at any depth), or the prose the
provider said it in ("try again in 24.9s"). A bare number is seconds; a
suffixed one says what it counts. The client imports no provider's exception
class. `llm_base_url` joined the config snapshot.

*Why:* the model layer is provider-agnostic by rule, and the account the
evaluator issued is a rate-limited OpenAI-compatible endpoint that may be
swapped. Code that catches a vendor's exception type breaks on the next swap
and, worse, reads the swap as a generic failure - which AGENTS #3 forbids
turning into "no evidence". The endpoint goes in the snapshot because the same
model name served elsewhere is a different answer, and a report has to say
which one it is quoting.

*Alternatives:* importing the provider's SDK exceptions - rejected: it is the
provider-specific coupling this rule forbids. Retrying on any 4xx - rejected:
a 400 is a wrong request and retrying it spends the key for nothing. Assuming
a fixed back-off - rejected: a provider that says "wait 30s" and is waited on
5s is retried six times against the same window.

---

## D-110 - A run has a budget, stops at it, keeps its answers and resumes

`LLM_CALL_BUDGET` and `LLM_TOKEN_BUDGET` (0 is no ceiling) are checked by the
client before each call, against its own counters, so one budget bounds one
run. Spending it raises `LLMBudgetExceeded`, which the run handles exactly like
the `LLMRateLimited` D-105 propagates: the results already written stay
committed, the run is labelled `rate_limited` - neither `done` nor `failed` -
and the message names what was already spent. `RunRepo.answered(run_id)` gives
the requirement x manufacturer pairs the run stores, and a resumed run skips
them. Both budgets go into the config snapshot. `make e2e` reads that status as
its own line - how many results are kept, and that the same command continues -
so a stop at a budget does not look like a match that failed.

*Why:* the assignment's model access is a limited corporate quota and the
advice is to spend it carefully, so the ceiling has to be the account's own
number, not a refusal discovered mid-run. A run that stops having kept its
answers means the next pass pays only for the pairs still missing; without
that, adding the last requirement costs re-reading the whole tender. The
budget is on the snapshot because a run that stopped at a budget is only
reproducible if the budget is known.

*Alternatives:* lowering `match_max_candidates` when the quota is small -
rejected: that changes what is looked for, not what may be spent, and the
report would read as a corpus gap. More retries - rejected: retrying against
a spent quota spends more. Folding the stop into `failed` - rejected: it
loses the difference between "this stopped early" and "this broke", and the
answers found are real. Deleting the partial results so the run starts clean -
rejected: it pays twice for the same calls.

---

## D-111 - The corpus is restorable from the manifests a fresh clone already has

`make corpus-restore` (`app/services.corpusrestore`) reads every manufacturer
manifest and every catalogue manifest and writes the files back where ingestion
looks for them. It chooses nothing: a document is fetched only from an address
a manifest already records, and never from a guessed, searched or
name-built one. A file whose hash does not match its manifest row is not
written, because the report cites pages of the file the manifest describes.
robots.txt is asked again, because a policy can change after collection, and a
document no longer allowed is reported as a gap. A row with no address is
reported as unrecorded. `corpus_max_bytes` (200 MB) is a separate cap from the
upload cap: the corpus holds a 96 MB pump range. `--dry-run` reports without
fetching, and ingestion's "no corpus found" now names the two commands that
would put it back.

*Why:* the collected PDFs are gitignored and the manifests are in git, so a
fresh clone knows exactly which files the corpus is made of and has none of
them - FR-8 answers "no corpus found" with no route out, and every test that
needs a corpus is silent on a machine that just cloned this repository.

*Alternatives:* committing the PDFs - rejected: 100 MB+ of third-party
copyright in git. Making the reviewer run `make collect` - rejected: it
searches, which finds different files than the ones the citations name. A
symlink to another disk - rejected: not reproducible anywhere else.

---

## D-112 - Tender to report in one command, and a preflight that answers first

`make e2e TENDER=<pdf|dir|tender_id>` runs the API's own chain in the
foreground and one process: corpus, embedding, tender read, matching, report.
Each step checks its precondition before it starts and the command stops at the
first it cannot meet, with the reason and the command that fixes it; a step
already done reports `skipped`, which is not `blocked`. `ARGS=--check` answers
without running anything. `GET /api/preflight` is the same question over HTTP,
with `can_run`, `why_not` and `next_steps`. It downloads nothing and calls no
model: it reads the database and the configuration. Both answers name the model
and the endpoint a run would call, because a key from one provider and a base
URL left pointing at another is a refusal discovered mid-run - the most
expensive way to find out.

*Why:* the assignment is satisfied by an end-to-end flow, and a reviewer with
a fresh clone cannot reach a report by reading the code to work out the order.
D-108 fixed the worst symptom by refusing the request; this makes the answer
available before any request is made, so the first attempt either produces a
report or names the one thing missing.

*Alternatives:* a shell or docker script that chains the commands - rejected:
it would be a second copy of the chain, and the chain a reviewer runs has to
be the one production runs. A background job - rejected: the value is seeing
each step's state. A README list of five commands - rejected: it works only
for someone who already knows which of the five will fail.

---

## D-113 - A manifest address is not gated by the rule written for a tender's links

`check_url` and `fetch` take `from_document` (default True). The host
allowlist applies to an address read out of a document; `make corpus-restore`
passes `from_document=False`. Every other guard still applies to both paths:
the scheme, no private address after DNS resolution, no redirect to another
host, the size cap, the time cap, and robots.txt.

*Why:* the two addresses have different trust. A tender is untrusted input -
its links are written by whoever uploaded it, so an allowlist is the fence.
A manifest row names a host a person chose and recorded, and the file it
fetches is checked against the hash in that same row, so the integrity guard
is the hash rather than the list. The first attempt at this widened
`fetch_allowed_domains` instead, and that was wrong twice over: it loosened a
fence designed for untrusted links, and `fetch_allowed_domains` is folded into
`stage_keys` as `fetch_config`, so changing the default invalidates the stored
tender's stages and forces a paid re-extraction against the limited key (D-110).

*Alternatives:* widening the allowlist - rejected, for both reasons above. A
separate `CORPUS_ALLOWED_DOMAINS` - rejected: a second list to keep in step
with the manifests, when the manifest already names the host. Turning the
checks off in restore - rejected: that removes the SSRF guard along with the
host rule.

---

## D-114 - A page that cannot be read says why, and the image carries the reader

`ocr_page_with_reason` returns the text and the reason there is none, and the
tender reader puts that reason in the ledger's `skip_reason`. Three answers
that used to look identical are now distinct: this machine has no OCR program
(`tesseract_missing()` names it and gives the install command), the OCR failed
on this page (the exception's type and message), the page holds nothing to read.
The backend image installs `tesseract-ocr` and `tesseract-ocr-eng`.

*Why:* Tesseract is a program the image has to carry, not a package `pip`
installs, so it is the one dependency an image-only BOQ page depends on - and
the reason is the only thing a reviewer sees. AGENTS #3 makes a failed page a
state of its own precisely so "this page is blank" and "this machine cannot
read pages" are not the same answer; a skip_reason that says only "no text"
sends the reviewer to the wrong fix.

*Alternatives:* raising when Tesseract is missing - rejected: one page's
dependency must not stop the other forty pages of a tender, and the ledger
already carries the failure. Returning empty text and letting the page count
as parsed - rejected: that is the silent page loss FR-9 forbids. Keeping
`ocr_page`'s `Optional[str]` shape and guessing from emptiness - rejected: it
is the collision this fixes.

---

## D-115 - The page ledger is backfilled for a tender read before the ledger existed

When the requirements stage is skipped because the tender is already stored,
the pipeline asks whether the ledger is empty (`_ledger_is_empty`) and, if it
is, re-reads the pages locally and writes them. `list_pages` now orders by
page. A repository that cannot answer counts as having a ledger: not writing
is the safe answer when the state of the pages is unknown.

*Why:* the ledger is a state of the *pages*, not of the requirements, so a
tender read before the ledger was written has requirements and no page states
at all - and because its requirements stage key has not changed, the stage
would never run again and the ledger would stay empty forever. Every page has
to end in a state (FR-T2), and the incomplete-ingestion guard cannot tell an
unread page from a page that was fine. Re-reading costs no model call: the
pages come from the parser on disk.

*Alternatives:* changing the requirements stage key to force a re-read -
rejected: it pays a model call to produce page states the local parser can
give. Writing the ledger in a SQL migration - rejected: the states are the
result of parsing the PDF, which is code, not data. Leaving it and requiring a
re-upload - rejected: it throws away the extraction the key already paid for.

---

## D-116 - A document the fence refused is supplied by hand, in the UI

The upload page lists a stored tender's attachments from
`GET /api/tenders/{id}/attachments` - name, address, hash, kind, status, size,
pages - and marks the rows whose status is `failed` or `restricted` as ones to
supply. The file picker posts them to `POST /api/tenders/{id}/attachments`,
which freezes the file with its hash and records it in the manifest like any
fetched document. The notice after an upload says to re-upload the tender to
have it read with the new document, and that the stages which do not depend on
it are not run twice.

*Why:* the endpoints already existed and nothing in the product surfaced them,
so the only way a reviewer could use a document on a host the fence refuses -
or one behind a login they have and the collector does not - was to find the
directory in the source. A hand-supplied file carries the same provenance as a
fetched one, and it does not loosen the fence for every future tender.

*Alternatives:* telling the reviewer to copy the file into the tender directory
- rejected: that is a step only someone who has read the code can take. Adding
the host to `fetch_allowed_domains` - rejected: it changes a rule about
untrusted links to solve one tender. Making the upload replace the tender
- rejected: the tender and its attachments are separate rows, and replacing
loses the requirement list the read already produced.

---

## D-117 - A wildcard robots.txt rule is a rule, and the standard library does not read it

`app/core/robots.py` matches a robots.txt itself: `*` is any sequence, a `$`
anchors the end, a rule with no `$` decides by prefix, the longest matching
path wins and an Allow of the same length wins the tie, and the group that
applies is the one whose agent token appears in the user agent, with `*` as the
fallback. `core/fetching.robots_allows` and the collector's robots check both
use it; `urllib.robotparser` is used by neither.

*Why:* auditing the corpus's 101 recorded addresses against this matcher found
four the collector had fetched anyway - four KSB catalogues on `www.ksb.com`,
whose robots.txt says `Disallow: *.pdf`. The project's own rules name that
reason: `fetch_allowed_domains` leaves KSB and Crane out *because* they disallow
`*.pdf`, and FR-4 is the promise that a host's robots.txt is obeyed. So the
policy was written down, and the code meant to enforce it silently disagreed
with it: CPython's parser ignores a rule that carries a wildcard - `*.pdf`,
`/*.pdf` and `/*.pdf$` were each tested, and none of them blocked anything. A
compliance check that fails open is the worst kind of check, because it reads as
compliance.

*Alternatives:* keep `RobotFileParser` and rewrite the rule into a form it
honours - rejected: there is no such form, which
`test_the_standard_library_parser_does_not_ban_that_document` pins. Fix only the
collector - rejected: the fence in `core.fetching` asks the same rule of the
same file, and two implementations of it is how they came to disagree; the
collector now imports the app's matcher instead of carrying a copy. Read an
unparsable rule as no restriction - rejected: the safe direction for a
compliance check is to refuse, and a rule this project cannot read is not a rule
this project may ignore.

*Boundary:* prefix semantics are deliberate and are the more careful of the two
readings: `Disallow: /a.pdf` also refuses `/a.pdf.old`. A document on a
distributor's own host is judged by that host's robots.txt, not the
manufacturer's - which is why the KSB booklets held as secondary sources stand
and the four fetched from ksb.com do not.

---

## D-118 - Ingestion counts the documents it read, not the listings it walked

A corpus file is often named by more than one manifest - the collector's own,
`catalogue/manifest.csv`, and the `catalogue/<maker>/manifest.json` written
beside it. `ingest_corpus` now keeps the set of hashes it has already walked in
the run and skips a listing of bytes it has seen, so the report's count is a
count of documents. The first listing of a file is the one whose metadata
reaches the row, which is the order the manifests were already read in.

*Why:* three numbers described one corpus - `make ingest` said 128, the restore
said 117, the database said 101 - and the difference was listings, not files,
not documents. A reviewer who cannot line the numbers up against the corpus
cannot tell a clean run from a corpus with something missing in it, and this
project's own rule is to dedup by hash.

*Alternatives:* documenting the three counts and leaving the walk as it was -
rejected: the counts then need an explanation every time they are read, and the
same file was parsed and re-written twice. Merging the manifests into one file -
rejected: they are written by two different collectors and both layouts are in
use. Reporting the collapsed listings as a separate counter - rejected: it is
the number nobody acts on, and it doubles the ways to be confused.

## D-119 - A number that changes how a run behaves has one home, in config

A sweep for literals that should have been settings closed six of them. The
collector's user agent and the fetch timeout now come from `fetch_user_agent`
and `fetch_timeout_seconds`, which both `app/core/fetching.py` and
`collect/collect.py` read; the document-type vocabulary got `doc_types_path`
like the two vocabularies beside it; the embedding batch size got
`embedding_batch_size`, read by the corpus walk and the encoder; the health
payload's environment got `app_environment`; `to_stored` defaults its
`schema_version` to the setting and its `extraction_version` to
`schemas.models.EXTRACTION_VERSION`; and `StoredRequirement` no longer carries
defaults for them. The collector also gained the size cap it never had, so a
document `make collect` stores is one `make corpus-restore` can bring back.

*Why:* each of these had two answers. The robots.txt fetch used `timeout=30`
while the configured fetch timeout is 60, so "how long do we wait for a site"
depended on which code path asked. The agent string existed in two files, so a
deployment collecting under a different name could set one and be refused by
the other. The stored row's version came from a literal while the cache key
came from the setting, which is the combination that writes rows no resume
path reads.

*Alternatives:* leaving the geometry constants inside the page parsers -
rejected deliberately, not by omission: a table's column tolerance is not a
deployment's choice, and a setting for it is a value nobody would tune.
Putting the batch size in `Settings` but keeping `embed_corpus(batch_size=...)`
- rejected: no caller ever passed it, so it was a second answer with the
appearance of a preference.

The sweep ended with `.env.example` diffed against `Settings.model_fields`, so
"every knob the stack reads is in `.env.example`" is checkable rather than
claimed. One setting went the other way: `pipeline_concurrency` was removed.
`docs/CONSTANTS.md` listed it as "the knob that keeps a run inside the rate
limit", and no line of code read it - a run processes one tender at a time, so
the documented knob was a promise, and a documented promise nobody can turn off
is worse than no knob.

*Switch back when:* a run dispatches more than one tender at once, at which
point the ceiling has to be honoured where the calls are made, not declared
here.

---

## D-120 - A read that no process will finish is reconciled at startup, and a reason belongs to the attempt that wrote it

The background read lives inside the API process. The pipeline commits each
stage as it goes, so a process that stops mid-read leaves the stage rows behind
and the tender's own row still saying `processing`. Nothing then ever writes a
terminal state: the requirements stream stays open because `processing` means
"still being read", the UI spins, and the reason - sitting in `tender.error` and
in the stage's own `detail` - is never shown.

Three separate things made that one dead row:

1. **No reconciliation.** `app/services/tenderrecovery.py` now reads the stage
   rows of every tender left `queued` or `processing` and records what they
   already prove: a stage that stopped at the provider's limit is
   `rate_limited` (a resumable state, D8), a failed stage is `failed`, and a
   read whose stages all settled and which stored requirements is `done`. It
   runs once from the FastAPI lifespan, before this process schedules a read of
   its own, and a failure there is logged rather than raised - a database that
   is down is what `/api/health` reports, and refusing to start turns a
   degraded service into no service.
2. **The decision is not a timer.** A slow read is not a dead one. The rule is
   read from the stage rows, so a read that is genuinely in flight (a stage in
   a non-terminal state) is left alone even when another worker shares the
   database. A tender whose stages all settled but which stored no requirement
   is left alone too: the stages cannot tell `done` from `incomplete`, and PRD 4
   forbids merging them (D-91).
3. **The reason outlived the attempt.** `TenderRepo.set_status` wrote `error`
   only when one was given, so `set_status(PROCESSING)` left the previous
   attempt's message on the row - which is exactly how the dead row carried a
   rate-limit error it had not produced. The status and the reason are now
   written together, and the same fix went to `RunRepo.set_status`, which had
   it too.

*Also fixed:* `TenderRepo.count_requirements` returned the empty result list
when a tender had none, so any caller comparing it to zero raised instead of
answering. It returns a count.

*Why:* the row was not a wrong verdict, it was an unfinished one, and the
product had no way to say so. The stages already carried the answer; nothing
read them. Repairing from them costs no model call and cannot invent a state the
read did not reach.

*Alternatives:* a staleness cutoff ("no progress for N minutes") - rejected: it
guesses at liveness from wall-clock time and would eventually finalise a slow
read that is still working. Sweeping inside the requirements endpoint -
rejected: that request may be served while another worker owns the read, and a
read path must not write. Marking every `queued` row `failed` at startup -
rejected: with more than one process, a row queued by one worker can be about
to run in another, and `failed` would be a verdict nobody reached.

*Boundary:* a tender left `queued` with no stage at all is **not** repaired,
because nothing distinguishes "the task was lost" from "the task is about to
start". It stays visible as `queued`; the honest fix for that case is a
heartbeat, not a guess.

## D-121 - A rule nothing can reach is not a rule, and a knob nothing reads is not policy

A laziness pass over the repo (the whole tree, not the last diff) found four
things in the same shape: an answer written twice, with one copy unreachable.

1. **The range comparison could never run.** A `between` clause keeps its value
   as the text it was written in ("10-20"), and `matching.py` handed the
   comparison `requirement_range=None, capability_range=None` at both call
   sites. `evaluate_numeric` then failed its own "a number on both sides" gate
   before reaching the range branch sitting below it, so every range requirement
   published *no evidence found* against a document that answered it. The pair is
   now read back with the grammar that parsed the clause -
   `normalizer.parse_range()`, one pattern list, not a second one in the
   comparison - and threaded at both sites. The gate accepts a requirement that
   states a pair instead of a number, and a `between` clause whose pair could
   not be read stays *no evidence found* with `operator_not_comparable`: it is no
   longer answered as `missing capability`, which would have been a verdict about
   the product drawn from a gap in our reading (AGENTS #4).
2. **What a verdict is worth was written twice.** `SCORING_FULL_SCORE` and
   `SCORING_MISSING_SCORE` are in `ScoringPolicy`, in `.env.example` and in the
   policy a run snapshots - and no code read either one, because each evaluator
   returned its own `1.0` and `0.0`. The declared score is now applied once, in
   `registry.evaluate()`, for the two verdicts that carry no graded decision;
   a partial match keeps the score the comparison band gave it.
3. **The container blanked its own model.** `docker-compose.yml` passed
   `LLM_MODEL: ${LLM_MODEL:-}` and `EMBEDDING_MODEL: ${EMBEDDING_MODEL:-}`, so on
   a machine where those are unset the container got an empty string, which beat
   the configured default and left the run with no model to ask. Both are
   pass-through now: unset means the backend keeps its own answer.
4. **Two copies of a name, one of them dead.** `services/facts.py` kept a second
   `PROMPT_VERSION` that no cache key and no caller read - only the one in
   `extractor.py` is used - so an edit to the prompt beside it would have looked
   versioned and been nothing. `collect/collect.py` re-derived the doc-type
   vocabulary path (`configured or shipped default`) instead of calling the
   `doc_types_path()` the app side added in D-119; it imports that now.

*Also:* `.env.example` is checked against `Settings.model_fields` in both
directions by `test_config.py`, with the seven names compose and the scripts own
listed beside it. A setting with no line is a knob nobody can find; a line with
no setting is a knob that does nothing - and `PIPELINE_CONCURRENCY` (D-119) is
the case the test would have caught.

*Why:* each of these cost a reader to find and no reader to notice. The cheapest
way to keep them out is to make the two lists agree by test rather than by
memory, and to prefer the code path that already exists over a second copy of
its answer.

*Alternatives:* delete the two scoring settings instead of wiring them -
rejected, PRD 8 names them as the deployment's policy, and the run snapshots
them into every report. Store the pair on the requirement row, or the fact's
operator on the cached read - rejected: a migration and a re-ingest of the whole
corpus for a number the stored text already carries, and the fact column is not
written by this path today. Apply the policy inside each evaluator - rejected:
twenty literals, and the next evaluator forgets it.

*Left:* `scoring_partial_default` is still read by nobody - a partial match's
score comes from the comparison band. It is listed under Open rather than
quietly kept.

---

## D-122 - The last knob nothing read is gone, and so is the module nothing called

D-121 wired `SCORING_FULL_SCORE` and `SCORING_MISSING_SCORE` into the
comparison's entry point and left one behind, listed under its own *Left:*
rather than quietly kept: `scoring_partial_default` (and
`ScoringPolicy.partial_default`) was still in config, in `.env.example` and in
the snapshot a run records, and still read by nothing. A partial match's score
has come from the comparison band (`comparison_partial_score_min/max`) since
before either setting existed, so there was no behaviour to wire it to. It is
deleted rather than wired: a knob that would override a graded comparison with a
flat number is the abstraction the band already replaces.

`services/tenderslice.py` (198 lines) and its test are deleted. Its only
reference in the tree was `tests/test_tenderslice.py` - no caller in `app/`, no
Makefile target, no doc. It was the first vertical slice (FR-12, FR-T6, FR-T7,
FR-T9, FR-T14), and `tender/ingest.py`, `tender/pipeline.py` and
`match/match.py` now do those jobs separately and under test.

`README.md` documented `make eval REPORT=report.json GOLD=eval/gold/tender-a.jsonl`.
That file has never existed. The line is removed rather than repointed at
`eval/gold/synthetic.example.jsonl`: scoring a report against synthetic labels
prints a real-looking accuracy number for labels that were invented, which is
the one thing the gold harness exists to prevent. `make eval-check` against the
synthetic file stays, because validating a gold file's shape is what that file
is for.

*Why:* both deletions are the same finding - a name that promised something and
had no reader. Neither could change a result, which is exactly why neither was
noticed.

*Not done:* the folder was not reorganised for looking crowded. A file count is
not evidence; each of the remaining modules has a caller, and the count was
taken from the import graph before anything was touched (D-123 is the grouping).

---

## D-123 - The services folder is grouped by stage, not by kind

`backend/app/services` held 31 modules in one flat list, and finding one meant
knowing the domain already. They are now grouped by the stage of the flow each
belongs to:

    tender/       receive, collect, parse, classify, ingest, pipeline
    requirement/  a clause turned into a typed requirement
    corpus/       the manufacturer corpus: read, seed, restore, audit
    match/        a requirement against the corpus: retrieve, extract,
                  verify, score, record
    output/       what a run produces: its state and its report
    cli/          the commands that drive the whole flow

Three modules stay at the top - `llm`, `embeddings`, `textnorm` - because every
layer imports them and no stage owns them. Giving them a package would invent an
owner and risk an import cycle for a tidier listing.

*The rule:* group by the stage a module serves, never by what it is. There is no
`utils/`, no `helpers/`, no `models/`: a folder named for a kind is where things
go when they do not belong anywhere, and then nobody can say what is in it.

*Renames:* a module was renamed only where the package already carried its name
(`tenderupload` -> `tender/upload`, `matching` -> `match/match`). Everything else
kept its name, so `rg verifier` still finds the verifier. The full map is in
`services/__init__.py`, because a decision written before this one names modules
by their old path and a reader following it must still land on a file.

*Rejected:* splitting `tender/pipeline.py` (964 lines) and
`tender/ingest.py` (1,019) into smaller files - the project's own rule is to
split by job, never by line count, and both already are one job. Grouping by FR
number - the folders would move every time the PRD is renumbered.

*Verified:* this is a move plus an import rewrite. Inside a moved file the edits
are its import lines, with two exceptions the move forced:
`match/evidence.py` and `match/category.py` find their data file by
`Path(__file__).resolve().parents[N]`, and the extra package level makes that
`parents[3]`. The test suite caught it (every category and doc-type assertion
failed at once) - a depth constant is the one thing a move can break silently.
Beyond that, the changes are the package docstrings and the one-line code
reference in `DECISIONS.md` and `WORKLOG.md`. All 47 distinct `app.services.*`
references in the tree resolve to a real module, and the full suite passes
unchanged (645).

---

## D-124 - The frontend reads like a case file, and a reviewer can go back and stop

The three screens were a generic admin panel: one blue, one sans, a stack of
white boxes. The work they show is a tender review, and the artifact a reviewer
produces is a case file - one verdict per requirement, each claim backed by a
quote on a page. The restyle takes its vocabulary from that, not from a UI kit.

*Tokens:* a cool ledger paper (`--paper`, `--surface`, `--sunk`), ink for text,
one seal navy for the product's own edge, and the five verdict colours already
named in PRD 4 (`--v-full`, `--v-partial`, `--v-missing`, `--v-silent`,
`--v-na`). Nothing new is encoded: a colour is a status and nowhere else.

*Type:* three roles, not three fonts to look designed. A serif for headings and
for the evidence quote - the two places a person reads a sentence; the system
sans for body; mono uppercase for labels, page numbers, run ids and document
names, which are identifiers and should not look like prose. No webfont is
loaded: `frontend/Dockerfile` serves a static bundle through nginx and the demo
machine has no network at runtime, so a font request is a render-blocking
failure for a slightly nicer letterform. The stack is
`Iowan Old Style / Palatino / Georgia`, all of which ship with the OS.

*The one signature:* the verdict rail - a 3px left edge in the status colour,
reused on a report row, a rank card, a notice, the drawer, the quote inside it
and the drawer title, and drawn in miniature in the brand mark. It is set with a
`--rail` custom property and `:has()` selectors
(`.report-table tbody tr:has(.pill-missing_capability)`), so no component
changed its DOM and no number on screen changed. The mandatory gate is the
loudest thing on a rank card for the same reason: `.rank-card:has(.flag)` turns
its rail crimson and paints the flag, so "fails mandatory" is visible before the
score is read.

*Back:* the app has two views and, until now, no way from a report back to an
upload except reloading the page. `App.tsx` enters each view as a history entry
(`pushState` on go, `popstate` on Back), so the browser's Back button works; the
topbar carries a sticky "Back to the tender" while a run is open; and a new
`Trail` component shows the hierarchy - Tender, Match, Report - with the current
step marked `aria-current="step"`. Following the research rather than the habit,
the trail complements Back instead of replacing it, and only the crumb that is
genuinely reachable is a button: "Match" is not clickable from the report,
because there is no screen that shows a chosen run on demand - clicking it would
have gone somewhere it does not describe.

*Stop:* there is no cancel route in the API and the contract was out of scope, so
a "Stop run" button would have been a lie. The controls are "Stop watching" and
"Watch it again" on the run page, and "Stop watching" / "Follow it again" on the
tender read. They close the page's own poll and EventSource, the last payload
stays on screen, and the copy says which thing stopped: the page asked once a
second, the run carries on server-side, nothing was cancelled.

*Motion:* a staggered rise on the stack, a breathing dot on a status pill that is
still in flight, a slow sheen along the progress fill, and a press scale on the
controls. Each one marks a state that changes on its own; none is decoration on
a static table. All sit inside the existing `prefers-reduced-motion` guard - the
sheen's gradient is dropped there and the dot stays, unblinking, so the
information survives.

*Craft:* concentric radii (a card's 12px outer corner over the rail's 4px inner),
a sticky table header with a shadow so it is obvious the header is floating,
tabular numbers, balanced headings, a ruled-paper background under the panels.

*Not done:* no CSS framework, no token pipeline, no component library, no dark
mode, no cancel endpoint, and no visual-regression harness. The run page's
progress bar is still a single bar: the API reports one percentage and the UI
does not invent stages it was not told about.

*Why:* the previous look said "software template" to a reviewer who is judging
whether the output can be trusted in a tender evaluation. A case file is the
form this work actually takes, and it also fixed two real gaps: no way back and
no way to stop a page that asks the API every second.

*Verified:* 27 frontend tests pass unchanged - every assertion is on text or an
ARIA role, and none of them moved, which is the proof that the data shown did
not change. `tsc -b && vite build` clean, `oxlint` 0 warnings 0 errors. In the
browser, computed styles confirm each rail resolves to its verdict colour
(`rgb(13,97,70)` full, `rgb(157,27,46)` missing, `rgb(43,92,138)` silent,
`rgb(138,91,6)` partial, `rgb(74,85,97)` unsupported), the gated rank card to
crimson against `rgb(18,50,95)` for a clean one, `position: sticky` on `th`, and
the drawer edge taking the status colour. Stop and resume were clicked through on
both the run page and the tender read.

---

## D-125 - Three backend states the screens did not speak

The frontend was read against the routes it calls. Every request shape and
response field matched, and three states did not exist in the UI at all:

`rate_limited` for a run. The API counts it terminal and serves the report of
what was stored (`api/runs.py`, and the status is really set at
`services/output/runs.py`), but the run page's terminal set was
`{done, failed}`. A run stopped by the provider's limit was polled forever:
frozen progress, no report, no exit. `rate_limited` joins the set.

`incomplete` for a tender. The read finished and came up short - no
specification page read, no BOQ items, no requirements
(`services/tender/upload.py`) - and the stream closes it with `end` because it
is not partial. The upload page mapped anything that was not done, failed or
rate-limited to `reading`, so the page spun on a read nobody was doing, offered
no run, and its polling fallback never stopped. `POST /api/runs` refuses that
tender too, so there was no path out at all. It is now its own phase, with the
reason the backend stored and the one action that works: supply the missing
document, then upload the same file again - a duplicate in this state is
re-read, unlike one in `done`.

`parsing`, `verifying`, `rate_limited` and `incomplete` as words. None had a
label or a pill colour, and `.pill-running` - added the same day - matched
nothing, because no status in either enum is called `running`. The in-flight
list now matches `RunStatus`, and the two stopped-short states wear the same
amber as a partial match: neither a failure nor an answer.

*Why these three and not the rest of the audit:* each one left a reviewer with
no screen and no next action. The other findings show the app dropping report
fields it could render (`not_assessed`, `unknown_requirements`, `reason_code`) -
a gap in what a reader is told, not a dead end - and are left for their own
decision.

*Verified:* two new tests, one per dead end - a rate-limited run reaches the
report, and an incomplete read names itself, quotes the stored reason, offers no
run and stops after one poll. 29 frontend tests pass, `tsc -b && vite build`
clean, `oxlint` 0 warnings 0 errors. No backend file, route or payload changed.

---

## D-126 - An attachment is named by the name in its link, not by the path it shares

GeM serves every SLA and ATC link through the same path: `/contract/slafds`,
with the real file named in the `fileDownloadPath` query parameter.
`_filename_for` read only `urlsplit(url).path`, so every one of those links
became `raw/slafds` and each fetch overwrote the one before it. Measured on the
stored tenders: `2bc8f917` held four attachment rows over three files - the
24-page SLA replaced on disk by the 17-page ATC - and `acdd1135`, a tender whose
status is `done`, held four rows over one file, two of them with no path at all.
A tender read in that state is read against a document its own row does not
name.

The name now comes from `fileDownloadPath` when the query carries one, unquoted
and reduced to its last segment, and from the URL path otherwise, for the hosts
that do put the name there. Two links that still resolve to one name are both
kept: the later one is prefixed with its index and recorded in a `claimed` map,
instead of silently replacing the first.

The cache path changed with it. A frozen copy used to be written under the *old*
manifest's filename, so a tender collected before this fix would never have
reached disk under the corrected name. The cached bytes are now written under
the name this run computes.

*Why the collision guard and not only the query parameter:* the parameter is a
GeM convention, not a promise from every host. The guard is what makes "every
attachment has its own file" true whichever host the link came from.

*Verified:* four tests - a GeM download is named from its download path, two
downloads behind one path get different names, a plain URL still uses its path
(and a URL with no name falls back to `document-<n>`), and two documents behind
one `/contract/slafds` path are both frozen and both recorded `new`. Run against
the stored `2bc8f917` manifest, the two colliding URLs now resolve to
`NewIP_dd4c4653-7156-4592-81be1785321584049_CPS2HMDAM1.pdf` and
`ATC_f7ad0fe4-2309-421f-95f81785321657881_CPS2HMDAM1.pdf`.

---

## D-127 - A test run can name its manufacturers

`make e2e` always ran the whole corpus: 21 requirements against 20
manufacturers is 420 pairs, and `_match_candidate` spends at least one model
call on every candidate passage, so a run under `LLM_CALL_BUDGET=300` could
never finish - it stopped at the budget and landed `rate_limited` with a partial
report. The API already accepted `manufacturers` on `POST /api/runs`; only the
CLI had no way to pass it.

`--manufacturers a,b` is threaded from `main()` through `run_end_to_end` and
`_run` into `_matching`, which hands the list to `create_run`. Omitted, the
behaviour is unchanged: the whole corpus. A name the corpus does not hold is
still rejected by `create_run` with the names it does hold, so a typo is an
error and not an empty report.

*Why:* the first honest end-to-end pass has to be cheap enough to repeat. The
three manufacturers the confirmed gold rows name (delval_flow, Flowserve,
valmatic) cost 63 pairs instead of 420 - measured at 1,498 candidate passages
for those 63 pairs.

*Verified:* two tests - the named list reaches `create_run` and the run
executes, and the command parses `"delval_flow, Flowserve"` into two trimmed
names. The `_matching` and `create_run` fakes in the existing tests were updated
for the new parameter; the full suite is 651 passing.

---

## D-128 - A review flag is a pointer, not a workflow

`review_required` is set wherever a claim, a comparison or a read did not settle
the question (`services/match/match.py`, `services/output/runs.py`,
`services/tender/pipeline.py`), and four things read it:

- the ranking: `ranking_score = (compatibility, coverage, -review_count)`
  (`services/match/score.py`), so an equally scored manufacturer with more open
  questions ranks lower;
- the report's review queue and the CSV's and HTML's review columns
  (`services/output/report.py`), which is where a person starts reading;
- the false-confident rate (`eval/harness.py`): a wrong pair that was *not*
  flagged is the metric the PRD calls the key one, because it is a wrong answer a
  reader would believe;
- the audit sheet, which carries the flag and its reason on every row so the
  reviewer sees what the system was unsure of (`eval/audit_sheet.py`).

What it does not do: decide anything, or remember a decision. There is no column
for a human verdict, no route that accepts one - the API has three POSTs and the
rest are GETs (`backend/app/api/`) - and no control that records one. Re-running
a tender recomputes every flag from the documents, so a flag is a pointer to
where to look, valid for one run, and nothing more. That is a limit, not a gap in
the rules: PRD 12.5 lists the queue itself as stretch, and every required status
is decided by deterministic code and the verifier, not by a reviewer.

Three things now say so instead of leaving it to be inferred:

- The review queue and the evidence drawer each carry one line - "A flag says
  where to look, not what to decide. This tool does not record the decision." -
  in the two places a reader would otherwise expect to answer.
- `eval/audit_score.py` grades a sheet a person *has* filled: accuracy among the
  audited rows, the false-confident rate, the flagged-but-correct count, and
  citation correctness, joined on the `pair` id and refused outright while any
  row lacks `audit_ok`. It writes its summary to `eval/results/` and never to the
  sheet, and it states that a sample of published rows measures the precision of
  the output, never what the tender asked and the run missed.
- A flag now always carries its reason. `match_requirement` built the reason from
  the verifier's *failed* layers only, but `review_required` is also true when a
  layer passes while unsure - family-level evidence, a document that names a
  family rather than the model. That case reached the queue with no reason, which
  the UI printed as "No reason recorded". `VerificationReport.review_reasons`
  collects the failed and the unsure layers, and the flag is built from it.
  Every other reason in the codebase names its cause; none says "needs review".

*Why now:* the corpus is close enough to full that a reviewer will read a real
report, and a queue that looks like a workflow but saves nothing is worse than a
queue that says it does not.

*Next step, deliberately not built:* a record-decisions workflow - one row per
(run, requirement, manufacturer) holding a human verdict, stored *beside* the
machine verdict. It must not rewrite the status or the score: the verifier gates
publication, and the false-confident rate is measured against the machine's own
flag. The stored decision is then a second column a report can show and a
re-run can compare itself against, not a correction.

*Verified:* one new assertion in `test_verifier.py` (a borderline layer passes,
so `reasons` alone leaves the reviewer nothing); the family-level path now carries
"the document covers the GV family, not the model GV-100". `eval/tests/test_audit_score.py`
adds seven tests over a synthetic sheet written by the real exporter: the
arithmetic, the citation outcomes, the refusal of an unfilled row, a duplicate
`pair` id, an unknown status and a file that is not a sheet, and that the sheet's
bytes are unchanged after a graded run. 8 frontend tests and the 24 eval tests in
the touched files pass.

---

## D-129 - A re-read replaces the answers along with the rows they answered

Re-uploading `9d62cf12` - a `rate_limited` tender with one run holding 7 results
- raised `ForeignKeyViolation` on `result.requirement_id -> requirement.id`, and
the tender then sat at `processing` with no terminal state. Reproduced against
the real rows: 31 requirements, one `rate_limited` run, 7 results.

Two separate faults:

- **The delete.** `replace_items` and `replace_requirements` both delete the
  tender's `requirement` rows, and the foreign key refuses while a `result` still
  points at one. Since the items stage runs first, that is where the re-upload
  hit it. `_forget_results(tender_id)` now removes the results of the tender's
  runs before either delete, and logs how many went. A result names exactly one
  requirement row, and a re-read gives every requirement a new `uuid4`, so those
  results are not answers to the new set: keeping them would leave a report
  pointing at rows that no longer exist.
- **The dead transaction.** `audit_tender` caught the pipeline's exception and
  returned a failure, but left the aborted transaction in place. `ingest` then
  wrote the terminal status on that transaction, which failed in turn, so the
  tender had no state a reader could act on. It now rolls the repo's session back
  before returning, and the failure is recorded where it belongs.

*Why delete the results and not refuse the re-read:* a `rate_limited` tender is
resumed by reading it again. Refusing whenever results exist would make the one
case that needs a re-read impossible.

*Left:* requirement ids are `uuid4`, so a re-read still re-keys every
requirement, and a gold label or audit row keyed on a requirement id stops
resolving after one. Deriving the id from the clause's provenance and text would
make it stable; that is a change to ingestion, not a guard, and was not made
here.

*Verified:* three tests - a requirements re-read and an items re-read, each with
a stored result, and an audit test that the session is rolled back when the
pipeline raises. Against the real failing tender, inside a transaction that was
then rolled back: 7 results removed, the 31 requirements deleted without an
error, and the rows intact after the rollback.

---

## D-130 - A run stopped by a limit is continued, not paid for twice

`execute_run` returned at once for a `rate_limited` run, while the loop below it
skipped the pairs already stored (`answered`), and `create_run` always wrote a
new run. D-110's promise - "a resumed run pays only for the pairs that are
missing" - was therefore unreachable from every entry point: `make e2e`,
`POST /api/runs`, and the report routes all went through `create_run`.

That is wasteful on any tier and fatal on a metered one. The first real attempt
on `2bc8f917` spent 9 calls and about 20k tokens before the provider's
per-minute limit stopped it, stored 0 results, and the next attempt would have
started from zero.

Two changes:

- `execute_run` runs a `rate_limited` run again. `done` and `failed` still
  return at once: the results they hold are the answer.
- `create_run` returns the newest `rate_limited` run of that tender with that
  manufacturer set, when the new attempt would ask the same questions.
  `_answers_alike` compares the stored `config_snapshot` with the one this
  attempt would write, ignoring the two spend limits: a budget changes how far a
  run gets, not what it answers. A run read by another model, or with a
  different candidate cap, is not continued - one report would then hold answers
  from two configurations and state a single one.

*Why the same run id and not a new one:* the report route, the CSV export and
the frontend all address a run by id. Reusing it is what makes "run the same
command again" cheap instead of merely free-looking, and it keeps one report per
attempt rather than one report per fragment.

*Verified:* the test that asserted a `rate_limited` run is *not* run again now
asserts the opposite and measures it - the stored pair is not asked about, the
missing one is, and the run ends `done`. Plus a test that a stopped run is
returned instead of created, one that a fresh run is created when nothing
matches, and a unit test of `_answers_alike`: a changed spend limit still
resumes, a changed model or candidate cap does not.

*Correction, same day:* as committed, the comparison never succeeded.
`RunRepo.create` stores `{**config_snapshot, "manufacturers": names}`, and
`create_run` compared the stored row against a plain `config_snapshot()` that
has no `manufacturers` key, so every attempt looked different and every attempt
wrote a new run. On the first real pass that produced two empty runs
(`1c357f89`, `83483c7c`) where one continuation was intended, each paying for
the same pairs again. `create_run` now builds the snapshot exactly as it will be
stored and passes that to both the comparison and the insert; a test asserts
`repo.compared == repo.created["config_snapshot"]`, which is the invariant whose
absence hid this.

## D-131 - A run records where its time and calls went

Every proposed speed-up is a claim about where the seconds go, and until now the
only evidence was a log line per retry. The run reported `llm_calls`,
`prompt_tokens` and `llm_retries` on the response that finished it - and nowhere
else: they were the counters of one process, so a polled run, a stored report and
a resumed attempt all said nothing, or said the last attempt and called it the
run (D-130's *Left*).

A `metrics` JSONB column on `run` now holds what the run measured itself:

- `stage_seconds`, per stage. The matcher times `cached_fact`, `retrieve`,
  `extract`, `page_read`, `verify` and `compare` for each pair (`stage()` and
  `merge_stages()` in `services/match/match.py`); the run loop times `store`,
  `summarise` and its own wall clock. A pair that never reaches the model has no
  `extract` to report, which is the number the next step is judged on.
- the four spend figures, so `GET /api/runs/{id}` and the report state what the
  run cost after the process that ran it is gone.

*Why not in `config_snapshot`:* that dictionary is compared to decide whether a
stopped run may be continued (D-130). A measurement inside it would make every
second attempt look like a different configuration, and the pairs already paid
for would be paid for again.

*Why the figures add rather than replace:* a resumed run is the same row, so its
cost is the sum of its attempts. The stage times are added to what the row already
held and the clock starts where the last attempt stopped, otherwise a run that
spent six seconds in its first pass reports the two seconds of its second.

*What it does not do:* nothing about a verdict changes - no status, score or flag
reads a timing. A run made before this column has nothing, and the HTML says *Not
recorded: this run was made before the stages were timed* rather than a column of
zeros, which would read as a fast run. A pair whose call raised contributes no
stage times, because the exception unwinds past the merge; its cost still shows in
the wall clock and the call count. The frontend does not display the timings yet.

*Verified:* `alembic/versions/0012_run_metrics.py`, applied with `make migrate`.
Seven tests added - a run stores its pair's stages plus `store`, `summarise` and a
total; a resumed run adds to the attempt before it (6 s of extract, not 2 s);
polling a finished run returns what was stored; a matched pair records each stage
and a pair nothing retrieved records only the search; the report carries the
figures, and says *Not recorded* when there are none. 103 tests pass across the
run, matching, report and run-API files, and the full unit suite passes.

## D-132 - One pass works a run, and no transaction waits on the provider

Judging the external architecture proposal turned up two defects in what we
already have, both of them about paying twice rather than about which model
reads a passage.

*Nothing is read while the provider thinks.* `match_requirement` retrieves rows,
and the first query begins a transaction. The run commits only once a pair is
answered, so that transaction stayed open for the whole call - measured at
roughly 27 seconds per pair - holding a pool connection idle-in-transaction,
which also keeps PostgreSQL from cleaning up the rows it read. `_match_candidate`
now commits before it asks the model. Nothing pending is lost: the run writes its
result and commits after the call returns, and the only other caller is the
one-off `match` command, which stores under `--store` after the answer. The
release happens per candidate read, not per requirement, so a second passage does
not resume a transaction the first one's page read opened.

*One pass works a run at a time.* D-130 made `POST /api/runs` answer a repeated
command with the run it can continue, and the route then schedules `execute_run`
for every caller. `execute_run` only refused to work on a `done` or `failed` run,
so two callers of one `rate_limited` run started two passes over the same missing
pairs - and `result` has no key on a requirement x manufacturer pair, so each
pair was paid for twice and stored twice. The run row's state is now the lock:
`RunRepo.claim` moves `queued` or `rate_limited` to `retrieving` in one update
whose condition is that same state, and only the caller whose update landed works
it. The caller that did not is told the state as stored, with the costs the run
already recorded, because it is the run it was handed.

*What it does not do:* no status, score, flag or verdict reads either change, so
no expected-status test moved. A run left `retrieving` or `scoring` by a process
that died is not picked up by the claim - that recovery does not exist either
way, since only a `rate_limited` run is ever continued, and a fresh command
starts a fresh run as before. The tender-reading pipeline keeps its own session
discipline; this is the matching path.

*Verified:* 3 tests added - a pass reads rows and asks the model twice, and each
call is made with a transaction already closed; a run another pass holds matches
nothing, stores nothing and reports the stored state; the claim's update names the
states an attempt may start from, so it cannot be an unconditional write. 71 tests
pass in `test_matching.py` and `test_runs.py`, and the full unit suite passes.

## D-133 - The fact cache is read where the product is known

FR-21's cache was written by every run and read by none of them. `_cached_fact`
can only answer when it knows which product to ask about, and it was asked at the
top of `match_requirement`, where the caller's `model` is the only source of that
name - which the run never has, because a run names a manufacturer and a clause,
and the product is whatever retrieval brings back. The write side was never the
missing half: every answered pair stores its fact through `store_match` ->
`store_fact`, so the facts were accumulating unread. (Step 24 said otherwise; it
was checked the same day and corrected there.)

The lookup now happens where the product is known: per candidate, on
`candidate.manufacturer` and `candidate.model`, before the passage is read. A hit
skips the call and nothing else:

- the comparison runs again, with the same `ComparisonRequest` shape the fresh
  reading builds, so a clause whose value was never normalised comes back
  `value_missing` from either path and a pair's verdict does not depend on which
  way it was reached;
- the applicability comes from the product row the fact is filed under, so
  family-level evidence is still capped and flagged;
- the quote, page and document travel with the answer, and the pair still gets
  its own fact row and its own result row. Two clauses asking one product for one
  attribute are one reading and two assessments - never merged (AGENTS).

*What a hit does not re-run:* the four verification layers. The gate is at the
write: `cached_fact` returns only a row stored as `verification_status = verified`,
and a fact is written verified only when the layers passed it. The limit of that
is stated in the code: re-fetching a document does not invalidate the facts read
from it, so a hit is the reading the corpus held when the fact was written.

*One value, chosen the same way twice.* A product can hold the same attribute from
two documents that disagree, and a `fact` row has no timestamp to choose by, so
`cached_fact` now orders by the id. It is an arbitrary choice, but a stable one:
without it a re-run could answer the same pair from a different value, which is
the flakiness D-130's resume exists to avoid.

*Verified:* 3 tests added - a product whose attribute is already verified is
answered with no call at all, the cache is asked with the passage's own
manufacturer and model, and a second clause on the same product costs no reading
while still carrying its own status and score. 74 tests pass in
`test_matching.py` and `test_runs.py`, and the full unit suite passes. What the
cache saves is measurable on a re-run without a new counter: `run.metrics` reports
the `extract` seconds and `llm_calls` of the run (D-131), and a run over a tender
whose products were already read reports fewer of both.

## D-134 - A run reports what it has measured while it is still working

D-131 stored the stage times and the spend, and wrote them only when the run
stopped. So the page that follows a run showed a count and nothing else - the
complaint the architecture proposal made was accurate about the display even
where it was wrong about the pipeline: for twenty minutes a working run looked
like a run that had measured nothing.

`execute_run` now writes the metrics with every pair, in the commit the pair
already makes: one more JSONB update on the run row, none of them a second
transaction. What a caller polls is therefore the run's own measurement up to
that pair - the same figures the report prints when it finishes, because the
terminal paths rewrite them the way D-131 did.

The run page shows them. It renders the stage names and the numbers the API
sends, adds nothing, and works nothing out: the seconds, the model calls and the
tokens are the backend's, and the page that recomputed them would be a second
answer to disagree with (the rule Step 16 has been applying to every other
number). Where no pair has landed yet it says so in words - *Nothing yet: the
first pair is still being read* - rather than a column of zeros, which a reader
would take for a fast run.

*What it does not do:* the API response is unchanged - these fields were already
in it and nothing was reading them. There is still no per-requirement line in the
progress tree: the run does not publish which clause it is inside, and a page
that guessed one from the counts would be fabricating the state the proposal asked
to stop fabricating. A resumed run's figures stay the sum of its attempts, because
the pair loop adds to what the row already held (D-130, D-131).

*Verified:* 1 backend test (the costs of the first pair are stored before the
second is asked, and `stage_seconds` holds `store` and a positive `total`
mid-run), 2 frontend tests (a working run prints its own stage seconds and token
count; an unmeasured run prints the sentence, not zeros). 44 pass in
`test_runs.py`, 31 frontend tests pass, `tsc --noEmit` is clean, and the full unit
suite passes.

## D-135 - A pair the category rule dropped is reported as one

A requirement is compared only against documents in its own BOQ item's category
(FR-17, non-negotiable 5), and when that leaves nothing to read the run said *the
corpus holds nothing that answers this requirement* - the same words a genuine gap
produces. The status was right: `no_evidence_found` counts as coverage and never as
a failing capability (PRD 4). What was missing is that a reviewer could not tell
"this manufacturer's catalogue is silent about the end connection" apart from "this
manufacturer sells pumps and the item is a valve", which is not a finding about the
manufacturer at all but the shape of the corpus.

It follows D-101's pattern exactly: the status stays, and a code says why the
comparison never ran. `ReasonCode.CATEGORY_MISMATCH` is the sixth value;
`result.reason_code` is a VARCHAR with a CHECK list rather than a native type, so
migration `0013` drops and recreates the constraint instead of altering an enum, and
a freshly created database gets the same list from `_enum(ReasonCode, ...)` in
`repositories/schema.py`.

*What it will not claim:* the code is set only when the corpus's own
`document.category` column says so, and only on the path that retrieved nothing. A
document that declares no category is kept by the rule - silence is not a different
category - so a corpus whose manifest never filled the column keeps reporting an
ordinary gap, and the category is never inferred here from a file name, which is
retrieval's job. The question is asked in the scope the search was asked in: the
manufacturer, and the model when the clause names one, so a clause about one model
is not called excluded by what that maker's other products declare. It costs one
`SELECT DISTINCT` on the empty path, no model call, and no new timed stage: a
microsecond read is not a column a reader needs.

*What a report contains now:* the review reason reads *not compared: this item is
valves, and the corpus for X declares pumps*. `reason_code` was already a column in
the CSV and a field on every stored result, so a report of a run made after this
change counts the category-excluded pairs apart from the silent ones; the sentence
reaches the HTML review flag and the frontend's flag tooltip and evidence drawer
without either of them changing. No status, score, coverage or flag moved: a pair
that was coverage is coverage still.

*Verified:* 2 tests (a pair the rule dropped earns the code and names both
categories; the probe joins the product when the clause names a model). 34 pass in
`test_matching.py` and the full unit suite passes.

*Left:* a database that has not applied `0013` rejects the write, so `make migrate`
comes before the next run; the new constraint has no integration coverage (it needs
`TEST_DATABASE_URL`); and the frontend still shows no `reason_code` column, so a
reader gets the sentence and not the code.


## D-136 - A canonical term written with a space is the same term

The structured fact cache is checked before retrieval and extraction run (FR-21),
and it is keyed by canonical term. A run that verified a fact and could not name
its attribute filed the answer with `attribute_id` null - so the next run missed
the cache and paid for a retrieval, an extraction and a verification it already
had. Four of the twelve verified facts in the local database were in that state,
both of them CIRCOR's, and the reason was a separator: a document writes
"Material Grade" and the vocabulary names the attribute `material_grade`.

`AliasRepo.resolve` already tried the name as written and as a token with every
space stripped; neither is the canonical form of a term that *contains* a space.
One candidate is added - the name lower-cased with its separators turned into
underscores - so a canonical term is recognised in the spelling the vocabulary
writes it in. It is the shared resolver, so all twenty-four attributes gain this
at once rather than twenty-four synonym edits.

One synonym is added alongside it, because it is attested rather than inferred:
`class rating` on `pressure_class`, from the CIRCOR documents that used it.
`pressure rating` is deliberately *not* added: it reads as easily of
`operating_pressure`, and FR-29 forbids guessing which one a clause meant.

*What it will not claim:* nothing here re-files the four facts already stored with
a null attribute. That is a write to the corpus database, which is a data change
to be approved, not a code change; the resolver only stops the next one from
landing in the same place. A term that resolves to no canonical name still goes
to review rather than being invented.

*Verified:* 2 tests (a spaced canonical term resolves to its attribute and needs
no review, and a near-miss plural does not; `class rating` resolves to
`pressure_class`). 11 pass in `test_resolution.py`, 67 across
`test_ingestion.py`/`test_resolution.py`/`test_runs.py`. No model call, no
migration - the alias table is re-seeded from the vocabulary file on the next
ingest.

## D-137 - The catalogue CSV declares a category

`iter_csv_corpus` wrote `"category": None` for every row of the hand-gathered
catalogue, so those documents could never be filtered by category (FR-17): a
valve requirement retrieved pump and motor documents and paid to read them, and
the D-135 probe could not say what the category rule had excluded, because a
whole half of the corpus declared nothing.

The row now carries what the manifest says, casefolded, and an empty field stays
`None`. A blank is not a wrong category: the retrieval filter keeps a document
that declares nothing (D-96), so labelling half the corpus is an improvement and
not a risk to the other half.

*What it will not claim:* the labels themselves. `data/catalogue/manifest.csv`
is open in another program on this machine and will not accept a write, so the
column is read and tested - against a synthetic manifest in a temporary
directory - while the 27 labels prepared for it are still staged. They come from
`collect/sources/backfill_notes.txt`, the collector's own notes, which state a
category per manufacturer block: nine Valmatic drawings and check-valve
whitepapers, six Sulzer pump-program and submersible-pump files, five Wilo data
sheets, seven Bharat Bijlee motor catalogues. The other 32 rows stay blank
because the notes do not say: Delval Flow's 25 are named by neither, and the
ISO certificate, the privacy policy, the DC brochure (their notes put it under
/drives) and the transformer brochure are not product documents of a stated
category. A category that would have to be inferred from a file name is not
recorded here; that is retrieval's job (D-100).

*Verified:* 1 test (a CSV row that declares `Valves` is stored as `valves`; a row
that declares nothing is stored as no category). 12 pass in
`test_ingestion.py`.

## D-138 - A manufacturer is looked up by identity, not by spelling

One company reaches the corpus under two spellings: a catalogue directory is
`delval_flow`, its manifest says "Delval Flow". `get_manufacturer_id` matched on
the name alone, so the second spelling became a second row, and a corpus split
across two manufacturers reports half of its documents as if they were the
whole of it - the run that names one never retrieves the other's.

The fold is one shared read, in `corpus.manufacturer_id`: an exact name is the
answer, and a name that is absent is tried again with its separators ignored,
using `normalise_token` - the comparison engine's own normaliser, so the corpus
does not grow a second idea of what "the same word" means. `PostgresDocumentRepo`
now delegates instead of repeating that query.

*What it will not claim:* a merge. Two rows that already differ only by spelling
stay two rows: joining them is a write to `manufacturer`, `document` and every
`result` that points at them, which is a data change to approve, not a default.
While both spellings exist, each is found by its exact name and the fold lands on
two rows, so it resolves neither - which is the same rule the rest of the project
uses for ambiguity (FR-16), and the reason this change never silently picks the
half a run should be scored against. What it does change is the next ingest: a
differently-spelled directory joins the manufacturer it means instead of starting
a rival.

*Verified:* 2 tests (a `delval_flow` ingest returns the id of the row stored as
"Delval Flow" and writes no new row; a fold that lands on two rows returns None).
67 pass across the three ingestion, resolution and run files. The database-backed
`test_ingestion_db.py` is not run here - it needs `TEST_DATABASE_URL`.

## D-139 - A clause on a mixed page is read on its own words

A `mixed` page is read as a whole, so its page tag cannot separate the buyer's
specification from the forms stapled to it. The re-typing step only asked
"was this page a specification?", which is true for the whole page - and a
GeM tender with no specification in it at all produced four `technical`
requirements, every one of them an undertaking to be signed on company
letterhead.

The clause is now read on its own words. `classify.reads_as_legal_clause`
scores the clause with the same weighted term lists that classify a page, and
requires two weighted non-technical hits before it calls a clause an
obligation - one incidental commercial word ("contract", "warranty") is a rider
on a specification, not a commercial clause. A tie goes to the obligation,
because the words that tie are the ones the two families share: "head" is a
pump's head and a letter head, and `head` is weight 2 in the technical list.
Two terms were added for the forms themselves - `letter head`/`letterhead` to
the boilerplate list, `undertaking`, `eligibility criteria` and `tenderer` to
the commercial list.

*What it changes in a report:* a pair built on such a clause no longer exists.
On the tender that exposed this, the four clauses it re-typed were the only
four requirements it assessed, so the run reported 8 results (4 x 2
manufacturers) over four legal sentences; it now reports none, and the clauses
are recorded as unread. That is the honest count: the tender states no
capability for a manufacturer to meet. The page is still read, and a real
specification on the same page is still re-typed - only the clause-level gate
is new.

*What it does not claim:* it does not decide that the tender is not technical.
It only refuses to call an obligation a specification. The two specification
lines this tender does state were captured as BOQ items rather than as
requirements, which is a separate defect and is not touched here.

*Verified:* 35 tests in `test_pageclassify.py` and `test_retyping.py`, including
the four shapes above and a specification clause that must stay technical; the
full unit suite (664), `collect` (15) and `eval` (52) pass.

## D-140 - A manufacturer is a company, not the folder a copy sits in

Two rows of the same company, `bharat_bijlee` and `Bharat Bijlee`, had been
measured and refused by D-138 rather than guessed at. The refusal was right and the
data was still split: 10 documents, 1468 chunks, 10 products, 55 facts and 172
results answered to the catalogue's directory slug while 6 documents and 731 chunks
answered to the name the manufacturer's own manifest states. A run naming either
one scored against half a corpus, and the report showed the same company twice.

Migration `0014` joins them on the attested name - what a reader of a report should
see - and moves `document`, `product` and `result` before dropping the emptied row.
It does nothing when a database has only one spelling, so it is safe on a fresh
clone, and it *refuses* when the two rows hold the same file hash or the same model:
`uq_document_manufacturer_sha256` and `uq_product_manufacturer_model` make a merge
of those a choice about which chunks, facts and results survive, which is a human
call. The local database had no such collision, so the merge ran.

The second half is the bug that made rows like this. An unlisted corpus document
took its manufacturer from `file_path.parent.name`, and a catalogue keeps the copies
it fetched in `<manufacturer>/raw/` - so those files were attributed to a company
named **raw**. That row is still in the database with no documents and 128 results,
because a run was once made against it: 122 clauses it could not type and 6 with
nothing to read. `parse.py` now names the catalogue's own directory for the file,
and re-ingesting after this change created no `circor`, `emerson-fisher` or
`xylem-godwin` row: with the D-138 fold those directory names resolve to CIRCOR,
Emerson Fisher and Xylem Godwin, so the document joins the manufacturer it belongs
to instead of founding a new one.

*What it will not claim:* the `raw` row was not deleted, and neither were the 128
results. They are the record of four runs that were made, and dropping the
manufacturer means dropping the history that says so - a different decision, asked
for after the measurement, not before it. Historical runs also keep the name they
were run under in their own `config_snapshot`, which no migration rewrites: what a
run was scored against stays what it was.

*Verified:* run against the live database. 20 manufacturers to 19; one `Bharat
Bijlee` row holding 16 documents, 16 products and 172 results; `manufacturer_id`
for `bharat_bijlee` now returns that row. A re-ingest read 101 documents, created
none, failed none, and refreshed 5 rows of metadata - which is why motor-labelled
documents went from 13 to 18, the CSV's own labels finally reaching the half of the
company that used to sit under the other spelling. `test_ingestion.py` and
`test_corpusrestore.py` pass 34; `test_migrations.py` skips without
`TEST_DATABASE_URL`, so `0014` is covered by this run and not by an automated test.

*Left:* the `raw` row and its 128 results, and four past runs whose reports list a
manufacturer that owns nothing.

## D-141 - A bill of quantities row that states a clause is the clause's item

A buyer can write the obligation into the bill of quantities instead of naming
a thing there: "The connector shall have LED indication for command ON" is a
row of the BOQ, not a sentence of the specification. The ingestion stored such
a row as a `tender_item` and nothing else, so no requirement pointed at it, no
comparison could run, and the report could not even say it had been skipped -
the clause was simply absent from the count. Measured on the GeM tender: 2
items, 77 requirements, and **0 requirements linked to any item**.

The requirements stage now asks each item whether its own words state an
obligation (`shall`, `should`, `must`) and, when they do, emits one requirement
pointing at that item, before the atomizer sees the list. A clause the
specification already stated for the same item is not stated twice. The type is
left `unknown` on purpose: the wording proves it is a clause, not that it is
technical, and the reader decides that.

*What it changes in a report:* two rows that were invisible now appear - and
appear as what they are. On the GeM tender the warranty clause is left
unmatched (commercial, PRD 4), and the LED-indication clause is left
`attribute_not_mapped`, because no attribute in the vocabulary describes an
indication. **The pair count does not move: it was 0 and it stays 0.** The
change is that the report now names two requirements it had silently dropped,
which is the difference between "nothing to assess" and "we did not look".

*What it does not claim:* it does not make this tender matchable. A compliance
document has no capability for a manufacturer to meet, and inventing an
attribute for one would be inventing a fact.

*Verified:* 3 tests in `test_tenderingest.py` (a clause row becomes a
requirement of itself, a bare item name stays an item, `should` counts as an
obligation), 56 in that file and `test_pipeline.py`, and the full unit suite
(667) passes.

## D-142 - The manufacturer picker is grouped by the category the tender's items name

Uploading a valve specification listed all 19 manufacturers of the corpus as one
undifferentiated grid, with motors and pumps mixed in among the valve makers, and
nothing on the screen said which ones the tender was about. The `tender` row does
carry a `category`, but every tender in the database stores it as `unknown`, so it
cannot drive anything; the real category is on the **items**, which is where FR-16
puts it.

The page already holds the requirement list, and each requirement ships with its
item's category. So the grouping is read from what has been read, not stored a
second time and not asked of a model: the majority category of the tender's
requirements is the tender's category; items that name none, or two equally, leave
the picker exactly as it was. When there is one, the list is grouped - the tender's
own category first, the other declared categories alphabetically, and "No category
recorded" last - with an `Only valves (8)` link that selects that group and nothing
else.

*What it does not change:* nothing is hidden (every manufacturer is still on
screen, in a group), the default selection is still the whole corpus, and no
comparison uses the category. The grouping is a way to find the vendors a tender
is likely to be about, not a claim that the rest cannot supply it, and it is not
the per-item category the matcher filters on. `delval_flow`, whose 25 documents
declare no category, stays visible under its own heading rather than being dropped
for answering a question it was never asked.

*Verified:* 2 tests in `UploadPage.test.tsx` (a valves tender groups the corpus and
`Only valves (1)` selects the valve maker alone; a tender whose items name no
category keeps the flat list and offers no narrowing), 13 in that file, 33 frontend
tests, `tsc -b` and `oxlint` clean.

## D-143 - A frozen baseline is checked by document hash, page and quote

The speed-ups are diffed against `eval/baseline/`, so the baseline has to survive
a re-ingest. It did not: every evidence block in the report names a `fact_id` and
a `document_id`, and re-reading the same corpus replaces both rows. A diff against
those ids compares nothing.

`eval/harness.py cite` therefore keys a claim by **(document sha256, page,
normalised quote)** - the three things that outlive a re-ingest - re-checks it
with the product's own `check_citation` against the PDF page text on disk, and
writes a copy of the report with the two ids removed. Claims are deduped by that
key and counted, so one sentence cited under five requirements is one claim with
`predictions: 5` and cannot inflate or hide the rate. `score` is left as it was:
it checks only the labelled pairs, which is the number a gold file can support.

*Measured on the baseline, no model call:* 16 distinct claims over 27 predictions -
6 verified, 10 failed, **correctness 0.375**. All 10 failures are Val-Matic
(`VMC-4702AAL-AAW_11-18-14.pdf` p1, `Series-51-53-v5b` p3 and p6,
`Series-58-2022-M2` p3) and all 10 read `the quote is longer than the cited page`.
Those pages return **0 characters in PyMuPDF and in pdfplumber alike**; the quoted
text exists only in chunks stored with `ocr = true`. So this is item-by-item
evidence, not a wrong answer: the quote is real and the checker is blind to an OCR
page, which is what the Val-Matic diagnosis now has to fix.

*What it does not change:* no product code and no report contents; the files
already in `eval/baseline/` were not edited, and `report-4e7ebbc1.json` is kept as
the pre-freeze original next to the frozen copy.

*Verified:* 2 new tests in `eval/tests/test_harness.py` (every claim checked and
the ids gone in the frozen copy; a document listed only in `catalogue/manifest.csv`
is still indexed - before that fix 12 of 16 claims were silently skipped as
`unavailable`), 19 in that file, and the full unit suite.

## D-144 - A claim the verifier did not confirm is never published unflagged

The baseline's single false-confident pair is `6ffe6648…` (Three piece ball valves
2" Screwed end ASME class 150, face to face 122mm) × valmatic: gold says
`missing_capability`, the run published `no_evidence_found` with
**`review_required = false`, no `review_reason`, and `verification_outcome =
not_checked`**. A reader sees a finished answer where the system gave up.

Measured on run `4e7ebbc1`: **36 of its 63 results have that exact shape** -
silence, nothing checked, nothing flagged. They are all one branch.
`_match_candidate` has five ways to end a pair as silence and four of them flag it
(no candidates, no passage stated a capability, the extraction failed, the
verifier rejected the quote). The fifth - the model proposed no fact *and* the
deterministic listed-set check had nothing citable - set only `reason`, and
`reason` has no column: the stored row carried no flag and no reason at all. It
even wrote the placeholder fact with `verification_status = needs_review`
(`match.py:741`), so the corpus said "review this" while the result said
"answered".

Why that pair took the branch is the same story as D-143: the cited Val-Matic page
1 (`VMC-4702ABL-ABW_11-18-14.pdf`) has no text layer, so `check_citation` cannot
confirm a quote read from its OCR chunk, and an unconfirmed set-exclusion falls
through to silence.

The branch now flags, with the reason its siblings use
(`the passage does not state this capability`). PRD 4 already says what
*no evidence found* is: "neither pass nor fail, **flagged for review**, reported
as coverage". An unflagged silence is the one result a reader believes.

*What it changes in a report:* the flag, nothing else. On the baseline run,
flagged results go from 24 of 63 to 60; the 3 left unflagged are quotes the
verifier passed. Statuses, scores, the denominator and the coverage count do not
move, and the review queue and `review_count` grow accordingly - that is the cost,
and it is the price of the PRD's own definition. The baseline's one
wrong-and-unflagged pair is this path, so closing it removes its only
false-confident row (arithmetic over the frozen report, not a re-run).

*Verified:* 2 assertions added to `test_matching.py` (`test_a_silent_passage_stays
_no_evidence_found` now pins the outcome, the flag and the exact reason;
`test_a_passage_that_states_nothing_is_no_evidence_found`, the same branch reached
through a `found: false` extraction), 34 in that file, full unit suite.

## D-145 - An OCR page is checked against the text that page was chunked from

`_page_text` read the PDF's text layer and nothing else. A scanned page has no
text layer, so the citation layer compared the quote with `""`, reported **the
quote is longer than the cited page**, and failed. The verdict looked like a
problem with the quote; it was a hole in the check, and it made **every image-only
page in the corpus unverifiable by construction** - the Val-Matic datasheets are
image-only, which is why the whole Val-Matic column came back silent.

Measured over the frozen baseline (D-143): 10 of its 16 distinct claims failed with
that one reason, all on pages that return 0 characters in PyMuPDF *and* pdfplumber
alike. Reading the page from the chunks it was cut into fixes 8 of them
(`the quote appears verbatim on the cited page`), and 2 are now quoted from text
that the re-ingested corpus does not hold on any page of those documents -
`150 | 280 | 140` in `Series-58-2022-M2`, `8 | 0.71 | 2.50 | 4.00` in
`Series-51-53-v5b`. Those two are the run quoting an older version of the corpus,
not a second failure of this fix; the baseline keeps them as they were measured.

The fallback is **page-level, not passage-level**: it joins every chunk stored for
that document and page, so a quote still has to appear somewhere on the page and
not merely inside the passage the model was sent. Verification is not weakened -
for a page that never had a text layer it is the only form the check could take,
and it is the same text the corpus was indexed from.

*What it changes in a report:* claims quoted from scanned pages can now pass layer
1 and be published as full/partial/missing instead of dying as silence. Nothing
that verified before changes.

*Known limit:* chunks carry no ordinal, so the page is rebuilt in `id` order and a
quote that spans two chunks will not be found. `eval/harness.py cite` still checks
the text layer only - it is deliberately database-free, so on a scanned page it is
stricter than the product, by design.

*Verified:* 1 test in `test_matching.py` (a page with no text layer is read from
its chunks, both chunk texts present in the page), 35 in that file, and the
measurement above against the stored corpus - 0 model calls.

## D-146 - A specification cell is read as the lines the buyer wrote

A table cell in a GeM specification states one parameter per line:

    Three piece ball valves 2"
    Screwed end
    ASME class 150
    Cast carbon steel
    Face to face distance 122mm

`_table_rows` flattened each cell with `" ".join(...)`, so the five parameters
became one 120-character clause. As one clause it parsed no attribute at all - the
atomizer sees a string naming a product, a thread, a pressure class, a material and
a dimension, and none of them is a sentence. `_clean_cell` stayed for the cells that
are genuinely one line (number, quantity, unit); a new `_clean_lines` keeps the
breaks, and `_table_rows` emits **one requirement row per line of the description
cell**, each carrying the row's own provenance - same page, same item number, same
section - so `item_id` is still preserved end to end.

A line break inside a cell is not the same thing as a line break in prose, which is
why this does not contradict D-88. In a prose page a break is a wrap, and the lines
that do not end a sentence are joined before anything is split. In a ruled cell the
buyer chose where the line ended; the grid is what tells the two apart, and
`extraction_method` carries that choice downstream.

The second half of the fix is in `drop_fragments`. Splitting the cell exposed lines
like `Cast carbon steel` - a material, no number, no verb - and the prose rules
called them fragments and dropped them. A dropped requirement is not reported as
unverified; it is reported as absent from the tender, which is a different claim
than the one the document makes. So a clause whose method is `table` or `item` is
never dropped: it came out of a grid, and its lines *are* the parameters. The merge
attempt still runs first, so a cell whose text wrapped mid-phrase (pdfplumber can
break `Gate wedged valve non rising stem` after `rising`) still rejoins.

*Measured on the gold tender's specification (0 model calls):* 48 requirement rows
from the 2 table pages where the flattened read gave 11 - 37 clauses that had been
buried inside a cell. Assigned 48, unassigned 0, merged 3, kept 45, **dropped 0
where the same read before this change dropped 15**, and all 15 were real
parameters (`Cast carbon steel`, `ASME class 150`, `Face to face distance 76`). Item
10 now reads as the five clauses above instead of one unparseable string.

*What it changes in a report:* the requirement count for a tender whose
specification is a ruled table goes up - on the gold tender, 11 rows of table
clauses become 45 - so every denominator, coverage percentage and per-item count in
a re-run report moves. That is the intended effect: the clauses were always in the
document, and the earlier report understated the tender. A run before this change
and a run after it are not comparable row-for-row, and no stored run was rewritten
to pretend otherwise.

*Known limit:* the split trusts the grid. A cell whose lines were set by the
typesetter rather than the buyer (a wrapped sentence inside a wide cell) becomes two
clauses, and the merge attempt only rejoins them when the second starts in lowercase
and the first does not end a sentence. *Upgrade when:* the page stores the cell's
own box width, so a line that fills it can be recognized as a wrap.

*Verified:* 2 new tests in `test_tenderingest.py` - a cell written over three lines
becomes three rows sharing number, page and method, and a table-method line that the
prose rules call a fragment is kept while a clause number is still dropped - 26 in
that file. Three tests in `test_fragments.py` failed first, because their fixture
built every clause with `RequirementRecord`'s default method, which is `table`: the
prose cases now say `text`, which is what they test. Production always names the
method from the row it read, so nothing there was mislabelled.

## D-147 - A clause that parsed no attribute says so instead of looking silent

`assessable()` already keeps an *unknown-type* clause out of a run: a row for it
would read as a result and turn "we could not read this" into "we checked and found
nothing" (D-92). The clause this decision is about is different - it **was** typed
technical, and nothing in it became an attribute. After D-146 a specification cell
like `Cast carbon steel` reaches the run as its own requirement: technical, real,
and with no canonical attribute to compare.

Such a pair ran, retrieved what it could, and was stored as *no evidence found* with
no reason code. A row with no code is what the report reads as a finding about the
product - "the catalogue is silent about this" (that is exactly what D-101 built the
code column to keep apart). It is not a finding about the product. Nothing was
compared, because the tender side of the comparison was empty. So `_assess` names it:

    reason_code = requirement_not_parsed, verification_outcome = not_checked,
    review_required = true, review_reason = "requirement_not_parsed: the clause
    named no attribute to compare"

The status stays *no evidence found*, because the product was never checked - that is
PRD 4's definition, and AGENTS #4 forbids reading absence of evidence as absence of
capability. The code goes in its own column, which is what the code column exists for
(D-86, D-101). Nothing that reached a verdict is touched: the gate only fires on a row
that is already *no evidence found*, and it never overwrites a code the comparison
earned, because a clause that did name an attribute is not this case.

It is not the same as `attribute_not_mapped`. That code comes out of the registry
when a canonical attribute **was** named and no evaluator is registered for it - the
comparison was asked and could not run. This one fires before that: no attribute, so
no evaluator was ever chosen. And it is not `unsupported_requirement`, which PRD 4
reserves for a clause no document could answer; a material clause could be answered,
by us and by a datasheet, if only the clause had been parsed.

*Measured (read-only, 0 model calls):* 25 of the 285 *no evidence found* rows stored
in the dev database belong to a technical or certification clause whose `attribute`
is empty. Each of them currently publishes as "the corpus is silent".

*Schema:* `reason_code` is a named CHECK on `result`, and 0013 is the pattern for
widening it - drop the constraint, recreate it with the longer list. `0016_
requirement_not_parsed` does that. `app/repositories/schema.py` needs no edit: it
declares the column as `_enum(ReasonCode, "reason_code")`, so the list it builds comes
from the enum this change adds the member to. The migration chains on `0015`, which
was head when it was written.

*What it changes in a report:* the count under each reason code gains a column value,
and 25 rows of the dev database's history would read differently on a re-run - still
*no evidence found*, still excluded from coverage as an unchecked pair, but now filed
as "we did not parse the clause" instead of "the documents do not state it". A report
of a tender whose specification is a ruled table therefore stops attributing our
parse gaps to the manufacturers. **The code is inert until the migration is applied**
- on a database still holding 0013's constraint, storing the new value fails the
CHECK, so the migration must land before a run writes this code.

*Verified:* 2 new tests in `test_runs.py` - a technical clause with no attribute
stores `requirement_not_parsed`, is flagged for review and keeps the *no evidence
found* status, and a clause that did parse an attribute keeps the `category_mismatch`
code it earned - and the fake store now records `reason_code`, which it did not
before, so no test could see that column at all. The full unit suite passes.

## D-148 - The corpus declares each document's role and type, from data

`_type_filter` compares the product type a requirement needs with the type the
candidate's product declares, and when the document never said, the evidence is
family-level: the score is capped and the pair is flagged for review (PRD 4,
D-100). **The type column has never been written.** Measured in the dev database:
`product.product_type` is set on 0 of 99 products, `product.classification_source`
is NULL on 99, and `document.doc_role` is NULL on 101 documents - so the filter had
nothing to compare, and every pair in every run was answered as if its datasheet were
a family brochure. That is the shape the pasted corpus audit reported for the Valtek
pairs - `is_family_level` false on the product, `family_level` on the result - which
is what a missing declaration looks like from the report side.

`app/services/corpus/classify.py` fills the declaration from data, and asks no model.
It writes no vocabulary of its own:

- the **type** comes from the file the item side already uses. `document_type_of`
  calls `match.category.document_type` (and `infer_category` when the product has no
  category), so a document and a requirement are placed in one vocabulary
  (`data/categories.json`) and adding a type stays a data edit (FR-6);
- the **role** comes from a new `data/doc_roles.json`, read the same way
  `categories.json` is: a declared name and the terms that name it. A manifest's own
  `doc_type` is honoured first (`specification` -> `datasheet`,
  `compliance_schedule` -> `certificate`); only a document whose manifest stayed
  silent or wrote `other` is read from its words. Of the 121 manifest rows the corpus
  carries, 41 declare `other`, which is where the words matter.

What is read is the file name, the declared model or family, and the first two pages
(`--pages`). A rule never overwrites a value that is already there, because it may be
a person's decision - the update is `WHERE product_type IS NULL`. A document whose
words name two roles equally, or two types equally, is **left unresolved rather than
coin-flipped** (FR-T9), and unresolved rows are exported to
`eval/product_types_todo.csv` with the manufacturer, the product, the document, a
snippet of the evidence the rules saw, and the answer column blank. `--load` reads a
filled CSV back and records `classification_source = 'human'`, which is the third
value of the enum the schema declares: `manifest` for what the collector said, `rule`
for what this module read, `human` for what a reviewer typed.

*Deliberately not in this change:* it does **not** write `product.category` (54 of 99
products have none, and a category is what makes a type reachable by retrieval - that
is D-92's ground and another session's Commit 2), it does **not** unlink the
certificate and privacy-policy documents from their products, and it does not exclude
any document from retrieval. Those change which pairs exist and what a report counts;
they are separate commits so each can be kept or reverted on its own. The pass has not
been run: the dev database's container stopped while this was written, so no count of
how many products get a type is claimed here.

*Verified:* 9 tests in `tests/test_classify.py` - the type is read from a document's
own words, a product with no category is placed from them, a document that names two
types equally is not guessed, the declared `doc_type` wins, a certificate and a
privacy policy are named from their words, a document naming no role stays
unresolved, an unreadable vocabulary file classifies nothing instead of failing, and
the reviewer's CSV gets one row per undecided document. `test_no_hardcoding` passes:
no corpus file name or URL is in `app/`. The full unit suite passes.

## D-149 - A document's type is declared only where its category is already known

The pass was run against the dev corpus once the database was back up: 101 documents
read, 74 given a `doc_role`, 36 products given a `product_type`, all of it marked
`classification_source = 'rule'` and reversible by that one predicate.

Checking those 36 against what the corpus itself calls each product found a defect in
D-148's own rule. **20 of the 36 sat on documents whose product names no category at
all**, so the pass had to invent a category from the same opening pages it read the
type from. The Delval and DelTorq actuator bulletins are the case: their first page
describes the valve the actuator mounts, so `infer_category` called the document a
valve, `infer_type` then called it `butterfly`, and a pneumatic actuator acquired a
valve type. One stray word on page 1 placed a whole product.

A wrong type costs more than no type. Retrieval drops every chunk of a document that
*declares* a different type (D-107), and a dropped chunk is not reported as a dropped
chunk - the pair comes back `no_evidence_found`, which reads like the manufacturer's
silence and is our own exclusion (D-135 says exactly this about the category rule). So
the guess was not conservative; it was a quiet false negative.

Two rules fix it, both in the pass rather than in the shared vocabulary:

- `document_type_of` takes a type **only inside a category the corpus row already
  names**. A blank category is unresolved, not a hole to fill from a 2,000-character
  excerpt - which is the same reasoning FR-T9 applies to a BOQ item.
- `classify_corpus` refuses a page-word type that **contradicts the document's own
  name**. The pages may fill a blank the cover left (`Valtek GS` names no type and is
  now `control`); they may not overrule it (`Control valves` was coming back `ball`).

The data was repaired rather than layered on: the 36 rule-written types were cleared,
the pass re-run (21), the guard added, re-run again (20). The actuator bulletins and
the industry brochures went back into the reviewer's CSV, which is now 80 rows.

*Still wrong, and why it is left:* two products that list several types in their own
name - "Gate, butterfly and check valves (Series 06/71/820)" and "Valve product
portfolio (butterfly, ball, gate, ...)" - come out `check`. The vocabulary matches
"check valve" as a phrase, so a comma list of bare type words matches only the one that
happens to appear in phrase form, and the longest-match rule then calls that the
answer. Making the rule refuse those means counting types named in a list, which is a
new matching job in shared code and is not what an excerpt can settle anyway. They are
two rows for the reviewer's CSV; `--load` writes them as `human`.

*What this pass changed in a run: nothing yet.* No code reads `product.product_type`
or `document.doc_role` - `_type_filter` still infers the type from the model, family
and file name on every run. Measured over the 42 documents under a typed product, that
name-only check reaches the same answer as the stored type for 17, says nothing at all
for 24, and disagrees for 1. The 24 are where today's `family_level` cap comes from, so
wiring the column would convert them to a confirmed type - and would exclude evidence
wrongly for every mis-typed row, which is why it is its own commit with the Valtek GS
pair read by hand first, not a follow-on edit here.

*Verified:* 9 tests in `tests/test_classify.py`, one of them rewritten - what used to
assert "a product with no category is placed from its document's words" now asserts
the opposite, and that the unknown sentinel is no category either. `test_corpusseed`,
`test_categories`, `test_retrieval` and `test_no_hardcoding` pass unchanged, and so
does the full unit suite. `--out` now defaults to the repository's own `eval/` instead
of a directory created under whatever the working directory happened to be.

## D-150 - The classification columns land as one reviewed migration

`0015_product_type_doc_role` was written and applied to the dev database by the session
that planned the corpus pass, but it sat uncommitted while D-148 and D-149 were built on
top of it. It is committed as it stands, with the schema and the enums that describe it:

- `product.product_type` (varchar 64), `product.classification_source` (varchar 32,
  CHECK `manifest | rule | human`), `product.confidence` (float);
- `document.doc_role` (varchar 32, CHECK over the seven roles of D-148).

The three columns are nullable with no default, because a corpus row that has not been
classified says nothing, and a rule that guessed would be read as evidence.
`classification_source` is the field that keeps the three ways a value can arrive apart
- what the collector wrote in the manifest, what `classify.py` read from the document,
and what a reviewer typed into the CSV - and it is the predicate the repair in D-149
could be run under. `repositories/schema.py` declares both enum columns through `_enum`,
so they are VARCHAR + a named CHECK rather than a native Postgres enum, which is what
the migration actually created.

*Not tested:* the CHECK value lists and the enum members are kept in step by hand, the
same way 0013 and 0016 do it. A test comparing them would freeze a historical migration
whose lists are meant to be widened by the next one.

*Verified:* the full unit suite, `test_no_hardcoding`, and `alembic current` on the dev
database, which was already at `0015_product_type_doc_role` before this commit and is
unchanged by it.

## D-151 - A product family is entered only from the document that names it

`data/categories.json` gains the families the corpus actually carries, so a page that
never says "butterfly valve" can still be placed by the family on its cover (D-100):
the eight Delval butterfly series, the DelVal ball series, the IVC/IVCT wafer check
valves, the butterfly and check term variants the bulletins use (`bfv`, `hpbfv`,
"swing check valve", "wafer check valve"), and a `knife_edge` type.

Every entry was checked against the document itself - opened locally, first three pages,
no model asked. `1692721738-...SERIES-40-41-CENTRIC-BFV.pdf` says "Product Specification
for Industrial Split Body Centric Butterfly Valves"; `...SERIES-65-72-BLV-R1` says
"Solid, One-Piece Ball". Two proposed families were **rejected on that evidence**:

- `Series 21pt` was listed under ball valves. The document is the DelTorq bulletin, and
  its own cover reads "Atuadores Pneumáticos de Pinhão e Cremalheira SÉRIE 21" - a
  pneumatic rack-and-pinion actuator.
- `Series 83` was listed under ball valves. The file is
  `Automation_Series-83-Valve-Position-Monitor.pdf`, and the matching technical bulletin
  names no valve type at all. It is a monitoring accessory.

Both are the failure D-149 was about, arriving through the data file instead of the code:
a family entered from a brand's naming scheme rather than from what the document
declares. A wrong family is worse than a missing one, because `document_type()` trusts
families exactly as much as terms, and retrieval drops every chunk of a document that
declares a different type (D-107).

*Not added:* there is no `actuators` category in the vocabulary, so DelTorq's actuator
bulletins stay unplaced by type. That is a category to add with its own terms and its own
evidence, not a place to park the two families removed here.

*Verified:* `test_categories`, `test_classify`, `test_corpusseed`, `test_retrieval`,
`test_no_hardcoding` and the full unit suite pass; the JSON parses and the trailing
newline the editor dropped is restored.

## D-152 - `unknown` is not a vote in the frontend's category pick

`tenderCategory()` in `frontend/src/components/UploadPage.tsx` counts the categories the
extracted requirements carry and takes the most common one to decide which manufacturers
to pre-select. An item whose words named no category stores the sentinel `unknown`
(FR-16), and the vote counted it as if it were a category of its own: a tender of mostly
unclassifiable items could pre-select on `unknown`, which matches no manufacturer. It is
skipped now, the same way an empty category is skipped.

The backend is unchanged and stays that way: a run still answers every selected
manufacturer, and a pair the category rule excluded is reported with
`ReasonCode.category_mismatch` (D-135) rather than dropped. This is a default in a
checkbox list, not a filter on what gets assessed.

*Verified:* `npx tsc --noEmit` and the frontend tests; the change is three lines in one
function that no backend path reads.

## D-153 - A role that removes evidence is read from the cover, and a policy is not evidence

The corpus holds documents that are not about a product at all - one privacy policy, two
quality certificates - and retrieval reads any chunk, so a requirement whose words meet a
policy page gets that page back as evidence. `data/doc_roles.json` now carries the
decision itself:

- `"retrieval_excluded": ["policy"]`, read by the new `excluded_roles()`, applied by
  `_role_filter()` in `retrieve.py` to both `keyword_search` and `semantic_search` next to
  the category filter. It is `doc_role IS NULL OR doc_role NOT IN (…)` because 28 of the
  101 documents have no role yet: silence is not a policy (PRD 4), and the bare inequality
  would drop their 3,217 chunks and report the gap as "no evidence found".
- `"from_name_only": true` on `certificate` and `policy`. A role that takes a document out
  of the evidence is accepted only from the manifest or from what the document calls
  itself, never from a paragraph. Measured reason: one valve datasheet has the
  specification line "fire safe certified: api 607", and that word labelled it a
  certificate. Descriptive roles - datasheet, catalogue, brochure - cost little when wrong,
  so their pages still name them.

A certificate stays searchable: it is evidence for a certification requirement (PRD 4), so
it is deliberately not on the excluded list.

*Data applied to the live corpus* (no rows deleted): 73 of 101 documents carry a role
(datasheet 31, catalogue 29, brochure 8, policy 1, certificate 2, white paper 2), and the
two documents that were linked to a product invented from their own file name - the privacy
policy and an ISO certificate - now have `product_id = NULL`. `raw/PED-Module-H-Ball-Valve.pdf`
is still linked: its product is a real model name, and unlinking it would have left a
ball-valve product with no document at all. The `from_name_only` rule changed exactly one
row, from certificate to no role - a datasheet whose cover names no descriptive word is left
for the reviewer rather than guessed.

*Durability gate:* `classify_corpus` now writes only a product whose `classification_source`
is empty. Without it the next run re-typed the two multi-type portfolio products that were
blanked by hand, because the pass fills any NULL; 18 products carry a type, all of them from
`rule`. A row blanked without a source is still re-fillable - a reviewer who wants a blank to
stick records `human` with no type.

*Not added:* no settings entry and no CLI flag for the excluded roles; the vocabulary file
is already the setting. `product.product_type` is still not wired into `_type_filter` (held
for the reviewed list, D-150).

*Verified:* `test_classify` (11), `test_retrieval` (24), `test_no_hardcoding` (4) and the
full unit suite pass. Against the live corpus, `keyword_search("privacy policy data
processing")` returned the policy's chunks before `_role_filter` and not after, with the
same 50-candidate budget.

## D-154 - A listed set is decided in the comparison layer, and a dense page is left silent

The rule "the document lists the values it offers, and the required one is not among
them" existed in one place only: `verifier.listed_set_exclusion`, a fallback that reads a
*page* when the extractor returned no claim (D-103). Two things were wrong with that, and
both were measured before this change.

*The page is not line-structured.* No corpus chunk carries a line break: 0 of 4,000
sampled chunks contain one, so a page arrives as a single line of 2,012 characters. The
old reader searched for the attribute word wherever it appeared, took everything after the
next `:` as the list, and dropped any part longer than 60 characters - so a dense
specification page became "the values the document offers", the whole page became the
quote, and the answer was a confident `missing_capability`. It fired on 2 of those 4,000
chunks against the stored requirements: a contents entry (`8.5.4.2 Connections:
Plug-to-Stem, Ball-to-Shaft.....186`) and a temperature table. It now fires on none: the
attribute name has to be the *label* of the line (what precedes its first `:` or `=`), the
line has to fit `_MAX_LIST_LINE_CHARS` (160), and a dotted leader is refused as contents
rather than as a value.

*The value is where the list is real.* An extracted value does carry the list, so the
decision moved to `evaluate_categorical`: the offered text is split by
`attrs.split_values`, membership decides Full, and an absent required value returns
`missing_capability` naming the values it read. `attrs.split_values` is the one splitter -
the page reader and the comparison now use the same rule for what a list is, and the
verifier's own copy is gone.

*Alias data, not alias code.* A new `value_groups` block in
`data/attribute_vocabulary.json` lists the categorical values that are one thing written
two ways - `threaded` holds "threaded", "screwed", "thread" and each with and without
"end" - because the corpus writes "Threaded" (97 times across the chunks) where the tender
writes "Screwed end". The group is consulted *before* the substring rule in the
comparison: a substring would read "dn50" inside "dn500", which is the mistake D-97 exists
to prevent, and the list branch sits *after* the IP rule so a list of IP values keeps its
coverage semantics. `attrs.value_group` answers by the value's whole normalised word, so
"gear operated" is never read as "threaded".

NPT and BSP are deliberately not in one group. They are two thread forms, not two names
for one connection, and grouping them would print a full match on a connection that cannot
be screwed into the pipework the tender describes.
`test_two_thread_forms_are_not_one_value` says so in the suite.

*Consequence for a reviewer:* the page-level fallback is now inert on this corpus - 35
sampled chunks name a connection with `:` and none of them put the label at the head of a
line - so a listed set is only ever decided from an extracted value. Silence from a dense
page is `no_evidence_found`, which is the honest reading (PRD 4); the D-103 true positive
(the page that lists "Flanged end / Butt weld end" mid-page) is unreachable until chunking
keeps line breaks, and is marked `shortcut:` where the guard is.

*Not added:* no value groups for attributes other than the connections named in this
item; no set on the *requirement* side ("flanged or threaded" as one clause); no new
`ValueType` and no registry key for one; no change to chunking, to the report denominator
or to any status meaning.

*Verified:* `test_comparison` (39), `test_verifier` (21), `test_matching`,
`test_normalizer`, `test_resolution` and the full unit suite pass; the counts above are a
direct read of the live corpus, not a run.

## D-155 - The gold pairs are answered from stored facts alone, and the cache has nothing to say

The speed-up was that a verified fact is read instead of a passage (D-133). Whether that
changes what the system *says* about a pair was untested, because testing it meant another
run - 63 pairs, and the calls to make them. `eval/facts_replay.py` tests it at no cost: it
answers the six frozen gold pairs (D-143) from the fact table only, makes no model call and
writes no row.

*How the no-call claim is proved, not promised.* The replay first reads the clause with the
deterministic resolver and looks for a product of that manufacturer holding a **verified**
fact for the attribute named - the same three lookups `_cached_fact` makes. Only then does
it call `match_requirement`, so the comparison, the family-level cap and the review flag are
the ones a run reaches, and the matcher returns before it retrieves. It is handed a client
whose `complete_json` raises, and `MatchResult.llm_calls` is checked after every pair.

| Metric | Baseline run `4e7ebbc1` | This replay |
| --- | --- | --- |
| Status accuracy (6 verifiable labels) | 0.0 | 0.0 |
| Confusion | 4 × missing → no evidence, 2 × partial → no evidence | the same six |
| False-confident rate | 0.1667 | 0.0 |
| Coverage | 0.0 | 0.0 |
| Review rate | 0.8333 | 1.0 |
| Citation correctness, six pairs | null (0 claims) | null (0 claims) |
| Model calls | 155 for the 63 pairs that run made | 0 |

*What the replay says about the cache.* Read from the local database on 2026-11-03: 237
facts, 28 verified, **24 of those carrying an attribute** - which is all the cache can
ever read, because `findings.cached_fact` joins `attribute` - across **5 products**. The
other 209 facts are `needs_review`, and a fact waiting for review is not an answer, which
is D-136's null-attribute problem still on the books (4 verified facts, still 4). So of
the six gold pairs, two (valmatic) have no verified fact for the clause's attribute at all,
and four are answered from a stored fact. The speed-up is real in the only sense it can be
until a run is paid for: the pair is settled with no reading of a page. It is not yet a
speed-up anyone can measure in seconds, because the cache answers 4 of 6 pairs and gets
none of them right.

*The four that die, and why.* All four come back `no_evidence_found` with the same sentence
the baseline recorded for Flowserve: *pressure class comparison needs a class on both
sides*. The cause was read, not reasoned about - `_categorical_value`'s pressure-class
branch uppercases the match and strips its spaces, so the requirement side arrives as
`ASMECLASS150`, and the ordered-class rule reads `Class 150`, `150` and `ASME class 150`
but not that. Measured against a stored `150` fact: those three forms give `full_match`,
`600` gives `missing_capability`, `ASMECLASS150` gives `no_evidence_found`. **Not fixed
here** - the instruction for this item was to measure and stop, and the fix belongs to
whoever decides whether the resolver or the class rule gives way.

*What the 0.0 false-confident rate does not mean.* The replay's silence rows are built by
copying the three fields the matcher sets when no passage answered, so no pair here can
come back confidently wrong by construction. The baseline's 0.1667 went to 0 because D-144
now flags every silence; this replay shows the cache path adds no confident wrongness, and
nothing more.

*Scope, printed with the row.* Retrieval does not run, so a pair is answered from the first
product of that manufacturer holding the attribute: `Series 75/76` answers a clause the gold
took from `Series 7/8`, and Valtek GS answers one from the Worcester document. That is not a
new sloppiness - the frozen baseline's own Flowserve row quotes `Class 150 RF` from
`VLENTB0300_EN_A4.pdf` page 13, which is Valtek GS - but it is why the table names the
model each answer came from. It closes when `product.product_type` reaches the type filter
(held for review, D-150).

*Six pairs is a spot check.* That is the whole labelled set the baseline has: two
requirements × three manufacturers, hand-confirmed. Nothing was tuned against it, and no
accuracy claim is made from it. The finer labels stay where they were, marked unconfirmed:
`eval/gold/proposed-GEM_9671792-items-10-11-atomic.csv` splits the same two cells into 22
rows (`valve_type`, `nominal_size`, `pressure_class`, `face_to_face` × 3 manufacturers),
every one `label_state=proposed, confirmed=no`. It does not yet contain the two attributes
the confirmed labels turn on - the end connection and the body material.

*Not added:* no report JSON is written (the scored summary is), no flag to fall through to
extraction, no second scoring path - the replay calls `harness.score` - and no status,
denominator, cache rule or retrieval filter moved.

*Verified:* 3 new tests in `eval/tests/test_facts_replay.py` (row shape with no database
ids, silence stays flagged, the refused client raises); `pytest eval` 57 pass; the full unit
suite passes. The counts above are read from the live database while another writer was
adding facts to it (221 facts at the first read, 237 at the last), so they are a snapshot of
2026-11-03, and the six-pair outcome did not change between the two reads.


## D-156 - A tender that fell short says which of three ways it did

Every tender that stopped short used to end at the same two answers: `failed` with a
sentence, or `incomplete` with a sentence, and the screen printed the sentence. Three
different problems hide behind that. A tender whose links never included a specification;
a tender whose specification is a scan the OCR could not read; and a tender that was read
page by page and states no requirement. The first is a missing document, the second is a
bad scan, the third is a fact about the tender - and only the third means the document was
actually read and says nothing (PRD 4).

Each now has a key of its own, `TenderReasonCode` (`no_specification_found`,
`specification_unreadable`, `no_requirements_stated`), stored on the tender row in
`reason_code` under the CHECK constraint `tender_reason_code` (migration
`0017_tender_reason_code`, same drop-and-recreate pattern as 0011/0013/0016). The API
sends the key with the requirements payload and `UploadPage` prints its own title and
paragraph for each, instead of one sentence for all three.

*Before any model call.* `pipeline._reads_nothing` opens the chosen specification, reads
page 1's text layer and, when that is under `INGEST_OCR_MIN_CHARS` (40, from the
environment), OCRs that page with tesseract. Only if both come back under the threshold is
the tender called unreadable, and no extraction runs. This is a page of local work instead
of a call per clause on a scan nobody can read. The setting is a threshold, not a promise:
a page whose OCR yields 1,561 characters of noise passes it (measured, below).

*Which attachment is the specification.* `resolve_roles` still tries `boq-csv`/`boq-pdf`
first for the bill of quantities and `boq-pdf`/`spec` first for the specification. Inside
the scanned step - the last resort for both - the file's own path or URL is now a
tie-break: `boq|bom|bill|price|item` for the bill, `spec|technical|annexure|requirement|
tcs` for the specification. A hint never lifts a scan above a real document.

*What was measured, read-only, on 2026-11-04.* Tesseract
`C:\Program Files\Tesseract-OCR\tesseract.EXE`, page 1 of four stored attachments:

| Tender | File | Pages | Text layer | OCR chars | What the text is |
| --- | --- | --- | --- | --- | --- |
| 41aaa025 | `TCVALVE_...JWM-M1A.pdf` | 4 | 0 | 3,723 | a terms-and-conditions page, not a specification |
| 41aaa025 | `drg_valve_...pdf` | 21 | 0 | 1,561 | drawing labels, mostly noise |
| 4e0a9c5f | `1791798951.pdf` | 1 | 0 | 392 | an indent annexure with real specifications |
| 5b68e376 | `specs_3500_...pdf` | 1 | 0 | 1,378 | a readable purchase-specification table |

So the threshold separates the empty case from the non-empty one, and it does **not**
detect noise: the drawing passes at 1,561 characters of garbage. Nothing here measures
whether the text is *useful*; that is what the extraction stage and the verifier are for,
and no model call has been made on tender 41aaa025.

*Verified:* six new `resolve_roles` tests in `test_tenderingest.py`, two OCR-probe tests in
`test_pipeline.py` (a blank page is unreadable, a page of prose is not), reason-code
assertions in `test_tenderupload.py`, a duplicate-settle test in
`test_tender_upload_api.py`, two screen tests in `UploadPage.test.tsx`, and the full unit
suite.


## D-157 - A read with its requirements stored and no stage rows is incomplete, not still reading

`pipeline._persist` writes the requirements first and the stage checkpoints after. A
process killed between the two leaves exactly the shape tender `41aaa025` was found in on
2026-11-04: 15 items, 31 requirements, zero `extraction_batch` rows, and the tender row
still saying `processing`. Nothing was reading it, and `final_state` returned `None` for a
tender with no stage rows - the right answer for a read that never started, the wrong one
here, where the output was already written.

Three things were changed, all of them the same fix reached from different doors:

- `final_state({}, requirements > 0)` is `INCOMPLETE`. The answer was stored and the record
  of reading it was not; nothing will ever finish it, so it is not settled by waiting.
- `recover.settle()` is the per-tender half of `reconcile`, and the upload route asks it
  about a duplicate the database still calls `processing` before deciding not to schedule a
  read. Re-uploading such a tender now repairs it and reads it again instead of returning a
  status that never moves.
- The reason it writes says what happened, so the reviewer is not told the read is still
  running.

The general rule this encodes: a status written by a process is a claim about the present.
When the process is gone, the row is re-derived from what was actually stored, and "the
output exists but the bookkeeping does not" is `incomplete` - never `done`, because no stage
says a page was read in full.

*Verified:* `test_tenderrecovery.py` (the pure decision, plus a database test that
`reconcile` repairs the shape), `test_tender_upload_api.py` (a duplicate left `processing`
with requirements stored is settled to `incomplete` and read again; one with nothing stored
is still left alone).


## D-158 - A product keeps a category only when the vocabulary names it

`seed_corpus` copies a document's `category` onto the product it creates. A document is
sometimes tagged with its manufacturer's slug rather than a product family, and a product
carrying `delval_flow` as its category filters retrieval on a word no BOQ item ever infers
- the product becomes invisible to every requirement except one that happens to say the
slug. `_keep_category` now keeps the value only when `category_names()` knows it; anything
else is stored as `null`, which means exactly what a document with no category means.

Blast radius is documents whose category is not a real name; the local corpus has none
today, so this is insurance rather than a repair, and it adds no query - the vocabulary is
read once per seed run.

*Verified:* `test_corpusseed.py` (6 pass), the new case being a slug that must not survive.

## D-159 - `/health` answers at the root as well as under `/api`

A container healthcheck and a load balancer probe `/health`, not `/api/health`. The same
handler is registered on a second, prefix-free router, so a probe of a service that is up
gets 200 instead of 404 - and a probe cannot report a healthy service as dead. The alias is
`include_in_schema=False`: the documented endpoint stays `/api/health`, and the OpenAPI
schema lists one health route.

The database check inside `health()` is unchanged, so both paths report a degraded database
the same way; this adds a route, not a second opinion.

*Verified:* `test_healthroute.py` (2 tests: the alias is served, and it is not in the
schema), plus `GET /health` and `GET /api/health` both answering against the local database.

## D-160 - A pressure class whose spaces were stripped is still a class

The normalizer stores a pressure class uppercased with its spaces removed, so a clause
saying "ASME class 150" becomes `ASMECLASS150`. `_CLASS_TOKEN` in the comparison registry
required one space between `ASME` and `CLASS`, and its bare-number fallback refuses a digit
that follows a letter, so that form parsed as *no class at all*: the pair came back
`no_evidence_found` with "pressure class comparison needs a class on both sides". The gap
is now optional (`(?:ANSI|ASME)\s*`), which is one token and leaves every form that already
worked untouched.

*What it changes in a report, measured with `eval/facts_replay.py` (stored facts only, no
model call, six frozen labels):*

| | before | after |
| --- | --- | --- |
| pairs that reach a comparison | 0 of 6 | 6 of 6 |
| coverage | 0.0 | 0.6667 |
| review rate | 1.0 | 0.6667 |
| false-confident rate | 0.0 | **0.3333** |
| status accuracy | 0.0 | 0.1667 |

The rise in confident wrongness is the point worth stating plainly: with the class readable,
four pairs get a verdict, and two of them are wrong without a review flag. Both are Flowserve
pairs whose gold label is `partial_match` - a judgement about the whole clause - answered
here from one attribute, which is the replay's cross-product scope (D-155) and not something
the class rule can fix. A verdict on one attribute is not a verdict on a clause; the fix
removes a false silence, it does not make the answer right.

This is a replay against six frozen labels, not a review: status accuracy, false-confident
rate and citation correctness stay unmeasured until the audit sheet is filled in.


## D-161 - A verdict on one attribute is not a verdict on a compound clause

A BOQ item title fixes several attributes in one line: "Three piece ball valves
2" Socket weld ends Face to face distance 122mm ASME class 600 Cast Carbon
steel" states a pressure class *and* a body material. The matcher read the one
attribute the fact stated, compared it, and published a full match - a claim
about the whole clause from evidence about half of it. PRD 4 already names that
case: a requirement met "only part of a compound requirement" is a partial
match.

`attributes_named(text)` counts the attributes a clause names *and carries a
value shape for*. The shape test is deliberate: a numeric attribute would
otherwise count any clause with a figure in it, and "Pressure class 150" would
read as two attributes (class and operating pressure). Against the 325 stored
requirements it returns 0 attributes for 87%, 1 for 11%, and 2 for 2% - the two
are always `pressure_class` + `material_grade` on an item title.

`_note_compound_clause` runs at the end of both evaluation paths - the fresh
read and the fact cache, because the two wrong Flowserve pairs come from the
cache. When the compared attribute is one of at least two named attributes:

- a `full_match` becomes `partial_match`, with the score capped at
  `comparison_partial_score_max`;
- a `partial_match` keeps its status and its score is capped;
- a `missing_capability` keeps its status - a failing value fails the clause
  whatever the other attributes say, and upgrading it would excuse a real miss;
- all three get `review_required` with a reason naming the attribute that was
  not read, and the note is appended to `reason` rather than replacing it.

Statuses that already say the clause was not answered (`no_evidence_found`,
`unsupported_requirement`) are left untouched.

*What it changes in a report, measured with `eval/facts_replay.py` (stored facts
only, no model call, six frozen labels):*

| | before | after |
| --- | --- | --- |
| status accuracy | 0.1667 | 0.3333 |
| false-confident rate | 0.3333 | **0.0** |
| review rate | 0.6667 | **1.0** |
| coverage | 0.6667 | 0.6667 |

Every pair in this sample is an item title, so every pair is now flagged - that
is the honest reading, not a tuned threshold: the replay answers a clause from
one stored attribute, and now says so. The two confident Flowserve answers are
gone; one of them becomes the gold's `partial_match`, the other is still wrong
but is no longer published as a verdict. Coverage does not move because coverage
counts whether evidence was found, not how much of the clause it covers; a
review rate of 1.0 on six item titles is not a claim about real tenders, where
requirements are atomised before matching.

This is a replay against six frozen labels, not a review: status accuracy,
false-confident rate and citation correctness stay unmeasured until the audit
sheet is filled in.


## D-162 - A wrapper is classified on the whole file, because it is the last resort

`classify_document` reads two pages of prose: a fetched document says what it is
at the front, and pdfplumber is slow on a long file. That window is wrong for
the one caller that is not a fetch. A GeM bid document opens with bid details
and a declaration - so two pages say `form` - and carries its technical
specification and BOQ item table further in. `_wrapper_document` only runs when
the wrapper yielded no documents at all, and it applied the same two-page test,
so the file was discarded and the tender failed with "the tender has no
attachments" while holding 17 pages of requirements.

`classify_document` now takes `page_limit` (default 2, unchanged for fetches);
the upload path passes `None` and reads the whole file before calling it a form.
The `_SELF_READING` gate is untouched: a wrapper that is only a form is still
not an attachment, because there is nothing in it to read.

*Verified:* tender `3b6befdc`'s wrapper classifies `form` on two pages and
`boq-pdf` on the whole file, and `_wrapper_document` now returns a `boq-pdf`
attachment of 17 pages instead of `None`. New synthetic test in
`test_tenderupload.py` (a three-page file whose specification is on page 3).

The re-read itself has not been run: `audit_tender` spends model calls, and the
next step waits on an approval for that spend. What is verified is the
classification and the attachment, not the extraction.


## D-163 - The startup repair is asked again while the database is down

`reconcile` runs once, at startup, and finishes the tenders a dead process left
mid-read (D-120). If it cannot reach the database it logs
`could not reconcile tenders left mid-read` and gives up for the lifetime of the
process: a row left `processing` then stays `processing` until someone restarts
the service, and `--reload` restarts are exactly when the database can be
starting too. The failure is not hypothetical - it is what a `--reload` restart
with Postgres down produced.

`_reconcile_on_startup` now returns whether the repair ran, and the lifespan
starts a background task that asks again every `RECONCILE_RETRY_SECONDS`
(default 30) until it does. The first attempt stays synchronous, as before, so
a healthy startup behaves identically: a live database means no task at all.
The retry runs the blocking database call off the event loop and the task is
cancelled and awaited on shutdown, so no request sees a half-started task.

Only the loop is new; the repair is the same idempotent `reconcile`, so asking
twice is safe. The interval is configuration, not a literal in the loop, because
a deployment that gives Postgres a longer start-up window needs to say so.

*Verified:* `test_startup.py` (4 tests: a down database is reported not raised,
a run is reported, the loop retries until an attempt succeeds, and the lifespan
starts no task when the first attempt worked), plus `test_health.py`,
`test_healthroute.py`, `test_tenderrecovery.py`, `test_tender_api.py` together
with it, and the full unit suite before the commit.

What is not verified is the restart that motivated it: the local Postgres is up,
and the next wedged row will be the first real exercise of the retry.
# Cache conflict guard — 2026-11-05

A verified fact cache hit requires agreement on value, unit, value type, operator and conditions across the product's verified readings for that attribute. Disagreement returns a cache miss and uses the existing evidence path. UUID ordering only selects provenance among equivalent readings. This deliberately treats different textual representations conservatively.

## Scoped query reuse and applicability — 2026-11-05

## Bounded passage extraction — 2026-11-05

## Source-table column rejection — 2026-11-05

## Evidence audit diagnostics are not gold — 2026-11-05

## Reviewed fact quarantine and range endpoints — 2026-11-05

## Fact source context storage — 2026-11-05

## Exact numeric requirements inside capability ranges — 2026-11-05

## Adjacent label/value evidence anchors — 2026-11-06

Accept exact value cells under either a recognized column header or an immediately adjacent attribute label. A label-cell proposal is resolved to its immediate right-hand value only when both the requested attribute label and claimed value match exactly; retain the original proposal and store resolved_anchor separately. Do not bridge empty cells, override conflicting recognized column headers, accept invalid indices or relax model/condition checks. The first row remains ambiguous and requires review. No prompt or cache-key change, no provider configuration values committed. This change depends on existing uncommitted source-context work; do not stage that history as one unrelated commit.

For an equality requirement with a numeric value, treat it as a point interval when comparing with a documented capability range. Convert capability bounds into requirement units first; incomplete or incompatible units require review. Do not infer gte/lte range semantics from this equality rule. Explicit capability conditions not established by the requirement remain unconfirmed in both fresh and cached matching. This preserves a 10–16-inch capability instead of reducing it to 10. Re-extraction proposals are saved beside existing facts; no automatic promotion from these diagnostics.

Migration 0018 adds nullable JSONB fact.source_context without rewriting existing facts. New extraction receives bounded local source tables, document hash, page/chunk, model/family metadata and passage. Claims retain selected table/row/column coordinates, complete value text, parsed range, claimed model/series, conditions and operator. The stored context and JSON report expose these separately from business verdicts. Conditions are actual extracted conditions, no longer the linked model name. Failed claims retain their proposed values and context for review.

A table claim must select an exact supporting cell and recognized attribute header. Ambiguous/merged headers fail to review rather than inherit flattened text. Claimed model disagreement or conditions absent from the source context also fail. This deliberately sacrifices coverage pending better row/column handling. Existing no-context facts are not reused as verified cache answers; they require re-extraction. Source snapshots of all 11 quarantined facts are separate, do not overwrite labels or promote facts, and cannot be called newly verified evidence. Grouping remains opt-in.

Apply eight explicitly reviewed status downgrades with complete before-images and all supplied labels retained in a separate JSON record. Preserve original values/quotes and historical results. Do not infer product mappings or reconstruct conditions from a reviewer note automatically. Reject scalar claims that collapse a quoted nonzero range endpoint, including cache reuse. Preserve disputed Flowserve labels but record the original page's nominal-size/actuator geometry separately; no automatic promotion or label rewrite. Full model/condition-linked range normalization remains open.

Export source-bound stored facts with separate machine diagnostics and blank human attribute/value/scope labels. Report rows and unique pages separately so repeated facts cannot imply diverse validation coverage. Only demonstrable failed checks qualify for quarantine; no new failures among verified facts means no database update. Full-tender quality claims wait for human labels; no automated non-rejection becomes a correctness label.

The Flowserve integral-flange example is a wrong-column extraction admitted by the semantic verifier, not justification for a connection alias. Reject value-cell matches exclusively under other columns when the source table has a recognized requested-attribute header. Apply the same guard to cached facts; retain existing review behavior on verification failure. Original PDF table structure is read locally and cached per page within a run. This is a bounded contradiction check, not a general solution to table semantics or evidence of perfect semantic verification. No vocabulary edit or historical-result rewrite.

Group passages for one requirement only, with independent passage IDs and strict response completeness. Quotes must occur in their assigned passage before the existing verifier runs. Keep the original single-passage path at the default batch size of one until reviewed real-passage comparisons establish accuracy. Both count and character limits come from environment settings and are recorded in run snapshots. Missing/duplicate/unknown IDs fail the batch visibly; rate limits propagate to the existing run retry/resume state. No automatic single-passage retry multiplies calls after malformed output. Grouping may spend more tokens than early-exit single reads; measure this before rollout.

Reuse successful query vectors only within an execution's SQLAlchemy session, resetting at run start. Include expanded query and embedding configuration in the key; do not cache failures. Existing SentenceTransformer auto-selection uses CUDA on this host. Explicit product cache hits must agree with requested category/type metadata; unknown metadata falls through. Cached candidate hits retain uncertain applicability. Three ambiguous Flowserve nominal-size facts were separately downgraded to review with provenance preserved; see `docs/FLOWServe_PERFORMANCE.md` for exact scope, measurements and limitations.


## Local database connectivity recovery — 2026-11-08

Windows refused binding the previous local host port. Resetting the original network endpoint and restarting did not repair publication. Retain the original database container stopped for rollback; run tender-pg-local on the same existing PostgreSQL volume, bound only to 127.0.0.1:15432. Change only the ignored root .env database port; no application, LLM prompt, provider configuration or cache-key change. Never run both containers against that volume concurrently. Application read confirms 11 tenders and 244 facts. Sandbox-denied host connections are separate from database availability: elevated local validation succeeds.

## Warm local embeddings before serving — 2026-11-08

Exercise the existing shared local embedding model during API lifespan startup, off the event loop, before accepting requests. Reuse the current per-process model cache; no new worker framework, dependency, configuration option or model identifier. Skip remote embedding backends so startup never introduces a remote request. Local initialization failure prevents ready startup instead of charging its failure/latency to the first tender. This moves cold loading to startup; it does not eliminate it or establish a full-tender SLA. No extraction prompt or cache-key changes.

## Isolated vision OCR comparison prompt — 2026-11-08

Version ocr-comparison-v1 transcribes the same two scanned specification pages into source rows only: row_id, section_or_vehicle, part_number, nomenclature, unit, quantity. Image contents are data, never instructions; retain punctuation, zeros, duplicates and source spelling; unreadable/absent cells are null. No inferred specifications or capability verdicts. Exact prompt retained locally in .audit_tmp/vision-ocr-prompt.txt. Separate benchmark configuration is read only from ignored OCR_VISION_* environment values; active application provider remains unchanged. No production extraction prompt or cache key changed. User explicitly authorized the designated free-tier Groq key and these pages; stop on authentication/rate errors, without retries or paid-provider fallback.

## Production OCR, document ledger and corpus preflight — 2026-11-08

Use a persistent isolated local Paddle interpreter, configured by environment, to avoid CUDA-library conflicts with the embedding runtime. Preserve all OCR boxes, confidence, orientation, exact inverse transforms and original PDF coordinates. Cache only source-pixel readings, keyed by rendered image, worker code/runtime/weights fingerprint and source schema; attach document provenance after cache reuse. Requirement-stage keys now also include reader version4, OCR signature, geometry thresholds, layout-code hash and the wrapper content hash, so changed wrapper clauses cannot reuse stale requirements. Source-table snapshot IDs include source scope, source image key, reader version, headers and reconstructed rows, retaining previous snapshots. Extraction-stage keys include the corpus fingerprint because corpus eligibility can now defer expensive reading. No LLM extraction prompt changed.

Use existing tender_table JSON and requirement source_table_id/source_row_id fields to retain page context and row links; never collapse duplicate parts across BOQ items. Migration0019 adds source_key to the page-ledger primary key, preserving old rows and preventing wrapper/attachment page-number collisions. Read and clause-classify distinct package PDFs locally, including wrapper terms; deduplicate identical bytes while keeping manifest references. A shared completeness gate covers unreadable/unaccounted pages, invalid skips, unknown clauses and unresolved item/source interpretation. Recovery must not promote such records to done.

For explicit source part numbers, query the existing full-text index and verify the normalized identifier in candidate text before spending embedding/LLM work. Absence is a corpus-gap review abstention, never Missing capability. Keep item assignments and catalogue coverage separate from ingestion certainty. Unknown BOQ category/item scope stops before unfiltered matching. Migration0020 names corpus_gap and scope_unresolved reason codes. Short-lived evidence-query caches are scoped to the current execution and query spelling. This first integration conservatively flags ambiguous geometry and retains source disagreements; it does not claim complete table understanding or supply equivalence.


## Exact source-part BOQ binding — 2026-11-08

Before title fallback, use a unique exact part-number token in the BOQ title to resolve nomenclature spelling differences. Preserve punctuation and leading zeros; reject substring-only matches, duplicate identifier candidates and disagreement with an explicit serial assignment. Do not repair corrupted serials or reconcile differing PDF/CSV identifiers automatically. Reader version5 invalidates item/requirement stages and downstream extraction caches; LLM prompts are unchanged. Preserve previous review reasons during deterministic technical reclassification. Historical source-table snapshots remain stored; validation counts only snapshots referenced by current requirements.


## Bounded wrapper catalogue-discovery classification — 2026-11-08

Reader version6 identifies only an explicitly closed wrapper search section: a GeMARPTS searched-strings/search heading followed by Categories selected for notification. It retains each source span as informational with catalogue_discovery provenance, including continuation pages. Text before and after the section is clause-classified normally; attached specifications do not use this wrapper-only rule. Missing boundaries leave text eligible for review. No blanket page skipping, catalogue value inference or LLM prompt change. Reader-version cache invalidation reruns affected stages. Existing non-technical gating prevents these informational spans from becoming verified requirements through deterministic retyping.


## Wrapper metadata roles from source geometry — 2026-11-08

Visual review of the second unseen wrapper pages1/2/10 identifies the remaining keyword-derived technical fragments as an Item Category table value, search-strings table value and repeated BOQ item heading next to attachment links. For wrappers only, retain labelled Item Category and GeMARPTS search-string table rows as informational using their actual row bounding boxes. Exact complete BOQ-title text lines are informational headings, not newly stated capability attributes. Keep source bounding boxes in the page snapshot. Crop only these metadata bands before prose clause assembly; process everything outside them normally. This prevents joining item headings and metadata cells to neighbouring obligations. No inferred part correction or keyword-based technical exclusions. Attached specifications retain their existing path. Reader version7 invalidates item/requirements/downstream caches; no LLM prompt change.


## Bounded unscoped-clause classification probe — 2026-11-08

Before changing production classification order, test at most5 readable unresolved wrapper clauses with the existing requirement extraction system prompt unchanged. Use only the already configured dedicated free-tier connection, resolved from ignored OCR_VISION_* and GROQ_API_KEY configuration in an isolated process; do not change the active application provider. No retries, paid fallback, database writes or capability verdicts. Keep raw clause text/page/source references, validated spans, token totals and elapsed time; record no credentials, configured model identifier or endpoint. Provider/error/malformed output stops the probe. This is a small diagnostic sample, not whole-tender classification or human-confirmed gold. No prompt or production cache-key change.


## Preserve interleaved bilingual English runs — 2026-11-08

The five-call free-tier wrapper probe completed but exposed unsafe empty extraction and total-order-quantity classification; do not apply its answers to requirements or generalize its accuracy. The shared English selector discarded everything before the last Hindi run, losing earlier English clauses in flattened bilingual tables. Retain all English/value runs in source order, remove only Devanagari runs, PDF CID encoding markers and control characters, normalize whitespace and separators, and preserve source identifier punctuation/zeros/operators. Retain original source text separately. Reader version8 invalidates affected stages; normalized input already participates in per-clause extraction hashes so old truncated-input answers cannot collide with corrected inputs. No LLM prompt changes and no further remote probe calls.


## Opt-in unscoped clause classification — 2026-11-08

Introduce a distinct clause-type-only prompt clause-type-1, returning one enum label and exact supporting source quote. Document contents are data, never instructions. No specification values, item assignments or capability verdicts are accepted. A mixed fragment with technical content must not be excluded as non-technical; uncertainty returns unknown. Preserve original text, source table/row/page and unresolved-scope warnings for every proposed label. Invalid/empty/malformed answers remain reviewable failures, never empty requirement output.

Use REQUIREMENT_CLASSIFICATION_ENABLED=false by default until bounded real-source validation supports rollout. When enabled, classify readable unknown/unassigned clauses after metadata/corpus gates and before the item-scope gate; no subsequent capability extraction or matching is performed for an unassigned item. Keep review_required on every classification proposal. Cache only validated labels/quotes; key includes normalized text, model configuration, schema and full prompt/version signature. Disabled classification preserves existing extraction-stage keys. Enabling it appends its state and prompt signature; upstream item/reader keys stay unchanged. Budget/rate stops preserve the unread tail for resume. REQUIREMENT_CLASSIFICATION_MAX_CHARS bounds each input (default4000); oversize/empty input is retained as a failed classification without a request. The enabled stage key includes this limit. Existing per-run client limits apply. No paid/live calls in implementation tests; do not enable whole-tender processing from this phase alone.


## Fixed classifier probe scope — 2026-11-08

Use the unchanged clause-type-1 prompt on a fixed five-case sample recorded before calls:3 source clauses with assistant-reviewed eligibility/commercial/informational labels, plus2 explicitly synthetic technical/mixed-content controls. This is not human-confirmed gold or a random whole-tender sample. At most5 already-authorized free-tier requests, no retries, database writes, automatic enablement or paid fallback. Retain labels, exact supporting quote, latency/token totals and prompt signature without provider credentials/model identifiers/endpoints. Stop on provider/validation error. No prompt or production cache-key change.


## Labelled wrapper fields and contextual classification — 2026-11-08

Reader version9 preserves each two-column labelled wrapper field as an intact table row with original cell values, requesting label, bounds and stable page-local row ID. Do not split sentences or atomize away its label. Persist field/value cells in the existing source-table snapshot; unchanged source item references remain separate. Explicit catalogue-discovery boundary fields stay with the cross-page segmenter; unlabelled/multi-column areas retain their existing reader rather than guessing row scope.

Clause-type-2 receives JSON containing the complete clause and requesting source section. Bidder authorization/admin qualification is eligibility; an unidentified certificate or unavailable reference alone stays unknown; informational cannot hide a submission obligation. Mixed product/administrative content remains technical; unresolved mixed non-technical roles stay unknown. Returned quote must occur in the clause itself, not merely supplied context. Classifier cache keys now include context as well as prompt signature; reader9 and enabled classifier signature invalidate affected stage outputs. No paid or live calls in this implementation phase, no automatic enablement or proposal application.


## General source-region isolation — 2026-11-08

Do not tune technical extraction to a tender ID, part number or a disputed phrase. Reader version10 fixes a layout-level issue: remove only characters fully contained in actual source row/heading rectangles, never a full-width horizontal band. Preserve neighbouring columns, wrapped values, duplicate values with separate row identity and unlabeled/empty-cell fallback. Exact duplicate rectangles are not emitted twice. Validate against independently authored synthetic non-GeM labels/layouts. Named GeM discovery/metadata fields are format adapters, not product-capability rules; unknown labels remain unresolved for contextual classification. No new classifier prompt or classifier cache semantics in this phase; reader version invalidates affected stage/source snapshots.


## Generality validation scope — 2026-11-08

Freeze classifier clause-type-2 before a bounded7-call free-tier probe:4 historical disputed source fragments with actual requesting context,3 independently authored synthetic holdouts (different product domain, unresolved reference, injected document instructions). Record acceptable label sets before calls, permitting explicit unknown abstention where source ambiguity remains. No per-case business guards, prompt edits during testing, automatic retries, paid fallback, database writes or enabling classification. Stop on provider/validation failure. Source regressions are not held-out generalization evidence; synthetic holdouts are not human-confirmed gold.


## Classification proposals cannot settle business types — 2026-11-08

Repeated semantic misclassification demonstrates that exact source-quote validation does not establish a clause label. The opt-in unscoped classifier must retain the original unknown requirement type and record its proposed label only in the persisted review reason/cache. Never let a proposed informational/commercial label remove a potentially technical source clause from unresolved coverage. Preserve this review reason in manufacturer outcomes for unknown clauses. Existing deterministic technical retyping and item-scope gates remain separate; unresolved item assignments still cannot reach matching. No new phrase-specific guards, LLM prompt changes or schema migrations. Enabled extraction-stage keys include policy version proposal-only-1; disabled keys and valid classifier response caches are unchanged. No further live calls or automatic type approval in this phase.


## Preserve unknown-clause review notes — 2026-11-08

Keep existing source/proposal review notes in manufacturer outcomes for unknown clauses. This changes reporting only: unknown remains review-required, no retrieval or business-type approval is introduced. No prompt or cache-key change.


## Frozen input integrity and recovered attachment — 2026-11-08

Verify every declared source path/SHA-256 and wrapper before ingestion stage/cache lookup. Missing/invalid checksums, missing bytes and mismatches stop the run; known affected PDF pages become failed, while unrelated ledger entries and historical requirements/results are preserved. Include extensionless PDFs identified by page metadata in package reading. Matching creation/resumption validates the frozen package before using old requirements or answered pairs. Historical completed reports are not rewritten.

Recovered one overwritten historical attachment from its recorded public source only after an exact match to its original SHA-256; saved under a unique content-addressed PDF name and updated only that existing attachment record/manifest path. Backed up the manifest and retained the surviving original file unchanged. All original package attachment hashes now match. No inferred replacement bytes or new catalogue evidence.

## General source-facet coverage policy — 2026-11-08

Requirement extraction prompt2 asks for separate distinct attributes, including nonnumeric content, minimal supporting spans and no inferred units. Preserve the complete normalized source clause in source_text rather than replacing it with model paraphrases; retain native table cells, inherited headers/header page, row identity and page snapshots. Check structured attribute/value/unit/operator token representation, never count paraphrase or broad span alone as proof that all facets were represented. Unrepresented tokens, rejected spans, failed/empty extraction remain explicit review gaps and keep ingestion incomplete. Function-word exclusions live in the shared vocabulary data, not tender-specific guards. This is conservative lexical coverage, not semantic accuracy proof; synonyms/product nouns can require review.

Per-clause cache is invalidated by prompt2; extraction-stage keys also include structured-source-coverage-1 and the vocabulary hash. Reader11 invalidates source-context snapshots. New/resumed matching rejects stored requirement prompt versions predating this policy; refresh must preserve historical run data rather than silently reuse unvalidated old rows. No requirement-specific aliases or source-value exceptions are added.


## Bounded post-fix live validation — 2026-11-09

After offline regressions pass, allow at most4 configured free-tier requests total in an isolated process: two complete known source clauses under requirement prompt2, then one bounded matching case using the same production retrieval/extractor/verifier/comparator path. No retries, provider changes outside the process, paid fallback, requirement/fact writes or published reports. PostgreSQL begin hooks enforce read-only transactions even if matching commits before model reads. Record source preservation, coverage warnings and per-candidate outcomes; a local budget stop remains pending rather than manufacturer silence. Source cases are regression fixtures, not independent human gold or a general accuracy estimate.


## Reusable frozen-source checksum validator — 2026-11-09

Validate declared source and wrapper bytes by SHA-256 regardless of extension. Missing paths, unreadable bytes and missing/invalid checksums are distinct failures; two records sharing a filename are each checked against their own expected hash. The helper introduces no model calls, prompt/cache semantics or database writes. Pipeline/matching integration is tracked separately.


## Conservative structured-source coverage utility — 2026-11-09

Source content is represented only by approved attribute names and structured values/units/operators, never by a paraphrase or broad span. Unknown attributes cannot launder source words into apparent coverage. Generic function-word exclusions are vocabulary data. Unrepresented tokens trigger review; their absence does not prove semantic correctness, and synonyms/product nouns may cause conservative warnings. This utility does not change model prompts or approve requirements. Its integration/cache policy is tracked separately.


## Matching source-lineage gate — 2026-11-09

Before creating/reusing a matching run or resuming unanswered pairs, validate frozen source bytes and stored requirement prompt versions. Stop with a source-integrity/refresh conflict before model construction, run claim or matching. Repository reads expose the stored prompt version; historical completed reports remain historical and are not rewritten. This gate does not change LLM prompts or report verdicts.

## Validation boundary after recovered-source replay — 2026-11-09

A local page-accounting pass, lexical source-coverage review and cached-only retrieval diagnostic are separate checks; none constitutes a successful end-to-end match. Stop bounded live probes after malformed proposals, retain source warnings and publish no confirmed claim. Do not repair unexpected model fields with silent aliases. Preserve historical results while designing validated extraction refresh. This checkpoint changes neither prompts nor cache keys; the prompt2 and coverage-key changes remain documented in their integration entries.

## Validator-derived proposal contracts — 2026-11-09

Supply each response's Pydantic JSON Schema in the corresponding requirement, single-passage fact or grouped-passage prompt. Require the requirements envelope explicitly: an omitted envelope is malformed output, whereas an explicit empty list remains a valid proposal. Unknown fields, including operation, remain rejected without aliases or repair calls. This is a provider-independent prompt contract, not a guarantee of schema-constrained generation.

Requirement prompt version changes from 2 to 3, invalidating requirement proposal/stage caches through the existing version keys and requiring current stored extraction for new matching. Matching snapshots and fact source provenance now include a digest of the fact instructions, grouping instructions and both response schemas, preventing automatic run resumption under a changed fact contract. Independently verified fact-cache claims still pass the existing evidence gates; this prompt change alone does not delete them or historical reports. No provider, endpoint, configured model or retry policy changes.

## Live contract acceptance is not evidence acceptance — 2026-11-09

Record response-schema acceptance separately from span support, source coverage, model/condition binding and four-layer verification. Preserve review when a value lies outside its cited span or the attribute lacks approval; do not repair offsets, add source-specific vocabulary or publish a claim because a response is valid JSON. Reuse retained proposals for the next offline regression phase instead of making additional remote calls. This evaluation adds no prompt, cache-key or provider changes.

## Source-span value support utility — 2026-11-09

Use literal source support with Unicode/case/whitespace normalization and exact decimal-number comparison. Require complete numeric tokens and range limits inside the cited offsets; reject a quoted range endpoint proposed as the whole scalar even when offsets clip to that endpoint. Recognize explicit dash/to/between ranges, not a plain discrete and-list. Do not infer semantic aliases, repair offsets or promote claims. Conservative textual rejection can require human review for legitimate paraphrases. The reusable utility itself makes no prompt, cache, database or provider changes; extraction integration and version invalidation are recorded separately.

## Extraction span-gate integration and version invalidation — 2026-11-09

The shared extractor passes each proposed value into the source-span gate. Any rejected proposal makes the existing pipeline retain the original clause as a source_coverage_gap and avoid caching the partial output. Advance the existing extraction/prompt lineage from 3 to 4 because the deterministic acceptance policy changed; model instructions are unchanged in this phase. This invalidates prior proposal/stage caches and blocks old stored extraction at matching admission. No offset repair, vocabulary approval, semantic alias inference or historical database refresh. A span check does not establish attribute/model/condition applicability or prevent every endpoint-as-comparison-operator ambiguity.

## Conservative inline model-binding utility — 2026-11-09

An explicit native line in the form model; attribute: value (or pipe/equal separators) can bind a candidate to one model without page-wide inference. Require exact model identity, an exact supplied attribute label/value, valid source coordinates, and a unique matching line. Reject repeated anchors, extra fields, wrong models, malformed geometry and unresolved conditions. Do not infer column ownership or semantic aliases. Complex headings, paragraph layouts and scans remain unbound until a separate explicit region-binding method is validated. This pure utility changes no provider, prompt, cache or database behavior by itself.

## Native-line binding integration and exclusion gate — 2026-11-09

Retain native horizontal line IDs, coordinates and text in source context version2, alongside the native-inline-model-1 policy. Non-table claims must resolve explicit native-line model/attribute/value ownership; missing/legacy bindings remain reviewable and cached claims cannot bypass this source gate. Record the binding policy in run snapshots to prevent automatic resumption across changed verification semantics. Native geometry is omitted from single/grouped model payloads; no extra generation, retry or offset/heading inference is introduced. Requirement extraction version remains4; fact prompt instructions are unchanged, while source-context metadata records the new version/policy. Deterministic offered-set exclusions must pass verification and the same source-binding gate before Missing capability can be published. Complex heading/column layouts and scanned prose stay unconfirmed until explicit region ownership is validated.

## Review-only heading-region resolver — 2026-11-09

Use native text/font geometry to propose source regions, never to approve applicability. A unique dominant body font supplies a relative heading tier. Overlapping aligned heading/caption boxes can group; disjoint peer headings establish candidate columns only when native body rectangles leave a clear gutter. Stop at later competing headings, reject gutter-crossing text and keep repeated anchors ambiguous. Require exact model-heading identity plus an explicit attribute/value field, or a value-only line explicitly present among the supplied approved attribute labels. Retain heading/evidence IDs and coordinates. Font hierarchy and gutters are layout heuristics, not proof; every result carries review_required and cannot pass the verifier. No model calls, source-specific names, thresholds or new dependencies in this pure utility.

## Review-only heading-region integration and source lineage — 2026-11-09

Retain native font sizes alongside nonempty line coordinates/text in source context version3. If inline binding fails, a unique heading-region candidate is stored as region_anchor_proposal, but the verifier still fails with review required; the proposal never becomes resolved_anchor. Change the run snapshot binding policy to native-inline-1-heading-proposals-1 so resumed runs cannot silently mix this review behavior. The region records heading-regions-review-1. Native geometry remains excluded from single/grouped prompts, requirement extraction version stays4, and model instructions do not change. Region heuristics must be validated against independently reviewed layouts before any automatic promotion policy is considered.

## Ambiguous heading provenance rejection — 2026-11-09

Require unique native line IDs before proposing source regions, and require the requested model heading to occur exactly once on the page even if its proposed value appears only once. Duplicate IDs or repeated model headings do not establish unique source ownership. Advance the region policy to heading-regions-review-2 and the run binding lineage to native-inline-1-heading-proposals-2. These remain review-only proposals; no model instructions, requirement extraction version or provider settings change. Record broader layout coverage separately from independent gold matching accuracy.

## Wider layout validation does not authorize promotion — 2026-11-09

Geometric region counts are coverage diagnostics, not independent model-binding labels. Corporate headings, diagram labels and accessory/component sections observed during assistant visual review remain data for review, never source-specific business guards or automatic model identities. Keep region verification disabled. Local page/table preparation dominates region resolution in this survey; a future persistent context cache must bind to frozen document hash/page and reader/binding lineage and preserve verification/conditions. This records the next phase direction only; no persistent cache or automatic promotion is introduced here.

## Verified persistent page-context cache utility — 2026-11-09

Cache only native PDF tables and nonempty horizontal line geometry, not facts, model decisions or verification verdicts. Key entries by actual frozen source SHA-256, one-based page, native-page-context-1 reader policy, PDF engine version and caller-supplied binding policy. Validate recorded source identity and re-hash source bytes before every memory/disk hit; parse those same verified bytes to avoid a hash/read race. Source mismatch/unavailability fails visibly before reuse. Validate cache payload checksums, strict table/line structure, finite ordered coordinates and unique IDs; malformed cache is a miss, never an empty successful extraction. Cache writes use an atomic temporary-file replacement and are disposable if storage is unavailable. This standalone utility changes no model prompts or provider configuration and performs no database writes. Retention/pruning is not introduced.

## Persistent page-context matching integration — 2026-11-09

Use the verified native-page-context-1 cache from matching under the existing DATA_DIR/tmp/page_context. Revalidate source bytes even before a same-session memory hit; preserve the frozen source metadata for the run and record its verified hash in evidence context. Add reader and PDF engine versions to run snapshots so automatic resumption does not silently mix changed readers. Source, page, reader, engine and binding policy identify cache entries; no prompt instructions, requirement extraction version or automatic region promotion change. The cache persists only tables/native geometry and keeps them outside model prompts. No new configuration, database table or dependency. Cache-write failure falls back to the valid local reading; source-integrity failure stops visibly. Retention pruning remains out of scope.

## Read-only fixed diagnostic and extraction contract boundary — 2026-11-09

Force each diagnostic transaction read-only, including after matching commits/reopens a transaction, and instrument actual native-context loader results without changing model inputs or production decisions. Keep outdated historical extraction explicitly marked as requiring refresh; a diagnostic bypass of production admission does not approve it. Saved-response inspection may identify exact source occurrences, but cannot retroactively reinterpret a paraphrase as an authoritative quote or repair offsets. A future verbatim-source-quote contract requires its own prompt/cache change and tests; it is proposed, not implemented here. No production prompt/cache policy changes in this phase.

## Verbatim requirement source-quote contract — 2026-11-10

Replace LLM-counted offsets with required source_quote and advance requirement prompt/cache version4 to5. Locate one exact quote in the normalized English clause actually sent to the model, then derive Python Unicode code-point offsets and apply full-value/range support checks. Missing, repeated, altered or unsupported quotes reject the clause's partial output without caching it; retain the original source for review. Never reinterpret legacy display text as an authoritative quote or silently repair offsets. Old span-only or mixed envelopes fail strict validation. Prompt values retain source wording, full ranges and classification/standard prefixes; classification labels are not measurement units. Document text is explicitly untrusted data and must not be obeyed as instructions. Unresolved units remain unchanged with explicit review, including after cache reuse.

Persist the same source_text and code-derived source_span through the structured cache, DTO and existing database column/read path. No migration, historical refresh or result deletion is performed in this phase. Budget stops remain resumable rather than extraction silence. Fact prompts/page-context cache identities are unchanged; quote acceptance does not approve unknown attributes, source-model applicability or conditions. This is one quote-contract/provenance change, kept separate from prior uncommitted OCR/matching integration.

## Quote support and extraction acceptance remain separate — 2026-11-10

The bounded live quote probe may pass schema, unique-source location and value support without approving vocabulary, type labels or canonical numeric values. Keep the production review flags and do not promote the twelve quote-supported proposals as twelve verified technical facts. No vocabulary aliases, numeric stripping, source-model claims, prompts or cache keys change in this evaluation. Next work should canonicalize values by approved type while retaining raw source provenance, review attribute/type gaps, and preserve historical data during refresh. Plan completion around four acceptance gates rather than an unsupported overall percentage.


### 2026-11-10 — Requirement refresh retains history
Refresh marks active requirements superseded and deactivates/upserts current item rows; it never deletes requirements or run results. Migration 0021 follows 0020 and also stores normalized values beside source quotes. New runs bind immutable requirement and item inputs in their config snapshot; a different active requirement-ID set prevents resumption. Historical reports use this binding. Legacy runs without one retain actual result-linked rows only and explicitly report incomplete historical inputs; historical item labels are left unknown instead of being reconstructed from current items. No historical refresh or migration is executed before the release candidate gate.


### 2026-11-10 — Derived normalization and run-input lineage
Matching consumes approved derived attribute/value/unit fields without altering raw requirement values or quotes. Derived mappings that require review abstain before retrieval with requirement_not_parsed. Legacy rows without derived fields retain the previous path. Structured-value and clause-classification policy digests join the run configuration snapshot, so resumption cannot combine answers from different deterministic policies. Every newly created run binds its complete requirement/item input set; obsolete or unbound pending runs stop before claim. Progress and execution read this stored set; legacy completed runs expose only result-linked requirements rather than borrowing a refreshed input set.


## Structured source normalization and clause lineage — 2026-11-10

Keep raw attribute/value/unit, original source text and source span unchanged; persist deterministic normalized fields beside them. Exact aliases and normalization collisions are resolved from approved vocabulary and alias data; ambiguous, unapproved and unmapped names require review, with no product-category fallback. Normalized attribute collisions are checked together rather than first-hit lookup. A reviewer database alias overrides its shipped synonym approval.

Nominal-size normalization is selected by vocabulary metadata and uses the referenced DN/NPS table in normalization_policy.json, with absolute label tolerance NOMINAL_SIZE_TOLERANCE. Labels not uniquely present in that table require review. This tolerance recognizes label notation, never an engineering fit tolerance. Nominal comparisons and verifier layer four use table identities, not physical millimetre/inch conversion. Preserve both range endpoints; known incompatible nominal identities fail equality rather than receiving a near-size partial match. Other measurements retain ordinary unit semantics.

Clause position and the subject of the obligation select technical versus bidder eligibility, including within ATC. Policy regexes/cues are versioned JSON data; original narrow source context wins over broad page tags. Decisive eligibility clauses need no extraction call; ambiguous classification stays review. Requirement prompts are unchanged in this phase. Clause cache identity now includes classification policy plus original page/section context; requirement stage keys include policy; resolution keys include normalization policy, vocabulary, tolerance and database alias data. A resolution-only change supersedes and persists a new revision without extraction calls. Run snapshots record the policy fingerprints. Grouping remains off for the release evaluation.

## Runtime-only provider settings and private diagnostics — 2026-11-10

Require the chat endpoint together with its key/model in both preflight and client construction. Provider/embedding model identifiers and endpoints have no shipped application/example defaults; operators supply them privately at runtime. Health configuration summaries expose configured booleans only. Application-owned model-loading logs and propagated provider/embedding errors retain HTTP status or exception type, never configured identifiers, endpoint addresses, keys or provider response bodies. Retry timing is still parsed from bounded provider responses without publishing those responses. Unit fixtures use a synthetic endpoint and explicit synthetic retry/budget settings so private host configuration cannot change test behavior. No prompt or cache-key change is introduced by this configuration/diagnostics phase.

## Public-image policy packaging and publication gates — 2026-11-10

Package only the five approved nonsecret policy JSON files under /app/data; keep writable corpus, uploads and caches under /var/lib/tender/data so runtime mounts cannot hide bundled policies. Docker build contexts exclude environment files, tests, corpus and bytecode. Provider settings enter through runtime environment only. Public-image release commands require an approved clean RC, revision-labelled immutable image tags, a built-application exact-private-value scan, disposable-database smoke checks, and anonymous pulls plus explicit public repository checks. The leak gate emits counts and redacted paths only; historical source documentation is outside its built-application scope. Image contents do not include corpus or model weights; current Linux packaging has no validated Paddle/CUDA parity. No image build or publication is authorized by documenting these commands, and no publication was performed here.


### 2026-11-10 — Historical source accounting is run-bound
New run inputs snapshot tender source identity, page accounting, extraction status, unknown-clause accounting and source tables alongside requirements/items. Reports read this snapshot rather than current ingestion metadata. Older runs lacking recorded source accounting show historical context unknown and incomplete ingestion, with unknown counts instead of invented current values. Pre-refresh preservation retains any existing bound context; a legacy run cannot prove its original page/extraction state from result links alone, so this is never backfilled with current metadata.


## Configuration metadata redaction — 2026-11-10

Remove exact privately configured provider identifiers/endpoints from tracked narrative and historical report configuration blocks; use a synthetic model fixture instead of the installed embedding identifier. Source facts, quotes and verdicts are unchanged. Historical report bytes are therefore redacted derivatives, not original-byte snapshots; config-redactions.json records original and redacted hashes. No old requirements or results are deleted. Deployment-specific database connection examples are blank.

Bind extracted/cached fact attributes to the approved requested canonical attribute before comparison. An unrelated dimension cannot be reinterpreted as a nominal size merely because a model returned that attribute. Alias confidence threshold participates in normalization lineage; signed mixed fractions retain the sign on the complete magnitude.


## Independent credentials and conservative source cleanup — 2026-11-11

Preserve the legacy LLM_API_KEY credential and allow LLM_TEXT_API_KEY to select an independent text credential. Client preflight and health use the same effective text key. Alternate client endpoints require an explicit credential; an empty explicit key fails instead of falling back across providers. Models/endpoints stay runtime-only and no credential value enters logs, source or audits. Local ignored environment references text and vision source credentials separately; no provider call is required to test this. Prompt contents and cache identities are unchanged.

Remove only source with no callers after checking imports, tests, CLI/dynamic entry points and documentation: four obsolete private helpers, their fake override, eleven imports and two constants. Preserve package re-exports, migrations, active tests, dynamic OCR worker and audit/gold history. Delete only regenerated pytest temporary directories; ignore local experiments, generated audit outputs and unseen user inputs by default. New reviewed audit artifacts require explicit forced staging; already tracked artifacts remain versioned. Remove unused vision environment forwarding from Compose because production OCR uses Tesseract/Paddle; independent vision experiments remain configured locally.


## D-166 - Compose derives its database URL from the database's own credentials

The compose backend took `DATABASE_URL_DOCKER`, while the database service took
`POSTGRES_*`. Two variables described one password, so they could disagree - and
on this machine they did: `.env` set the backend to `change-me` and left
`POSTGRES_PASSWORD` unset, which gives the container `tender`. The stack could
not start, and the failure would read as a database problem rather than a
configuration one. `.env.example` had the same drift: it documents
`POSTGRES_PASSWORD=change-me` and an empty `DATABASE_URL_DOCKER`, whose compose
default was `tender:tender`.

The backend URL is now built from `${POSTGRES_USER}`, `${POSTGRES_PASSWORD}` and
`${POSTGRES_DB}`, the same values the database container receives, so the two
cannot drift. `DATABASE_URL_DOCKER` is removed from the example file and from the
documented non-setting list.

*Verified:* the image builds and the compose stack starts, runs the migrations to
`0022_llm_spend` and answers `/api/health`; recorded in the gate 4 audit.


## Source-first extraction and bounded persistence — 2026-11-12

Separate tender extraction from manufacturer catalogue availability. Remove the catalogue preflight skip from ingestion; matching keeps its applicable-corpus preflight and still abstains when evidence is absent. Extraction stage identity now uses source-first-2 and the configured response-token limit, not a corpus fingerprint. Requirement prompt/schema and valid response-cache keys stay unchanged.

Cache only source-complete extracted answers; validate coverage on old cache hits so a partial cached response cannot permanently strand the same source. Record extraction coverage/parse failures as a failed extraction stage eligible for retry; rebuild clean local source records for such retries to avoid inheriting failed-attempt flags. Valid complete cached answers remain reusable. Never relabel an unresolved reading as successful to satisfy a latency target.

Progress commits persist only small response-cache entries, not successively larger full requirement revisions. Publish the full requirement snapshot at the end of an attempt or a resumable budget stop. Keep final evidence, old revisions, page accounting and run history. A measured rollback-only test of 332-row snapshots took 0.1358/0.1050/0.0822 seconds excluding commit; persistence is overhead, not a demonstrated explanation of the entire runtime. Synthetic 2/100/332-clause tests require exactly one full-list write.

Replace the local smoke profile (one call, 2000 total tokens, 512 response tokens, no retry) with bounded 200-call/400000-token/4096-response-token/one-retry settings; document the same example profile. Existing persistent monetary-cap work is separate and is not enabled or claimed validated here. No paid provider requests are made in this repair. UI recovery advice distinguishes source-file problems from interpretation failures, and empty budget stops no longer claim the source states no requirements.

## Effective development configuration and temporary retries — 2026-11-12

A live API retained terminal overrides (one call, 2000 tokens, 512 output, zero retries) and a different text credential from the private file. Preserve normal production environment precedence, but make the explicit local-development launcher load private dotenv values with override=True before app startup. Watch application code only. Health exposes numeric effective limits, never keys/model targets. Restarted the idle project API with a clean environment; old reports keep original snapshots.

Three retries mean up to four attempts for HTTP429, temporary HTTP408/5xx and connection/timeouts, still bounded by call/token budgets. Invalid HTTP400/401/403 requests are not retried blindly. This phase does not add automatic malformed-output/schema retries.


## Table field source context and extraction coverage — 2026-11-12

The first real row probe returned quantity alone because table headers/identifiers were dropped before extraction. Reader version12 preserves full row text plus headers/cells. Advance requirement prompt/cache version5 to6 and send a bounded JSON envelope containing source_text and table context. Require one entry per nonempty labelled cell, source header names, verbatim source quotes and explicit technical versus commercial/informational typing. Cache keys now include canonical table context; persist it beside source through normalized source metadata. No source/citation/normalization review is bypassed.

Source coverage v2 may count an open-vocabulary value only when its source span supports it; canonical alias approval remains separate and unresolved mappings remain review_required. Field typing uses the labelled field rather than adjacent row columns and passes only approved canonical attributes to clause classification. This prevents an arbitrary raw label or nearby pressure column from promoting an ordering quantity.
