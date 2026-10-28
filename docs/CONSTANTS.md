# Constants

Every hardcoded constant in the codebase, what it is, and why it is where it
is. The rule: **a constant that describes a product category belongs in data;
a constant that describes how a format is written belongs in config; a
constant that describes the programming language stays in the code.**

A test (`backend/tests/test_no_hardcoding.py`) reads the collected corpus and
checks that no tender id, file name or attachment URL appears in
`backend/app/`. The corpus is data; the code is code.

## The vocabulary of the system

| What | Where | Why there |
| --- | --- | --- |
| Requirement statuses, operators, value types, ledger states, run states | `app/schemas/enums.py` | The PRD's own vocabulary (§4, §5.3.1). These are the states the system speaks, not the properties of a product. A check constraint on the database uses the same list, so the code and the database cannot disagree. |
| Document classes and their priority | `app/services/evidence.py`, `EVIDENCE_PRIORITY` in config | The class of a document decides its authority when two documents disagree (FR-25). The order is deployment policy, so it is config. |
| The attribute vocabulary | `backend/data/attribute_vocabulary.json` | Data, not code: the canonical attributes, their synonyms, their value types, the material groups, and the format patterns. A deployment points `ATTRIBUTE_VOCABULARY_PATH` at its own copy. |
| The document-type vocabulary | `backend/data/doc_types.json` | The same, for document types: the classes, the filename markers, the extensions. The collector and the evidence layer both read it, so they cannot disagree about what a document type is. |
| The format patterns | `backend/data/attribute_vocabulary.json` | A material grade, a standard code, an IP rating and a nominal size are *format grammars*, not product names. They live in the data file, so a buyer who writes "NPS 2" is read without a code change. |

## Universal format grammars

These stay as patterns, because they describe how every document in every
category is written. They are in the data file, not in the code.

| Pattern | What it recognises |
| --- | --- |
| `material_pattern` | A material grade: `SS316`, `CF8M`, `AISI 316`, `1.4408`, `13Cr`. |
| `certification_pattern` | A standard code: `IS 1239`, `ISO 9001`, `ASTM A216`, `API 608`. |
| `ip_pattern` | An ingress protection rating: `IP68`. |
| `dn_pattern` | A nominal size: `DN50`, `NPS 2"`. |

The patterns are *grammars*, and the vocabulary decides which words are
materials. A valve grade and a pump grade are both "a word that names an
alloy", and the difference is the vocabulary, not the code.

## The requirement language

One list, in the vocabulary file, read by the atomizer, the normalizer, the
evaluators and the document parser.

| List | What it holds |
| --- | --- |
| `requirement_language.mandatory_markers` | "shall", "must", "required", "compulsory" |
| `requirement_language.optional_markers` | "may", "optional", "preferably", "recommended" |
| `requirement_language.clause_markers` | The words that say a clause carries a comparison of its own. |
| `boolean_words.truthy` / `.falsy` | The words that state a yes or a no. |

These were four lists in three modules, and they did not agree: a word could
be mandatory to the atomizer and informative to the normalizer, and truthy to
the evaluator and false to the normalizer. A duplicated word list is a list
that will drift.

## Policy

Policy is the rules a deployment may want to change, so it is in config
(`app/core/config.py`) and never in a comparison.

| Setting | What it decides |
| --- | --- |
| `scoring_*` | The score of each status, the weights, the family-level cap (PRD §8). |
| `comparison_*` | The tolerances and the score bands. A test changes one and the verdict changes with no code edit. |
| `evidence_priority` | Which document class wins when two disagree. |
| `alias_confidence_threshold` | How confident an alias has to be before it resolves a name. Below it, the requirement goes to review. |
| `ingest_*` | The OCR language and resolution, the chunk length, the header band. |
| `llm_*` | The model, the base URL, the token budget, the retry limit, the timeout. |
| `embedding_*` | The embedding backend, model, base URL and dimension. The default backend is `sentence-transformers`, local and account-free; `http` speaks the OpenAI shape (Ollama). |
| `fetch_*` | The hosts a document may be fetched from, the size and time limits, the cache, and the hosts allowed to be private. |
| `pipeline_concurrency` | How many tenders are processed at once. The provider's rate limit is shared, so this is the knob that keeps a run inside it. |
| `schema_version` | The shape of the stored records. A change to it invalidates the records written with the old one. |

## What is deliberately still in the code

| Constant | Why it stays |
| --- | --- |
| The stage names (`attachments`, `items`, `requirements`, `extraction`, `resolution`) | The pipeline's own structure. The order is the invalidation chain, and it is the same in the code, the checkpoints and the report. |
| `PROMPT_VERSION` | The version of the extraction prompt. It is part of the cache key, so it must change when the prompt does. |
| The operator grammar (`<=`, `at least`, `not exceeding`, …) | The words a requirement is written in. They are English and the same for every category, so they are a universal grammar. They live in the vocabulary file, next to the other grammars. |
| The number-with-unit grammar | How a number and its unit are written. Universal. |
| The identifier grammar (a token that ends in a number is an identifier) | How `DN50` and `IP67` are written. Universal. |
| The markdown table grammar | How a table is written down. Universal. |
| The unit dimensions and aliases (`psi`, `psig`, `rpm`, `m3/h`, …) | Engineering facts about units, not about a product. A pump and a valve are measured the same way. |
| The parsing tolerances (a chunk ends at 80 characters, a header is in the top 12% of the page) | Presentation, not meaning. They are config, and the code reads them from there. |
| The `_EPSILON` in the comparison | Floating point. A property of the language. |

## The rules the code follows

1. **No tender in the code.** No tender id, file name, category, manufacturer
   or attachment URL is written into `backend/app/`. The corpus is a
   directory, and the code reads whatever is in it.
2. **No product in the code.** A grade, a standard, an attribute: those are
   data. What is in the code is the *shape* of a grade, not its name.
3. **A format grammar is data too.** The patterns are in the data file
   because a deployment may write its documents differently, and because the
   same grammar has to be read by the parser, the normalizer and the
   evaluator - three readers, one source.
4. **Policy is config.** A threshold that changes a verdict is a setting, and
   a test proves it.
