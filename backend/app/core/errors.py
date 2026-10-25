"""Structured error contract (PRD 11: "Errors return structured JSON with a code and message")."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for errors the API reports with a stable code."""

    code = "internal_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class NotFoundError(AppError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND


class ValidationError(AppError):
    code = "validation_error"
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY


class DependencyUnavailableError(AppError):
    code = "dependency_unavailable"
    http_status = status.HTTP_503_SERVICE_UNAVAILABLE


class ConflictError(AppError):
    """The request is valid, but the thing it asks for is not ready yet."""

    code = "conflict"
    http_status = status.HTTP_409_CONFLICT


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())
