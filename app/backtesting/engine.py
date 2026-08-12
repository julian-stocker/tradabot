"""Deterministic historical replay of the production scanner.

What this is
------------
A loop over historical instants that calls **the same** analyser, feature service
and signal engine the live scanner calls, with ``as_of`` set to the historical
moment instead of now. There is no "backtest strategy": a second implementation
would drift from production within a release or two, and every comparison
between the two would then be measuring the drift rather than the market.

What it deliberately does not do
--------------------------------
It does not advance the tracked-signal lifecycle, touch ``scan_runs``, open
positions in the live portfolios, or publish anything. Those are production
state, the scheduler owns them, and a research job that mutated them while a scan
was running would corrupt both. Observations are written to
``signal_evaluations`` tagged with ``backtest_run_id``, which every production
read filters out.

The event-time model (part C)
-----------------------------
For a primary-timeframe bar closing at ``T``:

* ``T`` is the **candle close** and the earliest instant its close price exists.
* the signal is evaluated **at** ``T`` from bars that finished at or before ``T``.
* the order decision is made at ``T``.
* the earliest executable instant is the **next bar's open**, strictly after
  ``T``.

So a 5-minute candle closing at 10:05 produces a signal timed 10:05 that fills at
the 10:05-10:10 bar's open -- never at the 10:00 open, which had already passed
when the information arrived. Where the two conventions differ, the pessimistic
one is taken: the gap between the signal bar's close and the next open is a real
cost that a close-fill backtest silently deletes.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtesting.modes import ModeResolution, resolve_mode
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import ensure_utc, utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import BacktestRun, Instrument
from app.db.session import session_scope
from app.domain.enums import Horizon, PriceSeriesAdjustment, Timeframe
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.market_data.repository import CandleRepository
from app.research.costs import COST_MODEL_VERSION
from app.research.horizons import LABEL_POLICY_VERSION
from app.research.repository import BacktestRunRepository
from app.scanner.analysis import (
    FEATURE_SET_VERSION,
    SCANNER_POLICY_VERSION,
    MultiTimeframeAnalyser,
)
from app.scanner.enums import DataQuality, SessionPhase
from app.scanner.repository import SignalEvaluationRepository
from app.scanner.service import SIGNAL_MODEL_VERSION, _build_evaluation
from app.scanner.sessions import session_phase
from app.scanner.timeframes import PRIMARY_TIMEFRAME, SCANNER_TIMEFRAMES
from app.signals.service import SignalService

logger = get_logger(__name__)

ENGINE_VERSION: Final = "backtest-v1"

DEFAULT_SYMBOL_CHUNK: Final = 8
"""Symbols grouped per pass. Bounded so progress is logged steadily (part AH)."""

GRID_CHUNK: Final = 200
"""Evaluation instants per write transaction.

**A production-safety bound, not a performance tuning knob.** The replay shares
one SQLite file with the live scheduler, and SQLite permits a single writer: a
transaction held open for the length of a symbol's four-year grid blocks the
five-minute market-data sync for as long as it runs. At roughly 18 observations a
second this keeps the work between commits near seven seconds and the commit
itself far shorter, which the scheduler's `busy_timeout` absorbs without
failing. Sized against that timeout deliberately: the two numbers are one
decision, and changing either alone reintroduces the stall.

Smaller would be safer still and measurably slower -- each slice reopens the
session and reloads the feature warm-up window, so the query cache goes cold at
every boundary.
"""


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything that determines a result.

    Frozen and hashed into :meth:`run_key`: two runs with the same configuration
    over the same immutable candles must produce the same numbers, and the key is
    what makes that checkable rather than asserted.
    """

    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    primary_timeframe: Timeframe = PRIMARY_TIMEFRAME
    regular_session_only: bool = True
    scope: str = "symbols"
    horizon: Horizon = Horizon.D5
    adjustment: PriceSeriesAdjustment = PriceSeriesAdjustment.SPLIT_ADJUSTED

    def run_key(self) -> str:
        """A digest of the configuration and every strategy-affecting version.

        Version fields are included on purpose: the same date range scored by a
        different signal model is a *different run*, and letting the two collide
        would silently overwrite one result with the other.
        """
        payload = {
            "symbols": sorted(self.symbols),
            "start": ensure_utc(self.start).isoformat(),
            "end": ensure_utc(self.end).isoformat(),
            "primary_timeframe": self.primary_timeframe.value,
            "regular_session_only": self.regular_session_only,
            "scope": self.scope,
            "horizon": self.horizon.value,
            "adjustment": self.adjustment.value,
            "engine": ENGINE_VERSION,
            "features": FEATURE_SET_VERSION,
            "signal": SIGNAL_MODEL_VERSION,
            "scanner": SCANNER_POLICY_VERSION,
            "costs": COST_MODEL_VERSION,
            "labels": LABEL_POLICY_VERSION,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def universe_definition(self) -> dict[str, Any]:
        return {"scope": self.scope, "symbols": sorted(self.symbols), "count": len(self.symbols)}


@dataclass(slots=True)
class ReplayStats:
    """Counts from one replay."""

    observations: int = 0
    symbols_processed: int = 0
    timestamps: int = 0
    skipped_insufficient: int = 0
    qualified: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def as_metrics(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "symbols_processed": self.symbols_processed,
            "timestamps": self.timestamps,
            "skipped_insufficient": self.skipped_insufficient,
            "qualified": self.qualified,
            "errors": len(self.errors),
        }


def evaluation_grid(
    *,
    start: datetime,
    end: datetime,
    timeframe: Timeframe,
    calendar: TradingCalendar,
    regular_session_only: bool = True,
) -> Iterator[datetime]:
    """Historical instants to evaluate at, ascending.

    Yields **bar closes**, not bar opens. A bar's close price is the first moment
    it exists, so evaluating at the open would score a candle from the future --
    the same confusion the ``as_of`` filter was fixed to prevent, one level up.

    Restricted to the regular session by default (part L). Extended-hours
    observations on the IEX feed are dominated by the feed's thinness rather than
    by the market, and mixing them into a benchmark would make the benchmark
    partly a measurement of after-hours quoting.
    """
    step = timeframe.duration
    moment = _align(ensure_utc(start), step)
    finish = ensure_utc(end)

    while moment <= finish:
        if not regular_session_only or calendar.is_open_at(moment - step):
            # `moment` is a close; the bar it closes covers [moment-step, moment).
            # Judging the session by the bar's own span keeps a bar that closes
            # exactly at 20:00 inside the session it belongs to.
            yield moment
        moment += step


def _align(moment: datetime, step: timedelta) -> datetime:
    """Round up to the next multiple of ``step`` past the epoch."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = moment - epoch
    units = -(-elapsed // step)  # ceiling division
    return epoch + units * step


class HistoricalReplay:
    """Replays stored candles through the production evaluation path."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        provider: Any = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._provider = provider
        self._calendar = get_trading_calendar(settings.market_data.default_exchange)

    async def run(self, config: BacktestConfig) -> tuple[BacktestRun, ReplayStats]:
        """Execute a replay and persist its metadata.

        The run row is written first and completed last, so a crash leaves a
        ``RUNNING`` record naming the configuration rather than no evidence at
        all that the job was attempted.
        """
        started = time.perf_counter()
        stats = ReplayStats()

        async with session_scope(self._factory) as session:
            resolution = await self._resolve_mode(session, config)
            run = await BacktestRunRepository(session).create(
                _new_run(config, started_at=utc_now(), resolution=resolution)
            )
            run_id = run.id

        logger.info(
            "replay mode resolved",
            mode=resolution.mode.value,
            available=[t.value for t in resolution.available],
            missing=[t.value for t in resolution.missing],
            detail=resolution.detail,
        )

        try:
            await self._replay(config, run_id=run_id, stats=stats)
        except Exception as exc:
            message = safe_message(exc)
            async with session_scope(self._factory) as session:
                repo = BacktestRunRepository(session)
                failed = await repo.get(run_id)
                if failed is not None:
                    await repo.fail(failed, error=message)
            raise

        stats.duration_seconds = time.perf_counter() - started
        async with session_scope(self._factory) as session:
            repo = BacktestRunRepository(session)
            stored = await repo.get(run_id)
            if stored is not None:
                await repo.complete(
                    stored,
                    observation_count=stats.observations,
                    symbols_processed=stats.symbols_processed,
                    metrics=stats.as_metrics(),
                    duration_seconds=stats.duration_seconds,
                )

        async with session_scope(self._factory) as session:
            final = await BacktestRunRepository(session).get(run_id)
            assert final is not None
            return final, stats

    async def _replay(self, config: BacktestConfig, *, run_id: int, stats: ReplayStats) -> None:
        """Symbol-major, so each symbol's warm-up query cache stays hot.

        Symbol-major rather than time-major is safe here precisely because every
        observation is independent: nothing in this loop carries state from one
        instant to the next, since portfolio state belongs to the execution pass
        (:mod:`app.backtesting.execution`), not to signal generation. Ordering
        therefore cannot change the output -- which is what makes the replay
        deterministic under chunking.
        """
        grid = list(
            evaluation_grid(
                start=config.start,
                end=config.end,
                timeframe=config.primary_timeframe,
                calendar=self._calendar,
                regular_session_only=config.regular_session_only,
            )
        )
        stats.timestamps = len(grid)
        if not grid:
            return

        for batch in _chunks(list(config.symbols), DEFAULT_SYMBOL_CHUNK):
            for symbol in batch:
                written = 0
                failed = False
                # Sliced so no single write transaction is long. SQLAlchemy
                # autoflushes before each read, so the first insert opens the
                # write transaction and it stays open until commit -- over a
                # four-year window that was **53 minutes**, during which the live
                # market-data sync could not write and logged `database is
                # locked`. Research must never be able to starve production.
                for window in _chunks_of(grid, GRID_CHUNK):
                    try:
                        async with session_scope(self._factory) as session:
                            written += await self._replay_symbol(
                                session=session,
                                symbol=symbol,
                                grid=window,
                                config=config,
                                run_id=run_id,
                                stats=stats,
                            )
                    except Exception as exc:
                        message = safe_message(exc)
                        stats.errors.append(f"{symbol}: {message}")
                        logger.warning("backtest symbol failed", symbol=symbol, error=message)
                        failed = True
                        break
                if failed:
                    continue
                stats.observations += written
                stats.symbols_processed += 1
                logger.debug("replayed symbol", symbol=symbol, observations=written)

    async def _replay_symbol(
        self,
        *,
        session: AsyncSession,
        symbol: str,
        grid: list[datetime],
        config: BacktestConfig,
        run_id: int,
        stats: ReplayStats,
    ) -> int:
        # **Autoflush off, deliberately.** Otherwise SQLAlchemy flushes pending
        # observations before every candle read, which opens the SQLite write
        # transaction on the first evaluation and holds it for the whole slice --
        # tens of seconds, against a live scheduler whose `busy_timeout` is five.
        # Nothing here reads back what it writes: each observation is computed
        # from candles alone and is independent of every other, so there is no
        # pending state a query could need. The lock is now taken once, at commit.
        session.autoflush = False

        instruments = InstrumentRepository(session)
        instrument = await instruments.get_by_symbol(symbol)
        if instrument is None:
            stats.errors.append(f"{symbol}: instrument not found")
            return 0

        features = FeatureService(
            InstrumentService(instruments),
            CandleRepository(session),
            CorporateActionRepository(session),
        )
        analyser = MultiTimeframeAnalyser(
            session,
            features,
            max_data_age=timedelta(minutes=self._settings.scanner.max_data_age_minutes),
        )
        signals = SignalService(features, self._provider, self._settings)
        evaluations = SignalEvaluationRepository(session)

        written = 0
        for moment in grid:
            observation = await self._observe(
                instrument=instrument,
                analyser=analyser,
                signals=signals,
                moment=moment,
                config=config,
                run_id=run_id,
            )
            if observation is None:
                stats.skipped_insufficient += 1
                continue
            if observation.qualified:
                stats.qualified += 1
            await evaluations.record(observation)
            written += 1

        return written

    async def _observe(
        self,
        *,
        instrument: Instrument,
        analyser: MultiTimeframeAnalyser,
        signals: SignalService,
        moment: datetime,
        config: BacktestConfig,
        run_id: int,
    ) -> Any:
        """One observation, using only what was knowable at ``moment``.

        No quote is passed. Historical bid/ask does not exist in this database,
        and reaching for the *current* quote would apply a 2026 spread to a
        February fill -- look-ahead dressed up as realism. Execution cost is
        modelled later, from the observation's own volatility and session, and
        stamped ``MODELLED`` so it can never be read as measured.
        """
        analysis = await analyser.analyse(
            instrument=instrument, as_of=moment, adjustment=config.adjustment
        )
        primary = analysis.context.get(config.primary_timeframe)
        if primary is None or primary.quality is DataQuality.MISSING:
            return None

        signal = None
        if primary.quality in (DataQuality.OK, DataQuality.STALE):
            signal = await signals.evaluate(
                symbol=instrument.symbol,
                timeframe=config.primary_timeframe,
                horizon=config.horizon,
                as_of=moment,
                adjustment=config.adjustment,
            )
        if signal is None:
            return None

        phase = session_phase(self._calendar, moment)
        quality = analysis.context.quality
        score = float(signal.score)
        actionable = quality.is_actionable and (
            phase.is_tradable or not config.regular_session_only
        )
        qualified = actionable and score >= self._settings.notifications.signal_threshold

        evaluation = _build_evaluation(
            instrument=instrument,
            analysis=analysis,
            signal=signal,
            quote=None,
            now=moment,
            run_id=None,
            tracked_id=None,
            phase=phase,
            quality=quality,
            qualified=qualified,
            score=score,
            confidence=float(signal.confidence),
            classification=signal.classification.value,
        )
        # The isolation marker. Set here and nowhere else, so a row written by
        # this engine is always distinguishable from one the scheduler wrote.
        evaluation.backtest_run_id = run_id
        return evaluation

    async def _resolve_mode(self, session: AsyncSession, config: BacktestConfig) -> ModeResolution:
        """Measure which timeframes actually cover this window.

        Measured rather than configured on purpose -- see
        :mod:`app.backtesting.modes`. A caller cannot mislabel a run, because
        there is nothing for a caller to label.
        """
        candles = CandleRepository(session)
        instruments = InstrumentRepository(session)
        coverage: dict[Timeframe, tuple[datetime | None, datetime | None]] = {}

        # One representative symbol is enough and is deliberately cheap: history
        # depth here is a property of the *provider's* retention, not of any
        # individual listing, and scanning 52 symbols to rediscover the same
        # 2020-07-27 floor would cost minutes per run to learn nothing.
        probe = await instruments.get_by_symbol(config.symbols[0])
        for timeframe in SCANNER_TIMEFRAMES:
            if probe is None:
                coverage[timeframe] = (None, None)
                continue
            coverage[timeframe] = (
                await candles.earliest_timestamp(instrument_id=probe.id, timeframe=timeframe),
                await candles.latest_timestamp(instrument_id=probe.id, timeframe=timeframe),
            )
        return resolve_mode(coverage, start=config.start, end=config.end)


def _new_run(
    config: BacktestConfig, *, started_at: datetime, resolution: ModeResolution
) -> BacktestRun:
    return BacktestRun(
        replay_mode=resolution.mode.value,
        available_timeframes=",".join(t.value for t in resolution.available),
        run_key=config.run_key(),
        started_at=started_at,
        from_timestamp=ensure_utc(config.start),
        to_timestamp=ensure_utc(config.end),
        universe_definition=config.universe_definition(),
        primary_timeframe=config.primary_timeframe.value,
        regular_session_only=config.regular_session_only,
        feature_set_version=FEATURE_SET_VERSION,
        signal_model_version=SIGNAL_MODEL_VERSION,
        scanner_policy_version=SCANNER_POLICY_VERSION,
        cost_model_version=COST_MODEL_VERSION,
        label_policy_version=LABEL_POLICY_VERSION,
        engine_version=ENGINE_VERSION,
        status="RUNNING",
    )


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _chunks_of(items: list[datetime], size: int) -> Iterator[list[datetime]]:
    """Grid slices. Separate from :func:`_chunks` only because mypy --strict
    will not accept one generic helper without a TypeVar that buys nothing here."""
    for index in range(0, len(items), size):
        yield items[index : index + size]


__all__ = [
    "ENGINE_VERSION",
    "BacktestConfig",
    "HistoricalReplay",
    "ReplayStats",
    "SessionPhase",
    "evaluation_grid",
]
