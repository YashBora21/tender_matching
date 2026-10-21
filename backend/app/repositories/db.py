"""PostgreSQL engine / session handling.

One engine per process, created lazily from config. `ping` is the only query Phase 0
needs; it backs GET /api/health.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Optional[Engine] = None


def build_engine(settings: Optional[Settings] = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        connect_args={"connect_timeout": settings.db_connect_timeout},
        pool_pre_ping=True,
        future=True,
    )


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commit on success, roll back on error."""
    session = Session(get_engine(), future=True)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(engine: Optional[Engine] = None) -> None:
    """Raise if the database is unreachable. Used by /api/health."""
    engine = engine or get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
