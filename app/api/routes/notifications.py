"""Notification delivery health.

Read-only and **credential-free**. Reports whether notifications are configured
and whether they are arriving -- never a webhook URL, and nothing from which one
could be reconstructed. A webhook URL is a bearer credential, and an ops endpoint
is precisely the surface that ends up more exposed than intended.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import SessionDep, SettingsDep
from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.time import utc_now
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

HTTP_SERVICE_UNAVAILABLE = 503


class NotificationHealthResponse(BaseModel):
    """Delivery status.

    Contains channel *names* and counts. No URL, no token, no fragment of one.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Whether any backend will receive events.")
    backends: list[str] = Field(description="Active backend names, e.g. ['discord'].")
    configured_categories: list[str] = Field(
        description="Channels with a webhook set. Names only -- never the webhooks."
    )

    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str | None = Field(
        default=None, description="Last failure, redacted. Identifies a channel, not a URL."
    )

    delivered_count: int = 0
    failed_count: int = 0
    skipped_count: int = Field(
        default=0, description="Events with no configured backend for their category."
    )

    checked_at: datetime


@router.get(
    "/health/notifications",
    response_model=NotificationHealthResponse,
    summary="Notification delivery health",
)
async def notification_health(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> NotificationHealthResponse:
    """Report notification configuration and recent delivery outcomes.

    Returns 503 only when notifications are enabled *and* the most recent
    outcome was a failure. Disabled notifications are a valid configuration, not
    a degraded state -- reporting them as unhealthy would make the endpoint
    useless for anyone who runs without Discord.
    """
    service: NotificationService | None = getattr(request.app.state, "notifications", None)
    repository = NotificationRepository(session)

    counts = await repository.counts_by_status()
    last_success, last_failure = await repository.last_outcome()

    enabled = service.enabled if service is not None else False
    if (
        enabled
        and last_failure is not None
        and (last_success is None or last_failure > last_success)
    ):
        response.status_code = HTTP_SERVICE_UNAVAILABLE

    return NotificationHealthResponse(
        enabled=enabled,
        backends=list(service.backend_names) if service is not None else [],
        configured_categories=sorted(settings.discord.configured_categories),
        last_success=last_success,
        last_failure=last_failure,
        last_error=redact(service.last_error)
        if service is not None and service.last_error
        else None,
        delivered_count=counts.get("delivered", 0),
        failed_count=counts.get("failed", 0),
        skipped_count=counts.get("skipped", 0),
        checked_at=utc_now(),
    )
