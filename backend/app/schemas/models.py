"""Domain models.

`Requirement` is the unified requirement record (PRD 5.3.1): both the table pipeline
and the clause pipeline emit this same shape, and it is what carries `item_id` from
extraction through retrieval, comparison and reporting (FR-T9).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import (
    Applicability,
    ConfidenceLevel,
    ExtractionMethod,
    HealthState,
    LedgerStatus,
    Operator,
    PageTag,
    RequirementStatus,
    RequirementType,
    RunStatus,
    ValueType,
    VerificationOutcome,
)

# Bumped when the shape of a stored record changes; cached results are keyed on it.
SCHEMA_VERSION = "1"
EXTRACTION_VERSION = "1"


class Requirement(BaseModel):
    """One atomic requirement extracted from a tender (PRD 5.3.1 unified record)."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    tender_id: str
    item_id: Optional[str] = None
    source_page: int = Field(ge=1)
    source_section: Optional[str] = None
    parent_clause: Optional[str] = None
    source_text: str
    # Character offsets into the page text, or a table row id.
    source_span: Optional[tuple[int, int]] = None
    extraction_method: ExtractionMethod
    requirement_type: RequirementType
    attribute: Optional[str] = None
    operator: Optional[Operator] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    value_type: Optional[ValueType] = None
    mandatory: bool = True
    extraction_confidence: Optional[float] = None
    review_required: bool = False
    review_reason: Optional[str] = None
    parent_requirement_id: Optional[str] = None
    source_table_id: Optional[str] = None
    source_row_id: Optional[str] = None
    extraction_version: str = EXTRACTION_VERSION
    schema_version: str = SCHEMA_VERSION


class Result(BaseModel):
    """One requirement assessed against one manufacturer (PRD 7)."""

    model_config = ConfigDict(extra="forbid")

    result_id: str
    run_id: str
    requirement_id: str
    manufacturer_id: str
    status: RequirementStatus
    verification_outcome: VerificationOutcome
    applicability: Applicability = Applicability.UNKNOWN
    score: float = Field(ge=0.0, le=1.0)
    capability_text: Optional[str] = None
    evidence_ref: Optional[str] = None
    confidence: Optional[ConfidenceLevel] = None
    review_required: bool = False
    review_reason: Optional[str] = None


class TenderPage(BaseModel):
    """Per-page ledger entry (PRD 5.3.1 FR-T2)."""

    model_config = ConfigDict(extra="forbid")

    tender_id: str
    page: int = Field(ge=1)
    tags: list[PageTag] = Field(default_factory=list)
    extraction_method: Optional[ExtractionMethod] = None
    quality_warnings: list[str] = Field(default_factory=list)
    ledger_status: LedgerStatus
    skip_reason: Optional[str] = None


class TenderItem(BaseModel):
    """A BOQ item. `item_id` is stable and carried end to end (FR-T9)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    tender_id: str
    name: str
    category: Optional[str] = None
    boq_ref: Optional[str] = None


class DependencyHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: HealthState
    detail: Optional[str] = None
    latency_ms: Optional[float] = None


class HealthResponse(BaseModel):
    """Body of GET /api/health."""

    model_config = ConfigDict(extra="forbid")

    status: HealthState
    app_name: str
    version: str
    environment: str
    dependencies: list[DependencyHealth] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)


class RunState(BaseModel):
    """Body of GET /api/runs/{id} (PRD 11)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    tender_id: str
    status: RunStatus
    progress_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
