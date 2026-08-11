"""Feature orchestration: load candles, adjust prices, compute features.

This is where database access, corporate-action adjustment and feature
calculation meet -- and the *only* place. :mod:`app.features.engine` and
:mod:`app.features.indicators` remain pure functions of a Polars frame (coding
rule 11), which is what makes the no-look-ahead property test possible.

**Adjustment is decided here, once, for the whole series.** Individual indicators
never see a choice: they receive a frame and compute on it. An RSI on raw prices
next to a moving average on adjusted ones is a class of bug that cannot occur if
no indicator has an opinion about adjustment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import polars as pl

from app.core.errors import InsufficientDataError
from app.core.logging import get_logger
from app.corporate_actions.adjust import adjust_candles
from app.corporate_actions.models import CorporateAction
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Candle, Instrument
from app.domain.enums import PriceSeriesAdjustment, Timeframe
from app.features.engine import FeatureEngine, FeatureSnapshot
from app.features.frame import candles_to_frame
from app.instruments.service import InstrumentService
from app.market_data.repository import CandleRepository

logger = get_logger(__name__)

# Extra bars fetched beyond the warm-up requirement so a request for "the latest
# features" returns a usable series rather than a single non-null row.
DEFAULT_EXTRA_BARS = 60

DEFAULT_ADJUSTMENT = PriceSeriesAdjustment.SPLIT_ADJUSTED
"""Features default to split-adjusted prices.

Raw prices contain split discontinuities that every return-based feature reads as
a real move -- a 4-for-1 split becomes a -75% return, which distorts momentum,
volatility, ATR and every moving average at once. Raw remains available and is
the correct choice when the question is "what did this actually trade at".
"""


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    """A computed feature frame plus the context needed to interpret it."""

    instrument: Instrument
    timeframe: Timeframe
    frame: pl.DataFrame
    feature_names: tuple[str, ...]
    adjustment: PriceSeriesAdjustment
    actions_applied: tuple[CorporateAction, ...]
    """Actions that shaped this series. Carried so a surprising feature value can
    be traced to the split that caused it without re-querying."""

    @property
    def bars(self) -> int:
        return self.frame.height


class FeatureService:
    """Loads candles, applies adjustment, runs a :class:`FeatureEngine`."""

    def __init__(
        self,
        instruments: InstrumentService,
        candles: CandleRepository,
        corporate_actions: CorporateActionRepository,
    ) -> None:
        self._instruments = instruments
        self._candles = candles
        self._actions = corporate_actions

    async def compute(
        self,
        *,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        bars: int | None = None,
        as_of: datetime | None = None,
        adjustment: PriceSeriesAdjustment = DEFAULT_ADJUSTMENT,
    ) -> FeatureFrame:
        """Compute features over the most recent bars.

        Args:
            symbol: instrument ticker.
            timeframe: candle interval.
            bars: how many bars of *output* to aim for. Warm-up bars are fetched
                on top of this, so the caller does not have to know that a
                61-bar warm-up exists.
            as_of: compute as the world looked at this instant. Bars at or after
                it are excluded at the query level, and corporate actions not yet
                effective are excluded too.
            adjustment: which price series to compute on.

        Raises:
            InstrumentNotFoundError: unknown symbol.
            InsufficientDataError: not enough stored history to warm up.
            NotImplementedError: for ``TOTAL_RETURN``.
        """
        instrument = await self._instruments.get_instrument(symbol)
        engine = FeatureEngine.for_timeframe(timeframe)

        requested = bars if bars is not None else DEFAULT_EXTRA_BARS
        rows = await self._candles.get_latest(
            instrument_id=instrument.id,
            timeframe=timeframe,
            limit=engine.warmup_bars + requested,
            as_of=as_of,
        )
        self._require_warmup(rows, engine, instrument, timeframe)

        frame, actions = await self._build_frame(
            instrument=instrument, rows=rows, adjustment=adjustment, as_of=as_of
        )
        computed = engine.compute(frame)
        return FeatureFrame(
            instrument=instrument,
            timeframe=timeframe,
            frame=computed,
            feature_names=tuple(engine.feature_columns(computed)),
            adjustment=adjustment,
            actions_applied=actions,
        )

    async def snapshot(
        self,
        *,
        symbol: str,
        timeframe: Timeframe = Timeframe.D1,
        as_of: datetime | None = None,
        adjustment: PriceSeriesAdjustment = DEFAULT_ADJUSTMENT,
    ) -> tuple[Instrument, FeatureSnapshot]:
        """Feature values at the most recent bar (at or before ``as_of``).

        Fetches only the warm-up window plus a small margin -- the caller wants
        one row, not a series.
        """
        instrument = await self._instruments.get_instrument(symbol)
        engine = FeatureEngine.for_timeframe(timeframe)

        rows = await self._candles.get_latest(
            instrument_id=instrument.id,
            timeframe=timeframe,
            limit=engine.warmup_bars + 5,
            as_of=as_of,
        )
        self._require_warmup(rows, engine, instrument, timeframe)

        frame, _ = await self._build_frame(
            instrument=instrument, rows=rows, adjustment=adjustment, as_of=as_of
        )
        return instrument, engine.snapshot(frame)

    # -- internals ---------------------------------------------------------

    async def _build_frame(
        self,
        *,
        instrument: Instrument,
        rows: Sequence[Candle],
        adjustment: PriceSeriesAdjustment,
        as_of: datetime | None,
    ) -> tuple[pl.DataFrame, tuple[CorporateAction, ...]]:
        """Apply adjustment, then cross the Decimal -> float boundary.

        Order matters: adjustment runs on ``Decimal`` prices *before*
        ``candles_to_frame`` converts to float, so the scaling is exact rather
        than accumulating binary error across repeated splits.
        """
        if adjustment is PriceSeriesAdjustment.RAW:
            return candles_to_frame(rows), ()

        actions = await self._actions.list_for_instrument(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            known_as_of=as_of,
        )
        adjusted = adjust_candles(rows, actions, adjustment)
        return candles_to_frame(adjusted), tuple(actions)

    @staticmethod
    def _require_warmup(
        rows: Sequence[Candle],
        engine: FeatureEngine,
        instrument: Instrument,
        timeframe: Timeframe,
    ) -> None:
        available = len(rows)
        if available < engine.warmup_bars:
            raise InsufficientDataError(
                required=engine.warmup_bars,
                available=available,
                context=f"{instrument.symbol} {timeframe.value}",
            )
