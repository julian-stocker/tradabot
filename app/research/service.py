"""Generating and maturing outcome labels.

Re-runnable by design. Most recent observations cannot be labelled at any
horizon longer than their own age, so a single pass would permanently mark them
zero or drop them; instead they are written ``PENDING`` and a later run fills
them in. The job is therefore idempotent in the strong sense -- running it twice
changes nothing, running it tomorrow completes what yesterday could not.

Reads the market-data tables and writes only research tables. It never touches
``tracked_signals``, ``scan_runs``, portfolios or notifications, so it is safe to
run while the live scheduler is scanning (part AG).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.time import ensure_utc, utc_now
from app.db.models import Candle, Instrument, SignalEvaluation, SignalOutcome
from app.domain.enums import Horizon, LabelStatus, Side, Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.market_data.repository import CandleRepository
from app.research.horizons import (
    LABEL_POLICY_VERSION,
    LABEL_TIMEFRAMES,
    SUPPORTED_HORIZONS,
    resolve,
)
from app.research.labels import MarketOutcome, compute_market_outcome
from app.research.repository import EvaluationCursor, OutcomeRepository

logger = get_logger(__name__)

DEFAULT_CHUNK_SIZE = 250
"""Evaluations held in memory at once. Bounded on purpose -- see part AH."""

DEFAULT_TARGET_PCT = 0.02
DEFAULT_STOP_PCT = 0.01
"""Barrier levels for market-outcome labelling, as fractions of the reference.

A fixed 2:1 pair rather than the live risk profile's ATR-derived levels, because
a *market* label must mean the same thing for every observation. Letting the
barrier float with volatility would make ``TARGET_FIRST`` a different question
per row and the aggregate uninterpretable. Execution-aware barriers belong to the
trade outcome, where the risk profile properly applies.
"""


@dataclass(slots=True)
class LabelReport:
    """What one labelling pass did."""

    evaluations_seen: int = 0
    labels_written: int = 0
    labels_completed: int = 0
    labels_pending: int = 0
    labels_insufficient: int = 0
    matured: int = 0
    """Rows that were PENDING and are now COMPLETE."""
    skipped_complete: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{self.evaluations_seen} evaluations, {self.labels_written} labels "
            f"({self.labels_completed} complete, {self.labels_pending} pending, "
            f"{self.labels_insufficient} insufficient), {self.matured} matured"
        )


class OutcomeLabellingService:
    """Computes market outcomes for stored evaluations."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        horizons: tuple[Horizon, ...] = SUPPORTED_HORIZONS,
        target_pct: float = DEFAULT_TARGET_PCT,
        stop_pct: float = DEFAULT_STOP_PCT,
    ) -> None:
        self._session = session
        self._candles = CandleRepository(session)
        self._instruments = InstrumentRepository(session)
        self._outcomes = OutcomeRepository(session)
        self._cursor = EvaluationCursor(session)
        self._horizons = horizons
        self._target_pct = target_pct
        self._stop_pct = stop_pct
        self._calendars: dict[str, TradingCalendar] = {}
        self._instrument_cache: dict[int, Instrument] = {}

    async def generate(
        self,
        *,
        now: datetime | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        instrument_ids: list[int] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        recompute: bool = False,
    ) -> LabelReport:
        """Label every matching evaluation, in bounded chunks.

        Args:
            recompute: also revisit rows already ``COMPLETE``. Off by default so
                a scheduled run costs only what has changed.
        """
        moment = ensure_utc(now) if now is not None else utc_now()
        report = LabelReport()
        after_id = 0

        while True:
            chunk = await self._cursor.chunk(
                after_id=after_id,
                limit=chunk_size,
                since=since,
                until=until,
                instrument_ids=instrument_ids,
            )
            if not chunk:
                break
            after_id = chunk[-1].id

            for evaluation in chunk:
                report.evaluations_seen += 1
                try:
                    await self._label_one(
                        evaluation, now=moment, report=report, recompute=recompute
                    )
                except Exception as exc:
                    report.errors.append(f"evaluation {evaluation.id}: {exc}")
                    logger.warning(
                        "outcome labelling failed", evaluation_id=evaluation.id, error=str(exc)
                    )

            # Commit per chunk: one transaction spanning the whole job would hold
            # a SQLite write lock against the live scanner for its duration.
            await self._session.commit()

        return report

    async def _label_one(
        self,
        evaluation: SignalEvaluation,
        *,
        now: datetime,
        report: LabelReport,
        recompute: bool,
    ) -> None:
        existing = await self._outcomes.existing_for(
            evaluation_id=evaluation.id, label_policy_version=LABEL_POLICY_VERSION
        )
        instrument = await self._instrument(evaluation.instrument_id)
        if instrument is None:
            report.errors.append(f"evaluation {evaluation.id}: unknown instrument")
            return

        reference_price = _reference_price(evaluation)
        if reference_price is None:
            report.errors.append(f"evaluation {evaluation.id}: no reference price")
            return

        calendar = self._calendar(instrument.exchange)
        reference_time = ensure_utc(evaluation.evaluated_at)

        for horizon in self._horizons:
            key = (horizon.value, 1)
            prior = existing.get(key)
            if prior is not None and prior.status == LabelStatus.COMPLETE.value and not recompute:
                report.skipped_complete += 1
                continue

            # Read the prior status *before* the upsert. ``prior`` and the row
            # the upsert mutates are the same object in the session's identity
            # map, so afterwards it already reports the new status and every
            # maturation would be counted as zero.
            previous_status = prior.status if prior is not None else None

            outcome, rolled = await self._compute(
                instrument=instrument,
                horizon=horizon,
                reference_time=reference_time,
                reference_price=reference_price,
                calendar=calendar,
                now=now,
            )
            row = _to_row(
                evaluation_id=evaluation.id,
                outcome=outcome,
                rolled_to_next_session=rolled,
            )
            await self._outcomes.upsert(row)

            report.labels_written += 1
            if (
                previous_status is not None
                and previous_status != LabelStatus.COMPLETE.value
                and outcome.status is LabelStatus.COMPLETE
            ):
                report.matured += 1
            if outcome.status is LabelStatus.COMPLETE:
                report.labels_completed += 1
            elif outcome.status is LabelStatus.PENDING:
                report.labels_pending += 1
            else:
                report.labels_insufficient += 1

    async def _compute(
        self,
        *,
        instrument: Instrument,
        horizon: Horizon,
        reference_time: datetime,
        reference_price: Decimal,
        calendar: TradingCalendar,
        now: datetime,
    ) -> tuple[MarketOutcome, bool]:
        """Resolve the horizon, fetch its window and label it."""
        resolved = resolve(horizon, reference=reference_time, calendar=calendar)
        if resolved is None:
            return (
                MarketOutcome(
                    horizon=horizon,
                    status=LabelStatus.INSUFFICIENT_FUTURE_DATA,
                    reference_timestamp=reference_time,
                    reference_price=reference_price,
                ),
                False,
            )

        if resolved.target > now:
            # The horizon has not elapsed, so it is PENDING -- and the bars are
            # not consulted at all. Reading them would be the labeller's own
            # look-ahead: the database may already hold bars past ``now`` (a
            # backfill, or a replay of an old window), and using them would
            # compute a label from data that did not exist at label time. It
            # would also make every horizon complete immediately, which is
            # exactly the bug this guard replaced.
            return (
                MarketOutcome(
                    horizon=horizon,
                    status=LabelStatus.PENDING,
                    reference_timestamp=reference_time,
                    reference_price=reference_price,
                ),
                resolved.rolled_to_next_session,
            )

        timeframe, bars = await self._window(
            instrument=instrument,
            horizon=horizon,
            start=reference_time,
            end=resolved.target,
        )

        target_price = reference_price * Decimal(str(1 + self._target_pct))
        stop_price = reference_price * Decimal(str(1 - self._stop_pct))

        outcome = compute_market_outcome(
            horizon=horizon,
            reference_timestamp=reference_time,
            reference_price=reference_price,
            future_bars=bars,
            label_timeframe=timeframe,
            target_price=target_price,
            stop_price=stop_price,
            side=Side.LONG,
            horizon_elapsed=True,
        )
        return outcome, resolved.rolled_to_next_session

    async def _window(
        self,
        *,
        instrument: Instrument,
        horizon: Horizon,
        start: datetime,
        end: datetime,
    ) -> tuple[Timeframe, list[Candle]]:
        """Bars in ``(start, end]`` on the finest series that covers the window.

        Preference order comes from :data:`LABEL_TIMEFRAMES`. A series is only
        accepted if it has a bar at or after the horizon target -- otherwise the
        window is truncated and the "return" would be measured to whenever the
        data happened to stop, which is a silently wrong number rather than a
        missing one.
        """
        preferences = LABEL_TIMEFRAMES.get(horizon, (Timeframe.D1,))
        fallback = preferences[-1]

        for timeframe in preferences:
            # ``get_range`` is half-open, so the end is pushed out by one bar to
            # keep a bar starting exactly at the target inside the window.
            bars = await self._candles.get_range(
                instrument_id=instrument.id,
                timeframe=timeframe,
                start=start,
                end=end + timeframe.duration,
                limit=5000,
            )
            covering = [bar for bar in bars if start < bar.timestamp <= end]
            if not covering:
                continue
            # Does this series actually reach the horizon? The last bar must
            # close at or after the target.
            last = covering[-1]
            if last.timestamp + timeframe.duration >= end:
                return timeframe, covering

        return fallback, []

    async def _instrument(self, instrument_id: int) -> Instrument | None:
        if instrument_id not in self._instrument_cache:
            found = await self._instruments.get_by_id(instrument_id)
            if found is None:
                return None
            self._instrument_cache[instrument_id] = found
        return self._instrument_cache[instrument_id]

    def _calendar(self, exchange: str) -> TradingCalendar:
        if exchange not in self._calendars:
            self._calendars[exchange] = get_trading_calendar(exchange)
        return self._calendars[exchange]


def _reference_price(evaluation: SignalEvaluation) -> Decimal | None:
    """The price the signal actually saw.

    Part I: the reference for a *market* outcome is the primary timeframe's close
    at evaluation time -- the last value the scoring engine consumed. Reaching for
    a quote or a later bar would measure a return the signal never had access to
    the start of.
    """
    states: dict[str, Any] = evaluation.timeframe_states or {}
    for key in (Timeframe.H1.value, Timeframe.M15.value, Timeframe.M5.value, Timeframe.D1.value):
        state = states.get(key)
        if isinstance(state, dict):
            close = state.get("close")
            if close is not None:
                return Decimal(str(close))
    return None


def _to_row(
    *, evaluation_id: int, outcome: MarketOutcome, rolled_to_next_session: bool
) -> SignalOutcome:
    barriers = outcome.barriers
    return SignalOutcome(
        evaluation_id=evaluation_id,
        horizon=outcome.horizon.value,
        status=outcome.status.value,
        direction=1,
        reference_timestamp=outcome.reference_timestamp,
        reference_price=outcome.reference_price,
        future_timestamp=outcome.future_timestamp,
        future_price=outcome.future_price,
        raw_return=outcome.raw_return,
        mfe=outcome.mfe,
        mae=outcome.mae,
        target_status=("HIT" if barriers.target_hit else "NOT_HIT") if barriers else None,
        stop_status=("HIT" if barriers.stop_hit else "NOT_HIT") if barriers else None,
        barrier_outcome=barriers.outcome.value if barriers else None,
        time_to_target_seconds=barriers.time_to_target_seconds if barriers else None,
        time_to_stop_seconds=barriers.time_to_stop_seconds if barriers else None,
        ambiguous_bar_timestamp=barriers.ambiguous_bar_timestamp if barriers else None,
        label_timeframe=outcome.label_timeframe.value if outcome.label_timeframe else None,
        bars_observed=outcome.bars_observed,
        rolled_to_next_session=rolled_to_next_session,
        label_policy_version=LABEL_POLICY_VERSION,
        computed_at=utc_now(),
    )
