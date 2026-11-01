"""HTTP routes.

Phase 0 ships one route: GET /api/health (PRD 11). The remaining PRD 11 endpoints
(tenders, runs, reports, manufacturers) are added as their phases land.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.db import ping
from app.schemas.enums import HealthState
from app.schemas.models import DependencyHealth, HealthResponse
from app.services.e2e import preflight

logger = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health(response: Response) -> HealthResponse:
    """Liveness plus dependency status.

    A database that cannot be reached degrades the service, it does not crash it:
    the caller gets an explicit state instead of a stack trace.
    """
    settings = get_settings()
    dependencies: list[DependencyHealth] = []

    started = time.perf_counter()
    try:
        ping()
        latency = (time.perf_counter() - started) * 1000
        dependencies.append(
            DependencyHealth(name="database", state=HealthState.OK, latency_ms=round(latency, 2))
        )
    except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
        logger.warning("health: database unreachable: %s", exc)
        dependencies.append(
            DependencyHealth(name="database", state=HealthState.DOWN, detail=type(exc).__name__)
        )

    overall = (
        HealthState.OK
        if all(d.state is HealthState.OK for d in dependencies)
        else HealthState.DEGRADED
    )
    if overall is not HealthState.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.app_environment,
        dependencies=dependencies,
        config=settings.public_summary(),
    )


@router.get("/preflight")
def preflight_route(
    session: Session = Depends(get_session),
    tender: Optional[str] = Query(
        default=None,
        description="a stored tender id or a tender directory, to ask about one tender",
    ),
) -> dict:
    """What a matching run needs here, before anyone asks for one (D-112).

    `can_run` is the answer to "will a run produce a report on this machine?";
    `why_not` and `next_steps` name what is missing and the command that fixes
    it. Nothing is downloaded and no model is called: this reads the database
    and the configuration.
    """
    return preflight(session, tender_ref=tender)
