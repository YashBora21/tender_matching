"""API routes (PRD 6.2 layout: backend/app/api/)."""

from __future__ import annotations

from app.api.routes import router
from app.api.tenders import router as tenders_router

__all__ = ["router", "tenders_router"]
