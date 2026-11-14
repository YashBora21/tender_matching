"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import manufacturers_router, router, runs_router, tenders_router
from app.api.routes import root_router
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.logging import configure_logging, get_logger
from app.services.llm import embed
from app.services.tender.ocr import warm_ocr, close_ocr

logger = get_logger(__name__)


def _reconcile_on_startup() -> bool:
    """Finish the reads a previous process left mid-flight (D-120).

    A failure here must not stop the API: a database that is down is what
    `/api/health` reports, and refusing to start would turn a degraded service
    into no service at all. It returns whether the repair ran, so the caller
    knows whether to ask again later (D-163).
    """
    try:
        from app.repositories.db import session_scope
        from app.services.cli.recover import reconcile

        with session_scope() as session:
            reconcile(session)
        return True
    except Exception as exc:  # noqa: BLE001 - the API starts either way
        logger.warning("could not reconcile tenders left mid-read: %s", exc)
        return False


async def _reconcile_until_it_works(retry_seconds: float, attempt=None) -> None:
    """Keep trying the startup repair while the database is down (D-163).

    A service that starts before its database is up would otherwise hold every
    tender a dead process left mid-read as *processing* until the next restart.
    The repair is idempotent, so asking again is safe. The attempt is a
    blocking database call and is kept off the event loop.
    """
    attempt = attempt or _reconcile_on_startup
    while not await asyncio.to_thread(attempt):
        await asyncio.sleep(retry_seconds)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    task = None
    if not _reconcile_on_startup():
        task = asyncio.create_task(
            _reconcile_until_it_works(get_settings().reconcile_retry_seconds)
        )
    # The OCR worker and the embedding model load in the background: the API
    # answers requests while they warm up (a schema or report request reads
    # the database, not a model). Both load lazily on first use anyway, so a
    # read that starts before warm-up finishes simply loads what it needs.
    warm = asyncio.create_task(asyncio.to_thread(_warm_models))
    try:
        yield
    finally:
        warm.cancel()
        # The in-flight model load is not interruptible, so this await waits
        # for it to return before the worker is closed (no stray engine).
        with suppress(asyncio.CancelledError):
            await warm
        await asyncio.to_thread(close_ocr)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def _warm_models() -> None:
    warm_ocr()
    if get_settings().embedding_backend.strip().lower() == "sentence-transformers":
        # Load and exercise the shared local model before a tender run needs it.
        embed(["Synthetic embedding readiness check."])


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Tender Requirement Matching API",
        version=settings.app_version,
        description="Evidence-backed tender requirement matching (PRD: docs/PRD.md).",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_exception_handler(AppError, app_error_handler)
    app.include_router(router)
    app.include_router(root_router)
    app.include_router(tenders_router)
    app.include_router(runs_router)
    app.include_router(manufacturers_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": settings.app_name, "version": settings.app_version, "docs": "/docs"}

    return app


app = create_app()
