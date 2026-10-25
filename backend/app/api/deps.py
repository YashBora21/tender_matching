"""Shared API dependencies.

The routes take a database session through `Depends`, so a test can replace it
with one that never opens a connection (`app.dependency_overrides`), and the
session is closed by the framework whatever the route did.
"""

from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from app.repositories.db import session_scope


def get_session() -> Iterator[Session]:
    """One transactional session per request: commit on success, roll back on error."""
    with session_scope() as session:
        yield session
