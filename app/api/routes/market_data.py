"""Market-data provider health.

Read-only, and **credential-free**. The endpoint reports whether a provider is
configured, never what it is configured with: an ops endpoint is exactly the kind
of surface that gets exposed further than intended, so it must be safe if it is.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.deps import ProviderDep, SessionDep, SettingsDep
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.db.models import Candle
from app.market_data.provider import MarketDataProvider
from app.market_data.quality import quote_age_seconds

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

HTTP_SERVICE_UNAVAILABLE = 503
PROBE_SYMBOL_LIMIT = 1


class MarketDataHealthResponse(BaseModel):
    """Provider status.

    Contains no credential, no key prefix and no key length -- nothing from which
    a secret could be reconstructed or confirmed.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(description="Active provider name.")
    configured: bool = Field(
        description="Whether the provider has the credentials it needs. Never says what they are."
    )
    reachable: bool | None = Field(
        description="Whether a probe request succeeded. Null when not probed."
    )
    last_successful_request: datetime | None = None
    last_error: str | None = Field(
        default=None, description="Last failure, with credentials redacted."
    )

    last_market_timestamp: datetime | None = Field(
        default=None, description="Newest stored candle across all instruments."
    )
    market_data_age_seconds: float | None = None
    stale: bool = Field(
        description="True when the newest stored bar is older than the configured limit."
    )
    max_age_seconds: int

    watchlist_size: int
    checked_at: datetime


@router.get(
    "/health/market-data",
    response_model=MarketDataHealthResponse,
    summary="Market-data provider health",
)
async def market_data_health(
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    provider: ProviderDep,
    probe: bool = False,
) -> MarketDataHealthResponse:
    """Report provider configuration and data freshness.

    ``probe=true`` makes one live request to confirm reachability. It is opt-in
    because a health check that always hits an external, rate-limited API becomes
    a way to exhaust your own quota -- and, if polled by an orchestrator, a
    reliable way to get rate-limited during an incident.

    Returns 503 when data is stale or a probe fails, so an orchestrator can act on
    the status code without parsing the body.
    """
    now = utc_now()
    configured = _is_configured(settings, provider.name)

    newest = (
        await session.execute(select(Candle.timestamp).order_by(Candle.timestamp.desc()).limit(1))
    ).scalar_one_or_none()

    age = (now - newest).total_seconds() if newest is not None else None
    max_age = settings.market_data.max_quote_age_seconds
    stale = newest is None or (age is not None and age > max_age)

    reachable: bool | None = None
    last_error = getattr(provider, "last_error", None)
    if probe and configured:
        reachable, last_error = await _probe(provider, settings)

    if stale or reachable is False:
        response.status_code = HTTP_SERVICE_UNAVAILABLE

    return MarketDataHealthResponse(
        provider=provider.name,
        configured=configured,
        reachable=reachable,
        last_successful_request=getattr(provider, "last_successful_request", None),
        last_error=last_error,
        last_market_timestamp=newest,
        market_data_age_seconds=round(age, 1) if age is not None else None,
        stale=stale,
        max_age_seconds=max_age,
        watchlist_size=len(settings.market_data.watchlist),
        checked_at=now,
    )


def _is_configured(settings: Settings, provider_name: str) -> bool:
    """Whether the active provider has what it needs to run.

    The mock provider is always configured -- that is the point of it.
    """
    if provider_name == "mock":
        return True
    if provider_name == "alpaca":
        return bool(settings.alpaca.is_configured)
    return False


async def _probe(provider: MarketDataProvider, settings: Settings) -> tuple[bool, str | None]:
    """One live request against the first watchlist symbol.

    Any failure is caught and reported rather than propagated: a health check must
    report a problem, not become one.
    """
    symbols = settings.market_data.watchlist[:PROBE_SYMBOL_LIMIT]
    if not symbols:
        return False, "watchlist is empty; nothing to probe"

    try:
        quote = await provider.get_latest_quote(symbols[0])
    except Exception as exc:
        # Redacted at this boundary as well as inside the provider. A provider is
        # not the last place a secret can escape: this message goes into a log
        # line *and* an HTTP response, and only one of those is ours.
        message = safe_message(exc)
        logger.warning("market data probe failed", provider=provider.name, error=message)
        return False, message

    age = quote_age_seconds(quote, now=utc_now())
    return True, None if age <= settings.market_data.max_quote_age_seconds else (
        f"probe succeeded but the quote is {age:.0f}s old"
    )
