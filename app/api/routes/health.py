"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.api.deps import ProviderDep, SessionDep, SettingsDep
from app.api.schemas.common import HealthResponse
from app.core.logging import get_logger
from app.core.time import utc_now

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

VERSION = "0.1.0"
HTTP_SERVICE_UNAVAILABLE = 503


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and dependency status",
)
async def health(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    provider: ProviderDep,
) -> HealthResponse:
    """Report service status, including whether the database is reachable.

    Returns 503 when a dependency is down, so orchestrators can act on the status
    code without parsing the body. The exception is caught rather than propagated
    on purpose -- a health check must report failure, not become one -- but it is
    logged and surfaced in the response.
    """
    database = "ok"
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        database = f"error: {type(exc).__name__}"
        logger.warning("database health check failed", error=str(exc))

    healthy = database == "ok"
    if not healthy:
        response.status_code = HTTP_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=VERSION,
        environment=settings.env.value,
        database=database,
        market_data_provider=provider.name,
        timestamp=utc_now(),
    )
