"""API routes (PRD 6.2 layout: backend/app/api/)."""

from __future__ import annotations

from app.api.manufacturers import router as manufacturers_router
from app.api.routes import router
from app.api.runs import router as runs_router
from app.api.tenders import router as tenders_router

__all__ = ["router", "tenders_router", "runs_router", "manufacturers_router"]
