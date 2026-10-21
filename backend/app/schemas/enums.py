"""Enumerations from the PRD.

These are the vocabulary the whole system speaks. The PRD distinguishes a result's
*status* (business outcome), *verification_outcome* (did the verifier pass) and
*applicability* (does the evidence cover this exact model) - three separate facts
that must never be collapsed into one (PRD 4).
"""

from __future__ import annotations

from enum import StrEnum


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
    PROSE = "prose"
    OCR = "ocr"


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
    FAILED = "failed"


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
