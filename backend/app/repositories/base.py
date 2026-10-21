"""Repository interfaces (PRD 6.1).

The protocols describe what the services need; the SQLAlchemy implementation
lives in `repositories/postgres.py`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentRepo(Protocol):
    """The corpus: documents, their chunks and the products they cover."""

    def exists_by_sha256(self, sha256: str) -> bool: ...

    def upsert_document(self, record: dict) -> str: ...

    def insert_chunks(self, document_id: str, chunks: list[dict]) -> int: ...


@runtime_checkable
class FactRepo(Protocol):
    """Read access to verified facts and their provenance."""

    def get(self, fact_id: str) -> dict | None: ...


@runtime_checkable
class RunRepo(Protocol):
    """Run state and its results."""

    def get_status(self, run_id: str) -> str | None: ...
