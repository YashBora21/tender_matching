"""PostgreSQL access behind a swappable interface (PRD 6.1)."""

from __future__ import annotations

from app.repositories.base import DocumentRepo, FactRepo, RunRepo
from app.repositories.db import build_engine, get_engine, ping, session_scope
from app.repositories.postgres import PostgresDocumentRepo

__all__ = [
    "DocumentRepo",
    "FactRepo",
    "PostgresDocumentRepo",
    "RunRepo",
    "build_engine",
    "get_engine",
    "ping",
    "session_scope",
]
