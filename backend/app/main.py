"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import manufacturers_router, router, runs_router, tenders_router
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _reconcile_on_startup() -> None:
    """Finish the reads a previous process left mid-flight (D-120).

    A failure here must not stop the API: a database that is down is what
    `/api/health` reports, and refusing to start would turn a degraded service
    into no service at all.
    """
    try:
        from app.repositories.db import session_scope
        from app.services.cli.recover import reconcile

        with session_scope() as session:
            reconcile(session)
    except Exception as exc:  # noqa: BLE001 - the API starts either way
        logger.warning("could not reconcile tenders left mid-read: %s", exc)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    _reconcile_on_startup()
    yield


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
    app.include_router(tenders_router)
    app.include_router(runs_router)
    app.include_router(manufacturers_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": settings.app_name, "version": settings.app_version, "docs": "/docs"}

    return app


app = create_app()
