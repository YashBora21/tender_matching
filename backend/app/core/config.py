"""Application configuration.

Every threshold, weight, model name and path comes from here (env / .env), never
from a literal inside business logic (PRD 13 - "Config").
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Settings read from the environment; `.env` in the repo root is the local source."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ---
    app_name: str = "tender-backend"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # --- database (PostgreSQL + pgvector) ---
    database_url: str = "postgresql+psycopg://tender:tender@localhost:5432/tender"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_connect_timeout: int = 5

    # --- storage ---
    data_dir: Path = REPO_ROOT / "backend" / "data"

    # --- attribute vocabulary ---
    # The canonical attribute vocabulary is a data file, not code. A deployment
    # can point this at its own copy without touching the app.
    attribute_vocabulary_path: Optional[str] = None

    # --- llm / embeddings ---
    # The LLM and the embedding model are reached over an OpenAI-compatible
    # API, so the provider is a base URL and not a library. Groq serves
    # gpt-oss; a local Ollama serves the embedding model; the HuggingFace
    # router serves both if the account has credits.
    llm_api_key: Optional[str] = None
    # gpt-oss-20b over gpt-oss-120b: the provider's daily token limit is
    # per model, and 20b is the cheaper and faster of the two.
    llm_model: Optional[str] = "openai/gpt-oss-20b"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    # How many times a rate-limited call is retried, and how long it waits.
    llm_max_retries: int = 3
    llm_retry_seconds: float = 5.0
    # gpt-oss is a reasoning model: the answer comes after the reasoning, and
    # a small budget is spent entirely on reasoning.
    llm_max_tokens: int = 2048
    llm_timeout_seconds: int = 120
    llm_temperature: float = 0.0
    embedding_model: Optional[str] = "nomic-embed-text"
    embedding_base_url: str = "http://localhost:11434"
    # The dimension of the embedding model's vectors. It is a property of the
    # model, so changing the model means a migration (D-6).
    embedding_dimension: int = 768

    # --- fetching attachments ---
    # A tender links to documents on other hosts, and the collector follows
    # those links. It only follows them on the hosts it is allowed to, and
    # never to a private address.
    #
    # A manufacturer is added here when its robots.txt allows its PDFs.
    # KSB and Crane are not on the list: both disallow *.pdf.
    fetch_allowed_domains: str = (
        "gem.gov.in,bidplus.gem.gov.in,mkp.gem.gov.in,admin.gem.gov.in,"
        "fulfilment.gem.gov.in,"
        "flowserve.com,emerson.com,circor.com,xylem.com,"
        "pentair.com,apollovalves.com,abb.com,nidec.com"
    )
    # The largest a fetched document may be. A tender attachment is a
    # specification, not a dataset.
    fetch_max_bytes: int = 50 * 1024 * 1024
    fetch_timeout_seconds: int = 60
    # A document already frozen, with this hash, is not fetched again.
    fetch_cache_enabled: bool = True
    # Hosts that are allowed to be private. A local server is on the loopback
    # address, and a test needs to reach it. The default is empty: a
    # production fetch reaches nothing on the machine's own network.
    fetch_allowed_private_hosts: str = ""

    # --- api ---
    # Comma separated; kept as a string because pydantic-settings would try to
    # JSON-decode a list field straight from the environment.
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # --- scoring policy (defaults are the PRD 8 values, overridable per deployment) ---
    scoring_full_score: float = 1.0
    scoring_partial_default: float = 0.5
    scoring_missing_score: float = 0.0
    scoring_family_level_cap: float = 0.5
    scoring_weight_mandatory: int = 2
    scoring_weight_optional: int = 1

    # --- comparison policy (the rules the evaluators apply) ---
    # Two values this far apart are treated as the same number.
    comparison_equality_tolerance: float = 0.05
    # "Approximately" is accepted this far from the required value.
    comparison_approximation_tolerance: float = 0.2
    # How far short of a numeric requirement a capability may fall and still
    # count as a partial match rather than a failure. PRD 4 names "1200 RPM
    # max vs 1500 RPM required" as Missing capability, which is a 20% shortfall,
    # so the default sits below that.
    comparison_numeric_partial_tolerance: float = 0.1
    # Score band for a partial match, and the score for family-level evidence.
    comparison_partial_score_min: float = 0.25
    comparison_partial_score_max: float = 0.75
    comparison_family_level_score: float = 0.5
    # Document classes in descending authority. A conflict between two classes
    # is resolved by the higher one; a conflict inside one class stays a
    # conflict and goes to review (FR-25).
    evidence_priority: str = "compliance_schedule,technical_offer,datasheet"

    # --- document ingestion ---
    # A page with less text than this has no usable text layer and is read
    # through OCR instead (FR-9). Kept low: a scanned page yields almost
    # nothing, while a short page of real prose is still a text page.
    ingest_min_chars_for_text: int = 10
    # The confidence of a chunk read through OCR. OCR can be wrong, so the
    # evidence carries a lower confidence and is flagged for review.
    ingest_ocr_confidence: float = 0.5
    # Where Tesseract keeps its language data. PyMuPDF's OCR needs it to find
    # the trained data, and it is not on the default search path on Windows.
    tessdata_prefix: Optional[str] = None
    # The OCR language and resolution. A non-English tender needs its own
    # language, and the resolution is a quality/speed trade-off.
    ingest_ocr_language: str = "eng"
    ingest_ocr_dpi: int = 200
    # A page with less text than this ends a chunk; a header or footer sits
    # in the top or bottom band of the page, this far from the edge.
    ingest_min_chunk_chars: int = 80
    ingest_header_band_fraction: float = 0.12

    # --- aliases (FR-20) ---
    # An alias below this confidence does not resolve a name: the requirement
    # goes to review instead of being matched on a weak mapping.
    alias_confidence_threshold: float = 0.8

    # --- pipeline ---
    # The version of the stored record's shape. A change to it invalidates
    # the records that were written with the old one.
    schema_version: str = "1"
    # How many tenders are processed at once. The LLM's rate limit is shared,
    # so this is the knob that keeps a run inside it.
    pipeline_concurrency: int = 4

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def scoring(self) -> "ScoringPolicy":
        return ScoringPolicy(
            full_score=self.scoring_full_score,
            partial_default=self.scoring_partial_default,
            missing_score=self.scoring_missing_score,
            family_level_cap=self.scoring_family_level_cap,
            weight_mandatory=self.scoring_weight_mandatory,
            weight_optional=self.scoring_weight_optional,
        )

    @property
    def comparison(self) -> "ComparisonPolicy":
        return ComparisonPolicy(
            equality_tolerance=self.comparison_equality_tolerance,
            approximation_tolerance=self.comparison_approximation_tolerance,
            partial_score_min=self.comparison_partial_score_min,
            partial_score_max=self.comparison_partial_score_max,
            family_level_score=self.comparison_family_level_score,
            numeric_partial_tolerance=self.comparison_numeric_partial_tolerance,
        )

    @property
    def evidence_priority_list(self) -> list[str]:
        return [c.strip() for c in self.evidence_priority.split(",") if c.strip()]

    def public_summary(self) -> dict:
        """Config shape for /api/health. Never includes secret values."""
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "log_level": self.log_level,
            "database_configured": bool(self.database_url),
            "data_dir": str(self.data_dir),
            "llm_configured": bool(self.llm_api_key and self.llm_model),
            "embedding_model_configured": bool(self.embedding_model),
            "cors_origins": self.cors_origin_list,
        }


class ScoringPolicy(BaseModel):
    """Scoring constants from PRD 8, kept in one object so a report can snapshot them."""

    full_score: float = Field(default=1.0, ge=0.0, le=1.0)
    partial_default: float = Field(default=0.5, ge=0.0, le=1.0)
    missing_score: float = Field(default=0.0, ge=0.0, le=1.0)
    family_level_cap: float = Field(default=0.5, ge=0.0, le=1.0)
    weight_mandatory: int = Field(default=2, ge=1)
    weight_optional: int = Field(default=1, ge=1)

    model_config = {"frozen": True}


class ComparisonPolicy(BaseModel):
    """The rules the comparison evaluators apply.

    Every threshold the evaluators use lives here, so the rules are explicit
    policy rather than constants buried in the comparison code, and a report can
    record the policy it was assessed under (FR-34).
    """

    equality_tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    approximation_tolerance: float = Field(default=0.2, ge=0.0, le=1.0)
    partial_score_min: float = Field(default=0.25, ge=0.0, le=1.0)
    partial_score_max: float = Field(default=0.75, ge=0.0, le=1.0)
    family_level_score: float = Field(default=0.5, ge=0.0, le=1.0)
    # How far short of a numeric requirement a capability may fall and still
    # count as a partial match rather than a capability failure.
    numeric_partial_tolerance: float = Field(default=0.1, ge=0.0, le=1.0)

    model_config = {"frozen": True}


@lru_cache
def get_settings() -> Settings:
    return Settings()
