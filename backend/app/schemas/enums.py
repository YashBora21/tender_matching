"""Enumerations from the PRD.

These are the vocabulary the whole system speaks. The PRD distinguishes a result's
*status* (business outcome), *verification_outcome* (did the verifier pass) and
*applicability* (does the evidence cover this exact model) - three separate facts
that must never be collapsed into one (PRD 4).
"""

from __future__ import annotations

from enum import StrEnum


class ClassificationSource(StrEnum):
    """Source of a product or document classification."""

    MANIFEST = "manifest"
    RULE = "rule"
    HUMAN = "human"


class DocRole(StrEnum):
    """The role a document plays in the corpus."""

    DATASHEET = "datasheet"
    CATALOGUE = "catalogue"
    BROCHURE = "brochure"
    WHITE_PAPER = "white_paper"
    CERTIFICATE = "certificate"
    POLICY = "policy"
    OTHER = "other"


class RequirementStatus(StrEnum):
    """PRD 4 - the business result for one requirement against one manufacturer."""

    FULL_MATCH = "full_match"
    PARTIAL_MATCH = "partial_match"
    MISSING_CAPABILITY = "missing_capability"
    UNSUPPORTED_REQUIREMENT = "unsupported_requirement"
    NO_EVIDENCE_FOUND = "no_evidence_found"


class VerificationOutcome(StrEnum):
    """PRD 4 - the verifier's verdict, recorded separately from the status."""

    PASSED = "passed"
    FAILED = "failed"
    FAMILY_LEVEL = "family_level"
    CONFLICTING = "conflicting"
    NOT_CHECKED = "not_checked"


class Applicability(StrEnum):
    """PRD 4 - how specifically the evidence applies to the model under test."""

    MODEL_LEVEL = "model_level"
    FAMILY_LEVEL = "family_level"
    UNKNOWN = "unknown"


class TenderReasonCode(StrEnum):
    """Why a tender's own reading stopped short (PRD 4).

    These describe what happened to the *document*, never a finding about a
    manufacturer: a tender whose specification was never found says nothing
    about what anyone can supply, and the two must not be read as the same
    answer.
    """

    # No attachment plays the specification's role: the tender links to forms
    # or certificates, or to a bill of quantities and nothing else.
    NO_SPECIFICATION_FOUND = "no_specification_found"
    # A specification was found and nothing could be read out of it: its pages
    # are scans and the OCR gave little or no text. A page nobody read is not
    # a page that states nothing.
    SPECIFICATION_UNREADABLE = "specification_unreadable"
    # The specification was read, page by page, and it states no requirement.
    NO_REQUIREMENTS_STATED = "no_requirements_stated"


class ReasonCode(StrEnum):
    """Why a comparison produced no verdict (D-86).

    Each of these is a limit of *this system*, not a finding about the product:
    the clause is technical and a document could have answered it. They are kept
    apart from `unsupported_requirement`, which PRD 4 reserves for a clause no
    document could answer - commercial, legal, financial, warranty, vague.
    """

    # No attribute mapping, so no evaluator: "class" is not a canonical term.
    ATTRIBUTE_NOT_MAPPED = "attribute_not_mapped"
    # One side of the comparison had no value, so there was nothing to compare.
    VALUE_MISSING = "value_missing"
    # A material was named but is not a grade the vocabulary knows.
    GRADE_NOT_RECOGNISED = "grade_not_recognised"
    # The two units do not convert, or the conversion failed.
    UNIT_NOT_COMPARABLE = "unit_not_comparable"
    # The stored operator is not one the registry can apply.
    OPERATOR_NOT_COMPARABLE = "operator_not_comparable"
    # The rule that keeps a clause inside its own BOQ item's category (FR-17)
    # removed the only documents this manufacturer has. The pair was never
    # compared, and the corpus's own labels are why - which is not the same
    # statement as "the catalogue is silent about this capability" (D-135).
    CATEGORY_MISMATCH = "category_mismatch"
    # The clause was typed technical and nothing in it was read as an attribute,
    # so there was nothing to compare it with. The comparison never started, and
    # the reason is ours: a requirement we did not parse is not a capability the
    # corpus does not document (D-147).
    REQUIREMENT_NOT_PARSED = "requirement_not_parsed"


class LedgerStatus(StrEnum):
    """PRD 5.3.1 FR-T2 - the state every tender page must end in.

    `failed` is a first-class state: a page that could not be extracted or OCRed is
    never counted as processed, and the tender is labelled incomplete ingestion while
    any page is unresolved.
    """

    PARSED = "parsed"
    SKIPPED = "skipped"
    OCR = "ocr"
    FAILED = "failed"


class PageTag(StrEnum):
    """PRD 5.3.1 FR-T1 - page triage tags, assigned locally without an LLM."""

    TEXT = "text"
    TABLE = "table"
    SCANNED = "scanned"
    BOILERPLATE_CANDIDATE = "boilerplate-candidate"


class RequirementType(StrEnum):
    """PRD 5.3 FR-14 - clause classification. Only technical and certification are matched."""

    TECHNICAL = "technical"
    CERTIFICATION = "certification"
    COMMERCIAL = "commercial"
    ELIGIBILITY = "eligibility"
    INFORMATIONAL = "informational"
    UNKNOWN = "unknown"


class MatchableType(StrEnum):
    """Requirement types that go to matching; everything else is Unsupported (FR-14)."""

    TECHNICAL = "technical"
    CERTIFICATION = "certification"


class ExtractionMethod(StrEnum):
    """PRD 5.3.1 - how a requirement or page was read."""

    TABLE = "table"
    # A prose page: the text layer is joined into clauses and split on clause
    # numbering and sentence boundaries, never on line breaks (D-88).
    TEXT = "text"
    # The atomizer's split of a compound clause.
    PROSE = "prose"
    OCR = "ocr"
    # A clause the bill of quantities states in an item's own words. It is not
    # a table row: a row that is already one parameter is never split, and this
    # one may state two facts, so it has to go through the atomizer.
    ITEM = "item"


class Operator(StrEnum):
    """PRD 5.3 FR-15 - comparison operators after normalisation.

    Direction words are normalised into operators: "min 1500 RPM" -> GTE,
    "max 1200 RPM" -> LTE.
    """

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"
    APPROX = "approx"
    UNKNOWN = "unknown"


class ValueType(StrEnum):
    """PRD 5.6 FR-27 - the attribute kinds the comparison registry can evaluate."""

    NUMERIC = "numeric"
    RANGE = "range"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"
    CERTIFICATION = "certification"
    MATERIAL = "material"


class RunStatus(StrEnum):
    """PRD 11 - explicit run states."""

    QUEUED = "queued"
    PARSING = "parsing"
    RETRIEVING = "retrieving"
    VERIFYING = "verifying"
    SCORING = "scoring"
    DONE = "done"
    # The provider's rate limit stopped the run before it had answered every
    # requirement. Distinct from `failed`: nothing is wrong with the run, and
    # the answers it already has are kept.
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


class TenderStatus(StrEnum):
    """The state of a tender's ingestion (PRD 11).

    A tender is processed when it is uploaded, not when a run asks for it, so
    the caller needs a state to poll. `failed` is distinct from "read it and
    found no requirements": one is a document that could not be read, the
    other is a document that was read. Reporting the first as the second is
    the absence-of-evidence trap PRD 4 forbids.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    # The tender was read, but not in full: no page of the specification was
    # read, or the bill of quantities gave no items, or the specification
    # gave no requirements. It is not `failed` (a document that could not be
    # read) and not `done` (a document that was read): calling either of
    # those "done" would report a page nobody looked at as a page with
    # nothing in it (PRD 4).
    INCOMPLETE = "incomplete"
    # The extraction stage stopped at the provider's limit. The tender is not
    # failed and not complete: what was read is kept, and the next run resumes
    # (D8).
    RATE_LIMITED = "rate_limited"


class ConfidenceLevel(StrEnum):
    """PRD 9 - confidence buckets. Rule-based review flags override the bucket."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FactVerificationStatus(StrEnum):
    """PRD 7 - a stored fact's own verification state."""

    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class AliasSource(StrEnum):
    """PRD 5.4 FR-20 - how an alias was proposed."""

    MANUAL = "manual"
    LLM = "llm"


class HealthState(StrEnum):
    """Health of a dependency, as reported by GET /api/health."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
