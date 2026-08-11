"""Signal orchestration: features + spread -> explainable signal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.config import Settings
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.costs.calculator import spread_bps_for
from app.domain.enums import Horizon, PriceSeriesAdjustment, Timeframe
from app.features.service import DEFAULT_ADJUSTMENT, FeatureService
from app.market_data.provider import MarketDataProvider
from app.signals.engine import SignalEngine
from app.signals.models import SignalResult

logger = get_logger(__name__)


class SignalService:
    """Produces :class:`~app.signals.models.SignalResult` for an instrument."""

    def __init__(
        self,
        features: FeatureService,
        provider: MarketDataProvider,
        settings: Settings,
        engine: SignalEngine | None = None,
    ) -> None:
        self._features = features
        self._provider = provider
        self._settings = settings
        self._engine = engine or SignalEngine(settings.signals, settings.costs)

    async def evaluate(
        self,
        *,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        horizon: Horizon = Horizon.D5,
        as_of: datetime | None = None,
        adjustment: PriceSeriesAdjustment = DEFAULT_ADJUSTMENT,
    ) -> SignalResult:
        """Score one instrument.

        ``adjustment`` selects the price series the features are computed from.
        It defaults to split-adjusted: scoring momentum on raw prices would read
        a 4-for-1 split as a -75% move and produce a violently bearish signal
        from a non-event.

        Raises:
            InstrumentNotFoundError: unknown symbol.
            InsufficientDataError: not enough history to warm up the features.
        """
        instrument, snapshot = await self._features.snapshot(
            symbol=symbol, timeframe=timeframe, as_of=as_of, adjustment=adjustment
        )
        spread_bps = await self._resolve_spread_bps(instrument.symbol, as_of=as_of)

        return self._engine.evaluate(
            symbol=instrument.symbol,
            snapshot=snapshot,
            timeframe=timeframe,
            horizon=horizon,
            spread_bps=spread_bps,
            reference_price=Decimal(str(snapshot.close)),
        )

    async def _resolve_spread_bps(self, symbol: str, *, as_of: datetime | None) -> Decimal:
        """Spread to use for cost modelling.

        A **live** quote is only used for a signal on live data. For a historical
        ``as_of`` signal the configured fallback is used instead, because today's
        spread was not knowable then -- using it would be look-ahead bias in the
        cost model, the one place people rarely think to check for it.

        Storing historical quotes so that past spreads can be reconstructed is a
        phase 5 task.
        """
        if as_of is not None:
            return Decimal(str(self._settings.costs.default_spread_bps))

        try:
            quote = await self._provider.get_latest_quote(symbol)
        except ProviderError as exc:
            # Degraded, not failed: a signal without a live quote is still useful,
            # it just uses the configured spread assumption. Logged, never silent.
            logger.warning(
                "no live quote available; using configured default spread",
                symbol=symbol,
                error=str(exc),
            )
            return Decimal(str(self._settings.costs.default_spread_bps))

        return spread_bps_for(quote, self._settings.costs)
