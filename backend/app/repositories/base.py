"""Repository interfaces (PRD 6.1).

Phase 0 ships the protocols and the Postgres connection layer only. Concrete CRUD
lands in the phase that needs it, so nothing here is dead code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentRepo(Protocol):
    """Read access to the frozen manufacturer corpus and its chunks."""

    def exists_by_sha256(self, sha256: str) -> bool: ...


@runtime_checkable
class FactRepo(Protocol):
    """Read access to verified facts and their provenance."""

    def get(self, fact_id: str) -> dict | None: ...


@runtime_checkable
class RunRepo(Protocol):
    """Run state and its results."""

    def get_status(self, run_id: str) -> str | None: ...
