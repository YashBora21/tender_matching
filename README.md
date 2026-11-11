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

## Human review

`review_required` marks the rows a person should read. It breaks a ranking tie
(more open questions ranks lower), fills the report's review queue, is the other
half of the false-confident metric - a wrong answer that was *not* flagged is the
one a reader would believe - and travels onto the audit sheet with its reason.

It does not decide anything and it does not remember a decision. Nothing stores a
human verdict: there is no column, no route, no control for one, and re-running a
tender recomputes every flag from the documents. A flag says where to look, for
one run. `make eval-audit SHEET=eval/audit/sheet.csv` grades a sheet a person has
filled and writes the summary to `eval/results/`; the sheet itself is never
edited (D-128).

A next step, not built: record a human decision per (run, requirement,
manufacturer) *beside* the machine verdict - never rewriting the status or the
score, which the verifier gates.

## Layout

```
backend/app/
  api/            # FastAPI routes: health, tenders, runs, manufacturers
  services/       # atomizer, normalizer, ingestion, retrieval, extractor,
                  # verifier, evidence, scoring, pipeline, report
  comparison/     # registry + evaluators, units, attribute vocabulary
  repositories/   # PostgreSQL access behind an interface
  schemas/        # Pydantic v2 models and enums
  core/           # config, logging, errors
backend/data/     # attribute_vocabulary.json; the corpus (not in git)
backend/alembic/  # migrations; the backend image runs them on start
backend/tests/    # pytest
collect/          # collect.py and per-manufacturer source configs
eval/             # offline scoring harness and the gold-file format
frontend/         # React + Vite app, built to static files in its image
docs/             # PRD, DECISIONS.md, WORKLOG.md
```

## Running it

### The stack in containers

```bash
cp .env.example .env    # fill in LLM_API_KEY; the database defaults work as they are
docker compose up -d --build
```

- the app: <http://localhost:3000>
- the API: <http://localhost:8000/api/health>
- PostgreSQL + pgvector: `localhost:5432`

`docker compose up` starts the three services in order: the database becomes
healthy, the backend runs `alembic upgrade head` and then serves, and only then
does the frontend start. A fresh database is usable without a separate migration
step. `docker-compose.yml` mounts `backend/data` at `/app/data`, so the corpus
and uploaded tenders survive a rebuild.

Every knob the stack reads is in [`.env.example`](.env.example), including the
frontend's `VITE_API_URL` — see the build-time caveat below.

The image migrates the schema but does not ingest the corpus. Once the stack is
up, `make ingest` parses `backend/data/manufacturers/` into PostgreSQL and
`make embed` builds the vectors; both stay explicit steps because they need the
corpus on disk and the embedding model's weights (the first `make embed`
downloads them, ~130 MB for the default `[runtime-config-redacted]`). Without them
the app starts with an empty corpus and reports it as empty.

A fresh clone has the manifests and none of the PDFs. `make corpus-restore`
replays the addresses those manifests record and checks each file against the
hash written beside it: what is already in place is left alone, what fails the
check is not written, and a document with no address on record is reported as a
gap rather than guessed (`make corpus-restore ARGS=--dry-run` prints the list
first, `ARGS="--only flowserve"` limits it to one maker). The host's robots.txt
is asked again before each fetch, so a document a manufacturer has since
withdrawn from crawling stays withdrawn.

### One command, tender to report

```bash
make e2e TENDER=path/to/tender.pdf      # upload, read, match, report
make e2e TENDER=<tender_id>             # match a tender already read
make e2e TENDER=<id> ARGS=--check       # say what is missing, change nothing
curl localhost:8000/api/preflight       # the same answer as JSON
```

`make e2e` runs the chain in the foreground: ingest the corpus if the database
has not, embed the chunks that have no vector, read the tender, run the
matching, write `report-<run_id>.{json,html,csv}`. It checks each step's
precondition before taking it and stops at the first one it cannot meet — an
empty corpus, an unread tender, a model that is not configured — with the
reason and the command that fixes it. The exit code is 0 when the report was
written, 2 when it stopped before starting, 1 when a step failed.

`GET /api/preflight` measures the same things without writing or calling
anything: the corpus counts, whether a model is configured, and what a run
would need first. It downloads nothing and makes no model call, so it is safe
to point a monitor at.

### Independent text and vision credentials

`LLM_API_KEY` remains the legacy key. Set `LLM_TEXT_API_KEY` to use a different
credential for text extraction/matching; a blank override falls back to the
legacy key. Keep both provider credentials in the ignored local environment.
Model identifiers and endpoints are supplied only through runtime configuration.

Vision experiments use their own `OCR_VISION_API_KEY`, model and endpoint.
The production OCR path supports Tesseract/Paddle, not an automatic vision
fallback. A client targeting an alternate endpoint requires an explicit key.
See [GitHub readiness](docs/GITHUB_READINESS.md) for local wiring and checks.

### Spending a limited key

Two settings bound what one process may ask: `LLM_CALL_BUDGET` and
`LLM_TOKEN_BUDGET` (0 is no ceiling). A run that reaches one keeps every answer
it already wrote and ends `rate_limited`, not `failed` and not `done`, with the
message naming what was spent — `make e2e` prints that stop as its own line,
with how many results it kept and the same command to continue from. A run
started against the same tender pays only for the pairs still missing, so a
small quota is spent forward rather than twice over. The provider is read from the response, not assumed: the wait is
taken from `Retry-After`, from a field of the JSON error body, or from the prose
the provider said it in, which is what lets a hosted model, a self-hosted one
and a local one all be the same code. `LLM_BASE_URL` is recorded in every run's
config snapshot, because the same model name served elsewhere is a different
answer.

These per-run token/call limits do not enforce a persistent INR monthly cap.
A persisted spend ledger is still required before unattended paid batch runs.

A tender whose assessable clauses are all commercial, legal or financial is
answered from their clause types and needs no key at all, so it is not refused
for want of one.

### When a document cannot be fetched

A tender links to documents on other hosts, and the collector follows those
links only within `FETCH_ALLOWED_DOMAINS`, never to a private address, never
past a redirect, and only where robots.txt allows it. KSB and Crane are not on
that list: both disallow `*.pdf`. That list is a rule about a link found inside
a document; it does not gate `make corpus-restore`, whose addresses were
recorded by a person and are checked against the hash beside them.

robots.txt is matched by `backend/app/core/robots.py`, not by
`urllib.robotparser`, because the standard library ignores a rule that carries
a wildcard — `Disallow: *.pdf` is the line a manufacturer writes to keep its
catalogues out of crawlers, and the library reads it as no rule at all. `*` is
any sequence, `$` anchors the end, a rule with no `$` decides by prefix, and the
longest match wins. Both fetch paths use that one matcher, so the collector and
the fence cannot disagree about what a host allowed.

When an attachment still cannot be had — a host off the list, a PDF behind a
login, a link that has moved — the upload page lists it and takes the file from
disk instead. The document is frozen with its hash and recorded in the tender's
manifest like any fetched one, so the report can still say which file a quote
came from; re-uploading the tender PDF reads it with the rest.

### Locally

```bash
make install-dev
cp .env.example .env
make migrate
make run                                    # uvicorn on :8000
cd frontend && npm install && npm run dev   # Vite on :5173
```

Tests: `make test` (all), `make test-unit` (no database needed),
`make test-integration` (needs `TEST_DATABASE_URL`). The frontend has its own
suite: `cd frontend && npm test`.

One dependency is not a Python package: **Tesseract**, the OCR engine a page
with no text layer needs (FR-9). The backend image installs it
(`apt-get install tesseract-ocr tesseract-ocr-eng`); on Windows install the
UB-Mannheim build and put `tesseract.exe` on `PATH`, setting `TESSDATA_PREFIX`
if the language data is not found. Without it nothing breaks and nothing is
guessed — the page is recorded in the ledger as `failed` with the reason, the
tender is labelled *incomplete ingestion*, and `GET /api/preflight` says the
machine has no OCR. An unread page is never read as a missing capability.

Every threshold, weight, model name and path comes from the environment
(`backend/app/core/config.py`). There are no thresholds written into the
comparison code, the scoring code or the evidence logic.

## The two images

`backend/Dockerfile` builds the API. `frontend/Dockerfile` builds the React app
with Node and copies the static result into nginx, which serves it with an SPA
fallback.

**`VITE_API_URL` is a build argument, not a runtime variable.** The browser
executes the bundle, so the backend address has to be baked in when the image is
built — and it must be the address the *browser* uses. `http://backend:8000`
resolves only inside the compose network, so it is the wrong value even though
the backend can reach it. Change the address and rebuild the frontend image.

Nothing is pushed to Docker Hub from this repository yet, so the `docker pull`
and `docker push` lines below work only after you publish the images under your
own account.

### Build

```bash
docker build -t <user>/tender-backend ./backend
docker build --build-arg VITE_API_URL=http://localhost:8000 \
             -t <user>/tender-frontend ./frontend
```

### Run them on a shared network

The three containers find each other by name on a user-defined network, which
is what the compose file does for you. By hand, the same shape:

```bash
docker network create tender

docker run -d --name tender-db --network tender \
  -e POSTGRES_USER=tender -e POSTGRES_PASSWORD=tender -e POSTGRES_DB=tender \
  -v tender-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

docker run -d --name tender-backend --network tender \
  -p 8000:8000 \
  -e DATABASE_URL=postgresql+psycopg://tender:tender@tender-db:5432/tender \
  -e CORS_ORIGINS=http://localhost:3000 \
  -e LLM_API_KEY="$LLM_API_KEY" -e LLM_MODEL="$LLM_MODEL" \
  -e EMBEDDING_BACKEND="$EMBEDDING_BACKEND" -e EMBEDDING_MODEL="$EMBEDDING_MODEL" \
  -v "$PWD/backend/data:/app/data" \
  <user>/tender-backend

docker run -d --name tender-frontend --network tender \
  -p 3000:80 \
  <user>/tender-frontend
```

`CORS_ORIGINS` must name the origin the browser loads the app from, or the
frontend's requests to the API are refused. The `-v` argument needs an absolute
path; `$PWD` is the shell's working directory. Start the backend only once the
database accepts connections (`docker logs tender-db`), because this form has no
healthcheck to wait on — compose does. Without a corpus mounted at `/app/data`
the app starts with an empty corpus and says so, rather than inventing
manufacturers.

### Publish and pull

```bash
docker tag <user>/tender-backend <user>/tender-backend:1.0
docker push <user>/tender-backend:1.0
docker tag <user>/tender-frontend <user>/tender-frontend:1.0
docker push <user>/tender-frontend:1.0

docker pull <user>/tender-backend:1.0
docker pull <user>/tender-frontend:1.0
```

The `docker run` commands above are unchanged once the tags are published;
replace `<user>/tender-backend` with `<user>/tender-backend:1.0`.

## Example output

A trimmed export of a **synthetic** report, produced by the real CSV renderer
(`GET /api/runs/{run_id}/export.csv`, or `make report RUN=<id> FORMAT=csv`) from
the test fixture. The tender, the manufacturer and the document are invented,
and the corpus is not real: this shows the shape of the answer, not a finding
about any product.

```csv
requirement_id,item_id,requirement,type,mandatory,manufacturer,status,score,verification_outcome,applicability,capability,document,page,quote,confidence,review_required,review_reason
r1,1,Body shall be SS316,technical,yes,Acme,full_match,1.0,passed,model_level,Body material SS316,circor-d-series.pdf,12,Body: ASTM A351 CF8M (SS316),high,no,
r2,1,Delivery within 180 days,commercial,yes,Acme,unsupported_requirement,0.0,not_checked,unknown,,,,,high,no,
```

The same run in JSON carries the ranking, the page ledger and the review queue
(abridged):

```json
{
  "manufacturers": [
    {
      "manufacturer": "Acme",
      "rank": 1,
      "compatibility_percent": 100.0,
      "coverage_percent": 100.0,
      "status_counts": {"full_match": 1, "unsupported_requirement": 1},
      "fails_mandatory": false,
      "review_count": 0
    }
  ],
  "tender": {
    "pages": {
      "declared": 2, "accounted": 2, "unaccounted": 0,
      "counts": {"parsed": 1, "ocr": 1},
      "unresolved": [], "skipped": [],
      "incomplete_ingestion": false
    }
  },
  "results": {"stored": 2, "expected": 2, "complete": true}
}
```

The commercial clause is listed separately and does not move the score. A page
that was not read would set `incomplete_ingestion` and label the whole tender,
rather than being counted as processed; the full JSON also lists every page's
ledger state.

## Evaluation

The evaluation harness scores a stored report against hand-written labels,
offline — no database, API or model:

```bash
make eval-check GOLD=eval/gold/synthetic.example.jsonl
```

`check` validates a gold file on its own; `score` prints status accuracy, a
confusion matrix, per-status precision/recall/F1, the false-confident rate,
coverage, review rate and citation correctness. The 50–100 real labelled pairs
are not written yet: they need requirements from an actual model run, and
inventing them would fabricate the evidence the labels exist to check. See
[`eval/README.md`](eval/README.md) for the metric definitions and the gold
format.

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

As of 2026-11-11, the backend, frontend, corpus tools and evaluation harness
are implemented. The previous RC passed 1,128 offline unit tests. Its history
migration has been applied locally and the formerly failing requirements
endpoint returned HTTP 200. Current cleanup validation is recorded in
[GitHub readiness](docs/GITHUB_READINESS.md).

Extraction preserves page/item identity, raw quotations, table/model context
and complete ranges. Numeric nominal labels use a configured DN/NPS table;
unknown sizes and ambiguous aliases require review. In the saved twelve-row
replay, six rows had canonical comparison inputs and six abstained as unmapped;
one numeric value and one clause type changed. These are not twelve verified
manufacturer matches.

Retrieval combines keyword and vector search, applies per-item product scope,
checks the structured fact cache first, and gates claims through verification.
Model/table ambiguity and corpus silence remain review outcomes. Grouping stays
off. Full-tender matching accuracy and a 2–4 minute processing target have not
been demonstrated on the current RC.

Before acceptance:

- Confirm the intended pump file and full-tender gold labels, then evaluate the
  fixed valve tender and that pump tender on a tested RC.
- Review vendor evidence and unmapped aliases from those results; rerun the
  frozen evaluation after any corrections.
- Add persistent spend accounting/caps before unattended paid batches.
- Build and smoke-test both images; validate the deployment OCR/CUDA setup and
  public pull before announcing a release. Authentication is outside the v1
  scope; the API assumes a trusted network.

A fresh clone has manifests and policies, not the local PDF corpus, uploads,
weights, private credentials or generated experiments. Corpus restoration can
report unavailable/manual sources and must not invent missing documents.

[Work log](docs/WORKLOG.md) · [Decision log](docs/DECISIONS.md) ·
[Public Docker release commands](docs/DOCKER_HUB_PUBLIC_RELEASE.md)
