"""Running #market-trends against what the scanner already stored.

**No provider call happens here.** Every input is read from SQLite: the metrics
come from ``signal_evaluations`` rows the scan cycle persisted, and the daily
change comes from candles the sync job already downloaded. A second fetch would
cost API quota to re-learn something the database was told fifteen minutes ago,
and would make the trends job able to break the market-data budget.

This mirrors :meth:`ScannerService.top_candidates`, which reads persisted
evaluations rather than rescanning for the same reason: an overview -- and a
trend summary -- is a view of what the last scan found, and recomputing it would
answer a different question.

Separation from the scanner
---------------------------
This runs as its own scheduled job rather than at the end of the scan cycle.
Wrapping the publication in ``try/except`` inside the scanner would contain an
*exception*, but not a hang: an HTTP call that stalls for two minutes would hold
the scan lease and delay the next cycle. A separate process cannot do that to the
scanner no matter how it fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.events import Event, EventType
from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.db.session import session_scope
from app.domain.enums import Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.calendars import get_trading_calendar
from app.market_data.repository import CandleRepository
from app.market_data.volatility import VolatilityRegime
from app.market_data.volatility_service import VolatilityService
from app.notifications.feeds import TRENDS_ROUTING_KEY
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.notifications.trends import (
    TrendEvent,
    TrendSignal,
    assert_no_recommendation_language,
    build_payload,
    detect,
    rank,
    session_allows_trends,
    should_notify,
)
from app.notifications.volatility_events import (
    VolatilityEvent,
    build_section,
    detect_events,
    next_state,
)
from app.scanner.repository import SignalEvaluationRepository, WatchlistRepository
from app.scanner.sessions import session_phase

logger = get_logger(__name__)

MAX_OBSERVATION_AGE: Final = timedelta(hours=2)
"""How old the last scan may be before its metrics are not worth announcing.

An observation is a claim about *now*. If the scanner has been down since this
morning, "NVDA is up 4%" may have stopped being true hours ago, and repeating it
from stale rows would put tradabot's name on a stale fact.
"""

CHANGE_LOOKBACK: Final = 6
"""Daily bars needed for a 1-day and 5-day change: five gaps plus the current bar."""


@dataclass(frozen=True, slots=True)
class TrendsRun:
    """What one trends evaluation did. **The observability record.**

    Answers, without a second table: when it ran, how many symbols it looked at,
    how much was notable, how much the cooldown swallowed, and how many messages
    actually went out. Suppression is counted rather than inferred -- otherwise
    "the channel is quiet" and "the job is broken" look identical from outside.
    """

    evaluated_at: datetime
    session: str
    published: bool = False
    skipped_reason: str | None = None
    symbols_considered: int = 0
    events_detected: int = 0
    events_suppressed: int = 0
    events_published: int = 0
    messages_sent: int = 0
    signals: list[TrendSignal] = field(default_factory=list)
    volatility_events: list[VolatilityEvent] = field(default_factory=list)
    volatility_elevated: int = 0
    volatility_evaluated: int = 0

    def summary(self) -> str:
        if self.skipped_reason:
            return f"skipped ({self.skipped_reason})"
        return (
            f"{self.symbols_considered} symbols, {self.events_detected} notable, "
            f"{self.events_suppressed} suppressed, {self.events_published} published, "
            f"{len(self.volatility_events)} volatility transition(s) "
            f"({self.volatility_elevated} elevated), "
            f"{self.messages_sent} message(s) sent"
        )


class TrendsService:
    """Evaluates and publishes descriptive market activity.

    ``evaluate`` never sends anything and never writes state, so ``--preview``
    is the same code path the scheduler runs rather than a parallel rendering
    that could drift from it.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Settings,
        notifications: NotificationService | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        self._notifications = notifications

    async def evaluate(self, *, now: datetime | None = None) -> TrendsRun:
        """What is notable right now. **Reads only** -- sends nothing, writes nothing."""
        moment = now or utc_now()
        calendar = get_trading_calendar(self._settings.market_data.default_exchange)
        phase = session_phase(calendar, moment)

        allowed, reason = session_allows_trends(phase)
        if not allowed:
            return TrendsRun(evaluated_at=moment, session=phase.value, skipped_reason=reason)

        async with session_scope(self._factory) as session:
            signals, considered, freshness = await self._observe(session, now=moment)
            volatility, elevated, evaluated = await self._observe_volatility(session, now=moment)

        # A symbol whose regime changed is already reported in the volatility
        # section; its generic volatility-expansion trend signal would be a
        # second line about the same state in the same post.
        announced = {event.symbol for event in volatility}
        signals = [
            signal
            for signal in signals
            if not (signal.event is TrendEvent.VOLATILITY_EXPANSION and signal.symbol in announced)
        ]

        if freshness is not None:
            return TrendsRun(
                evaluated_at=moment,
                session=phase.value,
                symbols_considered=considered,
                skipped_reason=freshness,
            )

        return TrendsRun(
            evaluated_at=moment,
            session=phase.value,
            symbols_considered=considered,
            events_detected=len(signals),
            signals=signals,
            volatility_events=volatility,
            volatility_elevated=elevated,
            volatility_evaluated=evaluated,
        )

    async def publish(self, *, now: datetime | None = None) -> TrendsRun:
        """Evaluate, apply the cooldown, and send what survives.

        Returns the run either way. **Never raises**: this is scheduled work, and
        a trends failure must not become a non-zero exit that an operator learns
        to ignore -- or worse, a launchd job that retries in a tight loop.
        """
        moment = now or utc_now()
        run = await self.evaluate(now=moment)
        if run.skipped_reason or not (run.signals or run.volatility_events):
            logger.info("trends evaluated", session=run.session, outcome=run.summary())
            return run

        fresh = await self._filter_by_cooldown(run.signals, now=moment)
        suppressed = len(run.signals) - len(fresh)

        if not fresh and not run.volatility_events:
            # Silence is the expected state. No "nothing found" message: a channel
            # that speaks when there is nothing to say trains its reader to skim.
            logger.info(
                "trends evaluated; all suppressed",
                session=run.session,
                symbols=run.symbols_considered,
                detected=run.events_detected,
                suppressed=suppressed,
            )
            return replace(run, events_suppressed=suppressed)

        top = rank(fresh, limit=self._settings.scanner.top_candidates)
        sent = await self._send(top, run=run, now=moment)

        if sent:
            await self._remember(top, now=moment)
            # Regime state advances only on delivery, matching the cooldown rule:
            # a transition nobody saw must still be announceable next cycle.
            await self._remember_volatility(run, now=moment)

        result = replace(
            run,
            events_suppressed=suppressed,
            events_published=len(top),
            messages_sent=1 if sent else 0,
            published=sent,
        )
        logger.info(
            "trends published" if sent else "trends delivery failed",
            session=run.session,
            symbols=run.symbols_considered,
            detected=run.events_detected,
            suppressed=suppressed,
            published=len(top),
        )
        return result

    # -- Internals ---------------------------------------------------------

    async def _observe(
        self, session: AsyncSession, *, now: datetime
    ) -> tuple[list[TrendSignal], int, str | None]:
        """Detect over the last scan's persisted evaluations."""
        evaluations = await SignalEvaluationRepository(session).latest_per_instrument()
        if not evaluations:
            return [], 0, "no evaluations recorded yet"

        newest = max(row.evaluated_at for row in evaluations)
        if now - newest > MAX_OBSERVATION_AGE:
            return [], len(evaluations), f"last scan was {_hours(now - newest)} ago"

        instruments = InstrumentRepository(session)
        candles = CandleRepository(session)
        found: list[TrendSignal] = []

        for row in evaluations:
            instrument = await instruments.get_by_id(row.instrument_id)
            if instrument is None:  # pragma: no cover -- FK guarantees this
                continue
            change_1d, change_5d = await _changes(candles, instrument_id=row.instrument_id)
            found.extend(
                detect(
                    symbol=instrument.symbol,
                    change_1d_pct=change_1d,
                    change_5d_pct=change_5d,
                    relative_volume=_metric(row.volume_metrics, "relative_volume"),
                    volatility=_metric(row.volatility_metrics, "volatility"),
                    structure_state=_state(row.structure_metrics),
                )
            )
        return found, len(evaluations), None

    async def _observe_volatility(
        self, session: AsyncSession, *, now: datetime
    ) -> tuple[list[VolatilityEvent], int, int]:
        """Regime transitions since the last cycle.

        Reads stored candles through the same engine the CLI and the status
        dashboard use, so a volatility alert and the `Volatility` status line can
        never disagree about what the regime is.
        """
        symbols = await WatchlistRepository(session).symbols()
        snapshot = await VolatilityService(session).for_symbols(symbols, now=now)
        previous_raw = await NotificationRepository(session).volatility_regimes()
        previous = {symbol: _regime_or_none(value) for symbol, value in previous_raw.items()}
        events = detect_events(snapshot.estimates, previous, now=now)
        return events, len(snapshot.elevated), len(snapshot.estimates)

    async def _remember_volatility(self, run: TrendsRun, *, now: datetime) -> None:
        if not run.volatility_events:
            return
        estimates = [event.movement for event in run.volatility_events]
        async with session_scope(self._factory) as session:
            await NotificationRepository(session).save_volatility_regimes(
                next_state(estimates, now=now)
            )

    async def _filter_by_cooldown(
        self, signals: list[TrendSignal], *, now: datetime
    ) -> list[TrendSignal]:
        async with session_scope(self._factory) as session:
            repository = NotificationRepository(session)
            keep: list[TrendSignal] = []
            for signal in signals:
                state = await repository.trend_state(signal.key)
                if should_notify(signal, state, now=now):
                    keep.append(signal)
        return keep

    async def _remember(self, signals: list[TrendSignal], *, now: datetime) -> None:
        """Record what was announced -- **only after** it was delivered.

        Same rule as :meth:`NotificationService.notify_signal`: a cooldown started
        by a message that never arrived would silence the retry for four hours.
        """
        async with session_scope(self._factory) as session:
            repository = NotificationRepository(session)
            for signal in signals:
                await repository.save_trend_state(signal.key, value=signal.value, notified_at=now)

    async def _send(self, signals: list[TrendSignal], *, run: TrendsRun, now: datetime) -> bool:
        if self._notifications is None:
            return False

        payload = build_payload(
            signals, context={"session": run.session, "symbols": run.symbols_considered}
        )
        if run.volatility_events:
            # **One post, two sections.** Two messages per cycle would double the
            # notification count for a channel whose whole design is restraint.
            payload["volatility"] = build_section(
                run.volatility_events, elevated_total=run.volatility_elevated
            )
        # Checked before it leaves, on the rendered text rather than the template:
        # the guarantee is about what lands in Discord, not about what a formatter
        # intended.
        assert_no_recommendation_language(
            " ".join(str(mover) for mover in payload.get("movers", []))
        )

        return await self._notifications.publish(
            Event(
                type=EventType.MARKET_TRENDS,
                occurred_at=now,
                payload=payload,
                key=f"trends:{now:%Y-%m-%dT%H:%M}",
                # Dedicated destination. Never falls back to #market-signals:
                # `market-trends` is not a feed key, so an unconfigured webhook
                # means silence rather than trend text in a signals channel.
                routing_key=TRENDS_ROUTING_KEY,
            )
        )


async def _changes(
    candles: CandleRepository, *, instrument_id: int
) -> tuple[float | None, float | None]:
    """1-day and 5-day percentage change from stored daily bars.

    ``as_of`` is deliberately **not** passed. That filter exists to keep research
    honest by excluding bars that had not closed yet, and here the still-forming
    daily bar is exactly the subject: "NVDA is up 4% today" is a statement about
    the bar in progress. Nothing computed here reaches a research dataset.
    """
    try:
        bars = await candles.get_latest(
            instrument_id=instrument_id, timeframe=Timeframe.D1, limit=CHANGE_LOOKBACK
        )
    # Missing history is normal for a newly added symbol, not an error.
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("no daily candles for trend change", error=safe_message(exc))
        return None, None

    if len(bars) < 2:  # noqa: PLR2004
        return None, None

    latest = float(bars[-1].close)
    change_1d = _pct(latest, float(bars[-2].close))
    change_5d = _pct(latest, float(bars[0].close)) if len(bars) >= CHANGE_LOOKBACK else None
    return change_1d, change_5d


def _pct(latest: float, base: float) -> float | None:
    return None if base == 0 else (latest / base - 1.0) * 100.0


def _metric(metrics: dict[str, Any] | None, key: str) -> float | None:
    value = (metrics or {}).get(key)
    return float(value) if isinstance(value, int | float) else None


def _state(metrics: dict[str, Any] | None) -> str | None:
    value = (metrics or {}).get("state")
    return str(value) if isinstance(value, str) else None


def _hours(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f}h" if hours >= 1 else f"{delta.total_seconds() / 60:.0f}m"


def _regime_or_none(value: str) -> VolatilityRegime | None:
    """Stored regime string to enum, tolerating a value written by an older model."""
    try:
        return VolatilityRegime(value)
    except ValueError:
        return None
