"""Turning stored candles into a multi-timeframe assessment.

Sits between the feature engine and the scanner: it asks
:class:`~app.features.service.FeatureService` for each timeframe's warmed-up
snapshot, adds the structure metrics from :mod:`app.scanner.structure`, and
returns a :class:`~app.scanner.timeframes.MultiTimeframeContext`.

**No indicator is reimplemented here.** EMA, RSI, ATR, realised volatility and
relative volume all come from the existing registry. This module reads them.

Failure is per-timeframe, never per-symbol. An instrument with two years of daily
bars and one week of 5-minute bars produces a usable macro read and an
``INSUFFICIENT`` entry read -- which is honest, and much more useful than
refusing to evaluate it at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InstrumentNotFoundError, InsufficientDataError
from app.core.logging import get_logger
from app.db.models import Instrument
from app.domain.enums import PriceSeriesAdjustment, Timeframe
from app.features.service import FeatureService
from app.market_data.repository import CandleRepository
from app.scanner.enums import DataQuality, StructureState
from app.scanner.structure import StructureMetrics, analyse_structure
from app.scanner.timeframes import (
    SCANNER_TIMEFRAMES,
    TIMEFRAME_ROLES,
    MultiTimeframeContext,
    TimeframeAssessment,
    classify_trend,
)

logger = get_logger(__name__)

STRUCTURE_LOOKBACK: Final = 20
STRUCTURE_BARS: Final = 60
"""Bars fetched for structure analysis. Enough for a 20-bar range plus swing
confirmation, and bounded so a scan does not reload full history per symbol."""

STALE_BAR_MULTIPLE: Final = 3
"""A series is stale once it is this many bar-intervals behind. Three allows for
a missed bar and a late print without tolerating a genuinely dead feed."""

FEATURE_SET_VERSION: Final = "features-v1"
SCANNER_POLICY_VERSION: Final = "scanner-v1"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """A symbol's multi-timeframe read, plus the newest data it saw."""

    context: MultiTimeframeContext
    newest_bar: datetime | None
    primary_snapshot_values: dict[str, float | None]
    """The primary timeframe's raw feature values, for the evaluation record."""


class MultiTimeframeAnalyser:
    """Builds a multi-timeframe context from stored candles."""

    def __init__(
        self,
        session: AsyncSession,
        features: FeatureService,
        *,
        timeframes: tuple[Timeframe, ...] = SCANNER_TIMEFRAMES,
        max_data_age: timedelta | None = None,
    ) -> None:
        self._session = session
        self._features = features
        self._timeframes = timeframes
        self._candles = CandleRepository(session)
        self._max_data_age = max_data_age

    async def analyse(
        self,
        *,
        instrument: Instrument,
        as_of: datetime,
        adjustment: PriceSeriesAdjustment = PriceSeriesAdjustment.SPLIT_ADJUSTED,
    ) -> AnalysisResult:
        """Assess every configured timeframe for one instrument."""
        assessments: dict[Timeframe, TimeframeAssessment] = {}
        newest: datetime | None = None
        primary_values: dict[str, float | None] = {}

        for timeframe in self._timeframes:
            assessment, values = await self._assess(
                instrument=instrument,
                timeframe=timeframe,
                as_of=as_of,
                adjustment=adjustment,
            )
            assessments[timeframe] = assessment
            if assessment.bar_timestamp is not None and (
                newest is None or assessment.bar_timestamp > newest
            ):
                newest = assessment.bar_timestamp
            if timeframe is Timeframe.H1:
                primary_values = values

        return AnalysisResult(
            context=MultiTimeframeContext(symbol=instrument.symbol, assessments=assessments),
            newest_bar=newest,
            primary_snapshot_values=primary_values,
        )

    async def _assess(
        self,
        *,
        instrument: Instrument,
        timeframe: Timeframe,
        as_of: datetime,
        adjustment: PriceSeriesAdjustment,
    ) -> tuple[TimeframeAssessment, dict[str, float | None]]:
        """One timeframe. Degrades to an UNKNOWN assessment rather than raising.

        A missing or short timeframe is a *fact about the data*, recorded as
        such. Raising here would lose the other three timeframes and the
        evaluation with them -- and an evaluation that says "the 5m series is too
        short" is worth storing.
        """
        role = TIMEFRAME_ROLES.get(timeframe, timeframe.value)

        try:
            _, snapshot = await self._features.snapshot(
                symbol=instrument.symbol,
                timeframe=timeframe,
                as_of=as_of,
                adjustment=adjustment,
            )
        except InsufficientDataError:
            return (
                TimeframeAssessment(
                    timeframe=timeframe, role=role, quality=DataQuality.INSUFFICIENT
                ),
                {},
            )
        except InstrumentNotFoundError:
            return (
                TimeframeAssessment(timeframe=timeframe, role=role, quality=DataQuality.MISSING),
                {},
            )

        quality = self._quality_for(snapshot.timestamp, as_of, timeframe)
        structure = await self._structure(instrument=instrument, timeframe=timeframe, as_of=as_of)

        ema_spread = snapshot.get("ema_spread_20_50")
        assessment = TimeframeAssessment(
            timeframe=timeframe,
            role=role,
            quality=quality,
            trend=classify_trend(ema_spread_pct=ema_spread, structure=structure, quality=quality),
            structure=structure.state if structure else StructureState.UNKNOWN,
            bar_timestamp=snapshot.timestamp,
            bars_used=snapshot.bars_used,
            close=snapshot.close,
            ema_spread_pct=ema_spread,
            rsi=snapshot.get("rsi_14"),
            atr_pct=snapshot.get("atr_pct_14"),
            relative_volume=snapshot.get("rel_volume_20"),
            volatility=snapshot.get("volatility_20"),
            structure_metrics=structure,
        )
        return assessment, dict(snapshot.values)

    def _quality_for(
        self, bar_timestamp: datetime, as_of: datetime, timeframe: Timeframe
    ) -> DataQuality:
        """Fresh enough to act on?

        The tolerance **scales with the timeframe**, and getting this wrong is
        not subtle: a daily bar is by definition up to a day old, so a flat
        30-minute limit marks every daily series permanently stale, drags the
        whole multi-timeframe context down with it (quality is the worst of the
        four), and the scanner silently never qualifies anything. Ever.

        So the limit is the larger of the configured floor and a small multiple
        of the bar interval -- late by two bars is late on any timeframe, and
        that means the same thing at 5 minutes as at one day.

        Age is measured against the evaluation instant rather than wall-clock
        now, so a historical or replayed scan is judged by what it could actually
        have known.
        """
        if self._max_data_age is None:
            return DataQuality.OK
        tolerance = max(self._max_data_age, timeframe.duration * STALE_BAR_MULTIPLE)
        return DataQuality.OK if as_of - bar_timestamp <= tolerance else DataQuality.STALE

    async def _structure(
        self, *, instrument: Instrument, timeframe: Timeframe, as_of: datetime
    ) -> StructureMetrics | None:
        """Structure metrics from a bounded window of raw bars.

        Raw prices, matching the feature pipeline's own adjustment handling: the
        window is short enough that a split inside it would be visible as a gap
        rather than silently distorting a range.
        """
        rows = await self._candles.get_latest(
            instrument_id=instrument.id,
            timeframe=timeframe,
            limit=STRUCTURE_BARS,
            as_of=as_of,
        )
        if not rows:
            return None
        ordered = sorted(rows, key=lambda r: r.timestamp)
        return analyse_structure(
            highs=[float(r.high) for r in ordered],
            lows=[float(r.low) for r in ordered],
            closes=[float(r.close) for r in ordered],
            lookback=STRUCTURE_LOOKBACK,
        )
