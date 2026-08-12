"""The scan cycle.

``run_scan_cycle(as_of)`` does one pass over the watchlist and returns
statistics. It does **not** sleep, does **not** loop, and does **not** schedule
itself. An external scheduler -- cron, a systemd timer, a future supervisor --
decides when to call it. A domain service that owned its own clock would be
untestable without waiting and unkillable mid-cycle.

Per-symbol transaction boundary
-------------------------------
::

    for each symbol:
        calculate → persist → COMMIT → notify

Four consequences, each deliberate:

* **One symbol's failure does not abort the scan.** Its transaction rolls back;
  the other forty-nine are already committed.
* **Discord cannot roll back data.** Notifications are sent after the commit, so
  a delivery failure leaves the evaluation stored -- the priority stated in the
  phase brief and enforced by :meth:`NotificationService.publish` never raising.
* **A crash loses at most one symbol's work.**
* **Every evaluation is persisted**, qualified or not, notified or not. A
  rejected candidate is training data; see :mod:`app.db.models.scanner`.

What the scanner does not claim
-------------------------------
Zero qualified signals is a valid, common and *correct* result. Nothing here
lowers a threshold to produce activity, and a busy channel is evidence of a
threshold rather than of an opportunity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.errors import ProviderError
from app.core.events import Event
from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Instrument, ScanRun, SignalEvaluation
from app.db.session import session_scope
from app.domain.enums import Horizon, PriceSeriesAdjustment, Timeframe
from app.domain.quotes import Quote
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.market_data.import_service import MarketDataImportService
from app.market_data.provider import MarketDataProvider
from app.market_data.quality import quote_age_seconds
from app.market_data.repository import CandleRepository
from app.notifications.service import NotificationService
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.paper.service import PaperTradingService
from app.scanner.analysis import (
    FEATURE_SET_VERSION,
    SCANNER_POLICY_VERSION,
    AnalysisResult,
    MultiTimeframeAnalyser,
)
from app.scanner.enums import DataQuality, SessionPhase, SignalLifecycle
from app.scanner.horizons import TradingHorizon, classify_horizons
from app.scanner.lifecycle import (
    SignalIdentity,
    direction_label,
    evaluate_lifecycle,
    setup_for,
)
from app.scanner.ranking import RankedCandidate, rank_candidates, rank_score
from app.scanner.repository import (
    SCOPE_SCAN,
    SCOPE_SYNC,
    ScanRunRepository,
    SignalEvaluationRepository,
    TrackedSignalRepository,
    WatchlistRepository,
)
from app.scanner.sessions import session_phase
from app.scanner.timeframes import (
    PRIMARY_TIMEFRAME,
    SCANNER_TIMEFRAMES,
    MultiTimeframeContext,
)
from app.signals.repository import SignalRepository
from app.signals.service import SignalService
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository

logger = get_logger(__name__)

SIGNAL_MODEL_VERSION = "signal-v1"
BASIS_POINTS = Decimal(10_000)


@dataclass
class ScanCycleStats:
    """What one cycle did. Returned, logged and persisted."""

    started_at: datetime
    completed_at: datetime | None = None

    symbols_total: int = 0
    symbols_synced: int = 0
    symbols_evaluated: int = 0
    symbols_skipped: int = 0
    symbols_failed: int = 0

    candidates_discovered: int = 0
    signals_qualified: int = 0
    signals_strong: int = 0

    paper_decisions: int = 0
    positions_opened: int = 0
    positions_closed: int = 0

    session_phase: SessionPhase = SessionPhase.CLOSED
    skipped_reason: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        end = self.completed_at or utc_now()
        return (end - self.started_at).total_seconds()

    @property
    def hit_rate(self) -> float:
        """Qualified fraction of what was evaluated.

        Reported alongside every scan because it is the base rate: if this is
        routinely high, the threshold is not selective and the "hits" are just
        the market moving. See :mod:`app.scanner.models`.
        """
        if self.symbols_evaluated == 0:
            return 0.0
        return self.signals_qualified / self.symbols_evaluated

    def metrics(self) -> dict[str, int]:
        return {
            "symbols_total": self.symbols_total,
            "symbols_synced": self.symbols_synced,
            "symbols_evaluated": self.symbols_evaluated,
            "symbols_skipped": self.symbols_skipped,
            "symbols_failed": self.symbols_failed,
            "candidates_discovered": self.candidates_discovered,
            "signals_qualified": self.signals_qualified,
            "signals_strong": self.signals_strong,
            "paper_decisions": self.paper_decisions,
            "positions_opened": self.positions_opened,
            "positions_closed": self.positions_closed,
        }

    def summary(self) -> str:
        return (
            f"{self.symbols_evaluated}/{self.symbols_total} evaluated, "
            f"{self.signals_qualified} qualified ({self.hit_rate:.0%} hit rate), "
            f"{self.signals_strong} strong, {self.symbols_failed} failed, "
            f"{self.duration_seconds:.1f}s"
        )


@dataclass(frozen=True, slots=True)
class SymbolOutcome:
    """One symbol's result within a cycle."""

    symbol: str
    evaluated: bool
    qualified: bool = False
    strong: bool = False
    error: str | None = None
    ranked: RankedCandidate | None = None
    notify_lifecycle: SignalLifecycle | None = None
    notify_payload: dict[str, Any] | None = None
    paper_decisions: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    paper_payload: dict[str, Any] | None = None
    portfolio_events: list[Event] = field(default_factory=list)
    """One event per portfolio that has a notification channel. Sent to that
    portfolio's own Discord destination."""


class ScannerService:
    """Runs scan cycles over the watchlist."""

    def __init__(
        self,
        session_factory: async_sessionmaker,  # type: ignore[type-arg]
        *,
        settings: Settings,
        provider: MarketDataProvider,
        notifications: NotificationService | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        """
        Args:
            session_factory: a factory, not a session -- each symbol gets its own
                transaction, which is what makes failure isolation possible.
            settings: scanner thresholds and cadence.
            provider: market data. Only consulted for sync and live quotes.
            notifications: where lifecycle events go. ``None`` disables delivery
                without disabling persistence.
            calendar: venue calendar. Defaults to NYSE.
        """
        self._factory = session_factory
        self._settings = settings
        self._provider = provider
        self._notifications = notifications
        self._calendar = calendar or get_trading_calendar(settings.market_data.default_exchange)

    # -- Public API --------------------------------------------------------

    async def sync_market_data(
        self, *, as_of: datetime | None = None, timeframes: Sequence[Timeframe] | None = None
    ) -> ScanCycleStats:
        """Incrementally synchronise the watchlist's market data.

        Separate from the scan so it can run on a different cadence (every five
        minutes rather than fifteen). One symbol's provider failure is recorded
        and the rest continue.
        """
        now = as_of or utc_now()
        stats = ScanCycleStats(started_at=now)
        wanted = tuple(timeframes) if timeframes else self._scanner_timeframes()

        # Recorded as a run so `ops status` can answer "when did data last
        # arrive?". Without it the sync is invisible and a silently failing
        # scheduler looks identical to a healthy one.
        async with session_scope(self._factory) as session:
            symbols = await WatchlistRepository(session).symbols()
            run = await ScanRunRepository(session).acquire_lease(
                scope=SCOPE_SYNC, lease_seconds=self._settings.scanner.lease_seconds, now=now
            )
            run_id = run.id if run is not None else None
        stats.symbols_total = len(symbols)

        if run_id is None:
            stats.skipped_reason = "another sync holds the lease"
            stats.completed_at = utc_now()
            logger.info("sync skipped; lease held elsewhere")
            return stats

        for symbol in symbols:
            try:
                async with session_scope(self._factory) as session:
                    service = MarketDataImportService(session, self._provider)
                    for timeframe in wanted:
                        await service.sync_symbol(symbol=symbol, timeframe=timeframe, now=now)
                stats.symbols_synced += 1
            except (ProviderError, ValueError) as exc:
                stats.symbols_failed += 1
                stats.failures.append((symbol, safe_message(exc)))
                logger.warning("market data sync failed", symbol=symbol, error=safe_message(exc))

        stats.completed_at = utc_now()
        async with session_scope(self._factory) as session:
            row = await session.get(ScanRun, run_id)
            if row is not None:
                await ScanRunRepository(session).complete(row, metrics=stats.metrics())

        logger.info(
            "market data sync complete",
            synced=stats.symbols_synced,
            failed=stats.symbols_failed,
        )
        return stats

    async def run_scan_cycle(
        self, *, as_of: datetime | None = None, with_paper_trading: bool = True
    ) -> ScanCycleStats:
        """One pass over the watchlist.

        Acquires a database lease first: a second scheduled invocation returns
        immediately rather than evaluating the same symbols concurrently.
        """
        now = as_of or utc_now()
        stats = ScanCycleStats(started_at=now)
        stats.session_phase = session_phase(self._calendar, now)

        async with session_scope(self._factory) as session:
            run = await ScanRunRepository(session).acquire_lease(
                scope=SCOPE_SCAN, lease_seconds=self._settings.scanner.lease_seconds, now=now
            )
            run_id = run.id if run is not None else None

        if run_id is None:
            stats.skipped_reason = "another scan holds the lease"
            stats.completed_at = utc_now()
            logger.info("scan skipped; lease held elsewhere")
            return stats

        try:
            await self._scan(stats=stats, now=now, run_id=run_id, paper=with_paper_trading)
            error = None
        except Exception as exc:
            error = safe_message(exc)
            logger.exception("scan cycle failed")
        finally:
            stats.completed_at = utc_now()
            async with session_scope(self._factory) as session:
                run_row = await session.get(ScanRun, run_id)
                if run_row is not None:
                    await ScanRunRepository(session).complete(
                        run_row, metrics=stats.metrics(), error=error
                    )

        logger.info("scan cycle complete", summary=stats.summary())
        return stats

    async def top_candidates(self, limit: int | None = None) -> list[RankedCandidate]:
        """Currently qualified candidates, ranked.

        Reads persisted state rather than rescanning: an overview is a view of
        what the last scan found, and rescanning would give a different answer
        for reasons unrelated to the question.
        """
        limit = limit or self._settings.scanner.top_candidates
        async with session_scope(self._factory) as session:
            evaluations = await SignalEvaluationRepository(session).latest_per_instrument(
                qualified_only=True
            )
            instruments = InstrumentRepository(session)
            candidates: list[RankedCandidate] = []
            for evaluation in evaluations:
                instrument = await instruments.get_by_id(evaluation.instrument_id)
                if instrument is None:  # pragma: no cover -- FK guarantees this
                    continue
                candidates.append(to_ranked(evaluation, instrument.symbol))
        return rank_candidates(candidates, limit=limit)

    # -- Cycle internals ---------------------------------------------------

    async def _scan(
        self, *, stats: ScanCycleStats, now: datetime, run_id: int, paper: bool
    ) -> None:
        async with session_scope(self._factory) as session:
            entries = await WatchlistRepository(session).list_entries()
            symbols = [instrument.symbol for _, instrument in entries]
        stats.symbols_total = len(symbols)

        limit = self._settings.scanner.max_symbols_per_cycle
        for symbol in symbols[:limit]:
            outcome = await self._scan_symbol(symbol=symbol, now=now, run_id=run_id, paper=paper)
            _apply_outcome(stats, outcome)
            # Notification happens after the symbol's transaction has committed.
            await self._notify(outcome)

        await self._expire_stale(now=now)

    async def _scan_symbol(
        self, *, symbol: str, now: datetime, run_id: int, paper: bool
    ) -> SymbolOutcome:
        """Evaluate one symbol in its own transaction.

        Every failure mode returns a :class:`SymbolOutcome` rather than raising:
        the cycle must survive a bad symbol, and "NVDA failed" is a statistic,
        not an emergency.
        """
        try:
            async with session_scope(self._factory) as session:
                return await self._evaluate(
                    session=session, symbol=symbol, now=now, run_id=run_id, paper=paper
                )
        except Exception as exc:
            message = safe_message(exc)
            logger.warning("symbol evaluation failed", symbol=symbol, error=message)
            return SymbolOutcome(symbol=symbol, evaluated=False, error=message)

    async def _evaluate(
        self, *, session: Any, symbol: str, now: datetime, run_id: int, paper: bool
    ) -> SymbolOutcome:
        instruments = InstrumentRepository(session)
        instrument = await instruments.get_by_symbol(symbol)
        if instrument is None:
            return SymbolOutcome(symbol=symbol, evaluated=False, error="instrument not found")

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
        analysis = await analyser.analyse(instrument=instrument, as_of=now)

        signals = SignalService(features, self._provider, self._settings)
        primary = analysis.context.get(PRIMARY_TIMEFRAME)
        if primary is None or primary.quality is DataQuality.MISSING:
            return SymbolOutcome(symbol=symbol, evaluated=False, error="no primary timeframe data")

        signal = None
        if primary.quality in (DataQuality.OK, DataQuality.STALE):
            signal = await signals.evaluate(
                symbol=symbol,
                timeframe=PRIMARY_TIMEFRAME,
                horizon=Horizon.D5,
                as_of=now,
                adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            )

        # Mark and exit open positions before considering anything new. Without
        # this the scanner opens positions that can never close: stops, targets
        # and holding limits all live in `process_bar`, and nothing else calls it.
        quote = await self._quote(symbol)
        closed = await self._settle_open_positions(
            session=session, instrument=instrument, analysis=analysis, quote=quote
        )

        return await self._persist(
            session=session,
            instrument=instrument,
            analysis=analysis,
            signal=signal,
            quote=quote,
            now=now,
            run_id=run_id,
            paper=paper,
            closed=closed,
        )

    async def _settle_open_positions(
        self,
        *,
        session: Any,
        instrument: Instrument,
        analysis: AnalysisResult,
        quote: Quote | None,
    ) -> list[tuple[str, Any]]:
        """Push the newest primary bar through every profile.

        Returns ``(routing_key, trade)`` for each position that closed, so the
        caller can notify the owning portfolio's channel.

        ``advance_clock=False`` matters and is not an optimisation. The bar
        counter is per *portfolio*, not per instrument, so advancing it once per
        symbol would age a 15-bar holding limit by fifty-two bars in a single
        scan and close everything on the next cycle. Time exits therefore rely on
        the calendar deadline, which counts trading days and is independent of
        how many symbols happen to be watched.
        """
        primary = analysis.context.primary
        if primary is None or primary.bar_timestamp is None or primary.close is None:
            return []

        price = Decimal(str(primary.close))
        structure = primary.structure_metrics
        high = Decimal(str(structure.resistance)) if structure and structure.resistance else price
        low = Decimal(str(structure.support)) if structure and structure.support else price

        service = PaperTradingService(
            repository=PaperTradingRepository(session),
            profiles=SimulationProfileRepository(session),
            signals=SignalRepository(session),
            decisions=TradeDecisionRepository(session),
        )
        outcomes = await service.process_bar(
            instrument_id=instrument.id,
            bar=BarPrices(
                timestamp=primary.bar_timestamp,
                open=price,
                high=max(high, price),
                low=min(low, price),
                close=price,
            ),
            quote=quote,
            advance_clock=False,
        )

        profiles = {
            profile.name: profile
            for profile in await SimulationProfileRepository(session).list_profiles(
                enabled_only=True
            )
        }
        closed: list[tuple[str, Any]] = []
        for outcome in outcomes:
            channel = getattr(profiles.get(outcome.profile_name), "notification_channel", None)
            for trade in outcome.closed_trades:
                closed.append((channel or "", trade))
        return closed

    async def _persist(
        self,
        *,
        session: Any,
        instrument: Instrument,
        analysis: AnalysisResult,
        signal: Any,
        quote: Quote | None,
        now: datetime,
        run_id: int,
        paper: bool,
        closed: list[tuple[str, Any]] | None = None,
    ) -> SymbolOutcome:
        """Write the evaluation and advance the lifecycle. Always persists."""
        context = analysis.context
        phase = session_phase(self._calendar, now)
        quality = context.quality

        score = float(signal.score) if signal is not None else 0.0
        confidence = float(signal.confidence) if signal is not None else 0.0
        classification = signal.classification.value if signal is not None else "NEUTRAL"

        actionable = quality.is_actionable and (
            phase.is_tradable or not self._settings.scanner.require_regular_session
        )
        qualified = actionable and score >= self._settings.notifications.signal_threshold

        tracked, transition = await self._advance_lifecycle(
            session=session,
            instrument=instrument,
            context=context,
            score=score,
            confidence=confidence,
            actionable=actionable,
            now=now,
        )

        evaluation = _build_evaluation(
            instrument=instrument,
            analysis=analysis,
            signal=signal,
            quote=quote,
            now=now,
            run_id=run_id,
            tracked_id=tracked.id if tracked else None,
            phase=phase,
            quality=quality,
            qualified=qualified,
            score=score,
            confidence=confidence,
            classification=classification,
        )
        await SignalEvaluationRepository(session).record(evaluation)

        paper_result = None
        profiles_by_name: dict[str, Any] = {}
        if paper and qualified and signal is not None and transition is not None:
            profiles_by_name = {
                profile.name: profile
                for profile in await SimulationProfileRepository(session).list_profiles(
                    enabled_only=True
                )
            }
            paper_result = await self._run_paper(
                session=session,
                instrument=instrument,
                signal=signal,
                quote=quote,
                analysis=analysis,
                now=now,
            )

        ranked = to_ranked(evaluation, instrument.symbol)
        payload = _notification_payload(
            symbol=instrument.symbol,
            signal=signal,
            quote=quote,
            evaluation=evaluation,
            instrument=instrument,
            context=context,
            lifecycle=transition.lifecycle if transition is not None else None,
            settings=self._settings,
            now=now,
        )

        return SymbolOutcome(
            symbol=instrument.symbol,
            evaluated=True,
            qualified=qualified,
            strong=transition.lifecycle is SignalLifecycle.STRONG if transition else False,
            ranked=ranked,
            notify_lifecycle=transition.lifecycle
            if transition is not None and transition.changed
            else None,
            notify_payload=payload,
            paper_decisions=len(paper_result.decisions) if paper_result else 0,
            positions_opened=paper_result.positions_opened if paper_result else 0,
            paper_payload=_paper_payload(paper_result, evaluation) if paper_result else None,
            portfolio_events=(
                _portfolio_events(paper_result, evaluation, profiles_by_name)
                if paper_result
                else []
            )
            + _close_events(closed or [], symbol=instrument.symbol),
            positions_closed=len(closed or []),
        )

    async def _run_paper(
        self,
        *,
        session: Any,
        instrument: Instrument,
        signal: Any,
        quote: Quote | None,
        analysis: AnalysisResult,
        now: datetime,
    ) -> Any:
        """Fan the signal out to every simulation profile.

        **Paper thresholds are independent of the Discord threshold.** Reaching
        here means the signal was worth announcing; whether any given portfolio
        trades it is decided by that profile's own risk and cost model, which is
        why a EUR 50 portfolio can decline what a EUR 5000 one takes. Collapsing
        the two thresholds into one would delete that distinction, which is the
        most informative output the simulation produces.

        Execution is at the *next* instant, never the signal bar: the engine
        raises :class:`LookAheadError` if asked to fill at or before it.
        """
        service = PaperTradingService(
            repository=PaperTradingRepository(session),
            profiles=SimulationProfileRepository(session),
            signals=SignalRepository(session),
            decisions=TradeDecisionRepository(session),
        )
        primary = analysis.context.primary
        price = Decimal(str(primary.close)) if primary and primary.close else signal.reference_price
        atr_pct = primary.atr_pct if primary else None
        atr = (
            (price * Decimal(str(atr_pct)) / Decimal(100)).quantize(Decimal("0.000001"))
            if atr_pct
            else None
        )

        # One second after the signal bar: the earliest instant that is provably
        # after it, which is all the no-look-ahead guard requires.
        execution_at = max(now, signal.timestamp + timedelta(seconds=1))
        return await service.run_signal(
            signal=signal,
            instrument=instrument,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            execution_timestamp=execution_at,
            execution_price=price,
            quote=quote,
            atr=atr,
            now=execution_at,
        )

    async def _advance_lifecycle(
        self,
        *,
        session: Any,
        instrument: Instrument,
        context: MultiTimeframeContext,
        score: float,
        confidence: float,
        actionable: bool,
        now: datetime,
    ) -> tuple[Any, Any]:
        """Find or create the tracked signal and apply the transition.

        A direction of zero is not tracked: there is no setup to have an opinion
        about, and creating a signal for "no view" would fill the lifecycle table
        with rows that can never qualify.
        """
        direction = context.direction
        if direction == 0:
            return None, None

        primary = context.primary
        identity = SignalIdentity(
            instrument_id=instrument.id,
            direction=direction_label(direction),
            primary_timeframe=PRIMARY_TIMEFRAME.value,
            horizon=Horizon.D5.value,
            setup=setup_for(primary.structure) if primary else "UNKNOWN",
        )

        repository = TrackedSignalRepository(session)
        existing = await repository.find_active(identity)
        transition = evaluate_lifecycle(
            current=SignalLifecycle(existing.lifecycle) if existing else None,
            score=score,
            settings=self._settings.notifications,
            actionable=actionable,
        )

        if existing is None:
            tracked = await repository.create(
                identity=identity,
                lifecycle=transition.lifecycle,
                score=score,
                confidence=confidence,
                now=now,
            )
        else:
            tracked = await repository.apply_transition(
                existing,
                lifecycle=transition.lifecycle,
                score=score,
                confidence=confidence,
                now=now,
            )
        return tracked, transition

    async def _expire_stale(self, *, now: datetime) -> None:
        cutoff = now - timedelta(hours=self._settings.scanner.signal_expiry_hours)
        async with session_scope(self._factory) as session:
            expired = await TrackedSignalRepository(session).expire_stale(older_than=cutoff)
        if expired:
            logger.info("expired stale signals", count=expired)

    async def _notify(self, outcome: SymbolOutcome) -> None:
        """Emit lifecycle and portfolio events. **After** the commit, never fatal."""
        if self._notifications is None:
            return
        if outcome.notify_lifecycle is None or outcome.notify_payload is None:
            for event in outcome.portfolio_events:
                await self._notifications.publish(event)
            return

        # Qualified, strong and invalidated all route through notify_signal: the
        # notification policy owns which transition produces which event and
        # whether it is a duplicate. Deciding that here would put the same rule
        # in two places, and they would drift.
        announceable = {
            SignalLifecycle.QUALIFIED,
            SignalLifecycle.STRONG,
            SignalLifecycle.INVALIDATED,
        }
        if outcome.notify_lifecycle in announceable:
            await self._notifications.notify_signal(
                symbol=outcome.symbol,
                timeframe=PRIMARY_TIMEFRAME.value,
                horizon=Horizon.D5.value,
                score=float(outcome.notify_payload.get("score", 0.0)),
                payload=outcome.notify_payload,
            )

        # Portfolio channels. One message per portfolio that has a destination,
        # carrying only that portfolio's own numbers -- #paper-100 must not show
        # what paper-10000 did.
        for event in outcome.portfolio_events:
            await self._notifications.publish(event)

    async def _quote(self, symbol: str) -> Quote | None:
        """A live quote, or None. A missing quote never fails an evaluation."""
        try:
            return await self._provider.get_latest_quote(symbol)
        except ProviderError as exc:
            logger.debug("no quote available", symbol=symbol, error=safe_message(exc))
            return None

    def _scanner_timeframes(self) -> tuple[Timeframe, ...]:
        return SCANNER_TIMEFRAMES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _apply_outcome(stats: ScanCycleStats, outcome: SymbolOutcome) -> None:
    if not outcome.evaluated:
        stats.symbols_failed += 1
        if outcome.error:
            stats.failures.append((outcome.symbol, outcome.error))
        return
    stats.symbols_evaluated += 1
    stats.candidates_discovered += 1
    stats.paper_decisions += outcome.paper_decisions
    stats.positions_opened += outcome.positions_opened
    stats.positions_closed += outcome.positions_closed
    if outcome.qualified:
        stats.signals_qualified += 1
    if outcome.strong:
        stats.signals_strong += 1


def to_ranked(evaluation: SignalEvaluation, symbol: str) -> RankedCandidate:
    """Rank one stored evaluation. Public: the API ranks the same way the scanner does."""
    value, contributions = rank_score(
        score=evaluation.score,
        confidence=evaluation.confidence,
        agreement=evaluation.agreement or 0.0,
        net_edge_bps=evaluation.net_edge_bps,
        spread_bps=evaluation.spread_bps,
        relative_volume=_relative_volume(evaluation),
    )
    return RankedCandidate(
        symbol=symbol,
        evaluation_id=evaluation.id,
        tracked_signal_id=evaluation.tracked_signal_id,
        score=evaluation.score,
        confidence=evaluation.confidence,
        agreement=evaluation.agreement or 0.0,
        net_edge_bps=evaluation.net_edge_bps,
        spread_bps=evaluation.spread_bps,
        relative_volume=_relative_volume(evaluation),
        rank_score=value,
        contributions=contributions,
        direction=evaluation.direction,
        horizon=evaluation.expected_horizon or "",
    )


def _relative_volume(evaluation: SignalEvaluation) -> float | None:
    volume = evaluation.volume_metrics or {}
    value = volume.get("relative_volume")
    return float(value) if isinstance(value, int | float) else None


def _build_evaluation(
    *,
    instrument: Instrument,
    analysis: AnalysisResult,
    signal: Any,
    quote: Quote | None,
    now: datetime,
    run_id: int | None,
    tracked_id: int | None,
    phase: SessionPhase,
    quality: DataQuality,
    qualified: bool,
    score: float,
    confidence: float,
    classification: str,
) -> SignalEvaluation:
    """Assemble the X record.

    **Nothing future-derived goes in here.** Every value is knowable at ``now``:
    the timeframe states, the metrics, the quote, the verdict. Outcome labels are
    phase 5's, in their own table.

    ``run_id`` is nullable because a historical replay produces observations that
    belong to no scan run -- it marks them with ``backtest_run_id`` instead. The
    function is shared with :mod:`app.backtesting.engine` on purpose: one
    assembler means a backtested row and a live row cannot describe the same
    market state differently.
    """
    context = analysis.context
    primary = context.primary
    entry = context.entry

    return SignalEvaluation(
        instrument_id=instrument.id,
        tracked_signal_id=tracked_id,
        primary_signal_id=None,
        scan_run_id=run_id,
        evaluated_at=now,
        market_data_timestamp=analysis.newest_bar,
        score=score,
        confidence=confidence,
        classification=classification,
        direction=context.direction,
        qualified=qualified,
        agreement=context.agreement,
        aligned=context.aligned,
        expected_move_bps=float(signal.net_edge.expected_move_bps) if signal else None,
        cost_bps=float(signal.net_edge.cost_bps) if signal else None,
        net_edge_bps=float(signal.net_edge.net_edge_bps) if signal else None,
        expected_horizon=signal.horizon.value if signal else None,
        bid=float(quote.bid) if quote else None,
        ask=float(quote.ask) if quote else None,
        spread_bps=float(quote.spread_bps) if quote else None,
        quote_age_seconds=quote_age_seconds(quote, now=now) if quote else None,
        timeframe_states=context.as_dict()["timeframes"],
        trend_metrics={
            "direction": context.direction,
            "agreement": context.agreement,
            "aligned": context.aligned,
            "ema_spread_pct": primary.ema_spread_pct if primary else None,
        },
        momentum_metrics={"rsi": primary.rsi if primary else None},
        volume_metrics={
            "relative_volume": entry.relative_volume if entry else None,
            "volume_confirmed": context.volume_confirmed,
        },
        volatility_metrics={
            "atr_pct": primary.atr_pct if primary else None,
            "volatility": primary.volatility if primary else None,
        },
        structure_metrics=(
            primary.structure_metrics.as_dict() if primary and primary.structure_metrics else {}
        ),
        liquidity_metrics={
            "spread_bps": float(quote.spread_bps) if quote else None,
            "quote_age_seconds": quote_age_seconds(quote, now=now) if quote else None,
        },
        reason_codes=[r.code for r in getattr(signal, "reasons", ()) or ()] if signal else [],
        risk_codes=[r.code for r in getattr(signal, "risks", ()) or ()] if signal else [],
        data_quality=quality.value,
        session_phase=phase.value,
        feature_set_version=FEATURE_SET_VERSION,
        signal_model_version=SIGNAL_MODEL_VERSION,
        scanner_policy_version=SCANNER_POLICY_VERSION,
    )


def _paper_payload(result: Any, evaluation: SignalEvaluation) -> dict[str, Any]:
    """The grouped multi-profile decision message.

    **One event for all nine profiles**, not nine events. The interesting
    information is the *pattern* -- small portfolios declining what large ones
    take, because a fixed fee is a much larger fraction of a EUR 50 round trip --
    and nine separate messages would bury it. All nine decisions are still
    persisted individually by the paper service; only the notification is grouped.
    """
    return {
        "symbol": result.symbol,
        "score": evaluation.score,
        "decisions": [
            {
                "profile": decision.profile_name,
                "decision": "TRADE" if decision.is_trade else "SKIP",
                "reason": None if decision.is_trade else decision.reason.value,
            }
            for decision in result.decisions
        ],
        "positions_opened": result.positions_opened,
        "entries_rejected": result.entries_rejected,
    }


def _portfolio_events(
    result: Any, evaluation: SignalEvaluation, profiles: dict[str, Any]
) -> list[Event]:
    """One entry event per portfolio that has a notification channel.

    Routed by the profile's stored ``notification_channel``, not by its capital
    or its name -- so a portfolio added later routes correctly without any change
    here. A profile with no channel produces no message; the nine generic phase-3
    profiles are unaffected.

    Only *accepted* entries produce a message. A portfolio that declined the
    trade is visible in the grouped decision table and in the database; a message
    per declining portfolio would fill three channels with non-events.
    """
    events: list[Event] = []
    for profile_name, entry in (result.entries or {}).items():
        profile = profiles.get(profile_name)
        channel = getattr(profile, "notification_channel", None)
        if profile is None or channel is None or not entry.accepted:
            continue
        events.append(
            Event.paper_trade_opened(
                symbol=result.symbol,
                routing_key=channel,
                payload={
                    "symbol": result.symbol,
                    "portfolio": profile_name,
                    "score": evaluation.score,
                    "equity": float(profile.initial_capital),
                    "decisions": [{"profile": profile_name, "decision": "TRADE"}],
                    "positions_opened": 1,
                },
            )
        )
    return events


def _close_events(closed: list[tuple[str, Any]], *, symbol: str) -> list[Event]:
    """A CLOSE event per closed position, routed to its own portfolio channel.

    Routing comes from the profile's stored ``notification_channel`` -- never
    from the capital amount and never from the rendered text. A portfolio with
    no channel produces no message, which is how the nine generic profiles stay
    silent.

    Gross, costs and net are all carried. A message reporting only net would hide
    the cost model's output, and one reporting only gross would flatter every
    result.
    """
    events: list[Event] = []
    for channel, trade in closed:
        if not channel:
            continue
        events.append(
            Event.paper_trade_closed(
                symbol=symbol,
                routing_key=channel,
                payload={
                    "symbol": symbol,
                    "portfolio": channel,
                    "entry_price": float(trade.entry_price),
                    "exit_price": float(trade.exit_price),
                    "quantity": float(trade.quantity),
                    "holding": f"{trade.holding_bars} bars",
                    "exit_reason": trade.exit_reason.value
                    if hasattr(trade.exit_reason, "value")
                    else str(trade.exit_reason),
                    "gross_pnl": float(trade.gross_pnl),
                    "fees": float(trade.total_fees),
                    "spread_cost": float(trade.total_spread_cost),
                    "slippage_cost": float(trade.total_slippage_cost),
                    "net_pnl": float(trade.net_pnl),
                    "net_return": float(trade.net_return),
                },
            )
        )
    return events


def _notification_payload(
    *,
    symbol: str,
    signal: Any,
    quote: Quote | None,
    evaluation: SignalEvaluation,
    instrument: Instrument | None = None,
    context: MultiTimeframeContext | None = None,
    lifecycle: SignalLifecycle | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a Discord message renders from.

    **Only values that exist.** A null here produces a shorter message, never a
    fabricated metric -- and there is deliberately no key for support,
    resistance, a price target, an entry zone or an expected price, because the
    scanner computes none of them.

    Everything is read from the evaluation that was just persisted or the context
    that produced it, so the message and the stored row cannot disagree.
    """
    primary = context.primary if context is not None else None
    entry = context.entry if context is not None else None

    payload: dict[str, Any] = {
        "symbol": symbol,
        "score": evaluation.score,
        "confidence": evaluation.confidence,
        "classification": evaluation.classification,
        "direction": evaluation.direction,
        "timeframe": PRIMARY_TIMEFRAME.value,
        "components": {
            tf: state.get("trend")
            for tf, state in (evaluation.timeframe_states or {}).items()
            if isinstance(state, dict)
        },
    }

    if instrument is not None and instrument.name and instrument.name != symbol:
        # Populated only since the identity refresh; before that `name` was the
        # ticker, and repeating the symbol as its own company name is noise.
        payload["company_name"] = instrument.name
    if lifecycle is not None:
        payload["lifecycle_state"] = lifecycle.value

    if context is not None:
        payload.update(_horizon_states(context))

    payload.update(_component_fields(primary, entry))

    if signal is not None:
        payload["horizon"] = signal.horizon.value
        payload["net_edge_bps"] = float(signal.net_edge.net_edge_bps)
        payload["reasons"] = [r.message for r in (signal.reasons or ())][:5]
        payload["risks"] = [r.message for r in (signal.risks or ())][:4]
    if quote is not None:
        payload["bid"] = float(quote.bid)
        payload["ask"] = float(quote.ask)
        payload["spread_bps"] = float(quote.spread_bps)
        payload["liquidity"] = _spread_label(float(quote.spread_bps))

    if evaluation.market_data_timestamp is not None:
        payload["market_data_timestamp"] = evaluation.market_data_timestamp.strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        if now is not None:
            age = (now - evaluation.market_data_timestamp).total_seconds() / 60.0
            payload["freshness"] = f"{age:.0f} min old ({evaluation.data_quality})"
    if settings is not None:
        payload["provider"] = settings.market_data_provider
        payload["feed"] = settings.alpaca.feed

    return payload


def _component_fields(primary: Any, entry: Any) -> dict[str, Any]:
    """Trend, price, momentum, structure, volatility and volume, where present.

    Split out so the payload builder stays readable; each value is still omitted
    individually rather than defaulted.
    """
    fields: dict[str, Any] = {}
    if primary is not None:
        fields["trend"] = primary.trend.value
        if primary.close is not None:
            fields["price"] = float(primary.close)
        if primary.rsi is not None:
            fields["momentum"] = _rsi_label(primary.rsi)
        if primary.structure is not None:
            fields["structure"] = primary.structure.value
        if primary.volatility is not None:
            fields["volatility"] = _volatility_label(primary.volatility)
    if entry is not None and entry.relative_volume is not None:
        fields["volume"] = _volume_label(entry.relative_volume)
    return fields


def _horizon_states(context: MultiTimeframeContext) -> dict[str, str]:
    """The four trading horizons as payload keys.

    LONG_TERM is included precisely because it reads NOT_AVAILABLE: omitting it
    would let a reader assume the horizon was merely neutral.
    """
    assessed = classify_horizons(context)
    return {
        "intraday": assessed[TradingHorizon.INTRADAY].state.value,
        "short_term": assessed[TradingHorizon.SHORT_TERM].state.value,
        "medium_term": assessed[TradingHorizon.MEDIUM_TERM].state.value,
        "long_term": assessed[TradingHorizon.LONG_TERM].state.value,
    }


def _rsi_label(rsi: float) -> str:
    """RSI as words. The number itself is already in the plaintext body."""
    if rsi >= 70:  # noqa: PLR2004 -- conventional RSI bands
        return "overbought"
    if rsi >= 55:  # noqa: PLR2004
        return "positive"
    if rsi <= 30:  # noqa: PLR2004
        return "oversold"
    if rsi <= 45:  # noqa: PLR2004
        return "weak"
    return "neutral"


def _volume_label(relative: float) -> str:
    if relative >= 2:  # noqa: PLR2004
        return f"surging ({relative:.1f}x)"
    if relative >= 1:
        return f"confirmed ({relative:.1f}x)"
    return f"thin ({relative:.1f}x)"


def _volatility_label(volatility: float) -> str:
    """``volatility_20`` is annualised and fractional: 0.22 is 22% a year."""
    percent = volatility * 100
    if percent >= 60:  # noqa: PLR2004
        return f"very high ({percent:.0f}%)"
    if percent >= 35:  # noqa: PLR2004
        return f"elevated ({percent:.0f}%)"
    if percent >= 20:  # noqa: PLR2004
        return f"normal ({percent:.0f}%)"
    return f"low ({percent:.0f}%)"


def _spread_label(spread_bps: float) -> str:
    """Liquidity from the observed spread, with the phase-4 caveat built in."""
    if spread_bps >= 100:  # noqa: PLR2004
        return f"very wide ({spread_bps:.0f} bps -- likely a thin book)"
    if spread_bps >= 20:  # noqa: PLR2004
        return f"wide ({spread_bps:.0f} bps)"
    return f"normal ({spread_bps:.1f} bps)"
