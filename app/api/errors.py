"""Domain errors to HTTP responses.

The single translation point between the domain exception hierarchy and HTTP.
Business code raises :class:`~app.core.errors.TradabotError` subclasses and never
imports ``fastapi`` (coding rule 10).

Nothing here swallows an exception (coding rule 8): every handler logs before
responding, and unexpected exceptions are logged with a full traceback.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.schemas.common import ErrorResponse
from app.core.errors import (
    ConfigurationError,
    InsufficientDataError,
    NotFoundError,
    ProviderError,
    TradabotError,
    ValidationError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# Plain integers rather than `starlette.status` constants: Starlette renames
# these across versions (HTTP_422_UNPROCESSABLE_ENTITY ->
# HTTP_422_UNPROCESSABLE_CONTENT) and emits a DeprecationWarning on the old
# name. A warning raised *inside* an exception handler turns a clean 422 into an
# opaque 500, so the status codes are pinned here instead.
HTTP_BAD_REQUEST: Final = 400
HTTP_NOT_FOUND: Final = 404
HTTP_UNPROCESSABLE: Final = 422
HTTP_INTERNAL_ERROR: Final = 500
HTTP_BAD_GATEWAY: Final = 502


def _response(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=error, detail=detail).model_dump(),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach handlers for every error class that has a defined HTTP meaning."""

    @app.exception_handler(NotFoundError)
    async def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        logger.info("resource not found", path=request.url.path, detail=str(exc))
        return _response(HTTP_NOT_FOUND, f"{exc.entity}_not_found", str(exc))

    @app.exception_handler(InsufficientDataError)
    async def _insufficient_data(request: Request, exc: InsufficientDataError) -> JSONResponse:
        # 422, not 404: the instrument exists, the request is well-formed, and the
        # server simply cannot compute an answer from the data it holds. Telling
        # the client "not found" would send them looking for the wrong problem.
        logger.info("insufficient data", path=request.url.path, detail=str(exc))
        return _response(HTTP_UNPROCESSABLE, "insufficient_data", str(exc))

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError) -> JSONResponse:
        logger.warning("data validation failed", path=request.url.path, detail=str(exc))
        return _response(HTTP_UNPROCESSABLE, "validation_error", str(exc))

    @app.exception_handler(ProviderError)
    async def _provider(request: Request, exc: ProviderError) -> JSONResponse:
        logger.warning("market data provider error", path=request.url.path, detail=str(exc))
        return _response(HTTP_BAD_GATEWAY, "provider_error", str(exc))

    @app.exception_handler(ConfigurationError)
    async def _configuration(request: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("configuration error", path=request.url.path, detail=str(exc))
        return _response(HTTP_INTERNAL_ERROR, "configuration_error", str(exc))

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        logger.info("invalid request", path=request.url.path, detail=str(exc))
        return _response(HTTP_BAD_REQUEST, "invalid_request", str(exc))

    @app.exception_handler(TradabotError)
    async def _tradabot(request: Request, exc: TradabotError) -> JSONResponse:
        logger.error("unhandled domain error", path=request.url.path, detail=str(exc))
        return _response(HTTP_INTERNAL_ERROR, "internal_error", str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        # Log the traceback, but do not leak internals to the client.
        logger.exception("unexpected error", path=request.url.path)
        return _response(
            HTTP_INTERNAL_ERROR,
            "internal_error",
            "An unexpected error occurred. Check the server logs.",
        )
