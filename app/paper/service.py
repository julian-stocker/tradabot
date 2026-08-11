"""Multi-profile paper-trading orchestration.

Joins the phase 2 decision fan-out to the phase 3 execution engine::

    signal ──> decisions (one per profile)  [phase 2]
                  │
                  ├─ SKIP  ──> recorded, and later evaluated counterfactually
                  └─ TRADE ──> sized order ──> fill ──> position   [phase 3]

**Portfolios are isolated.** Each profile gets its own engine, its own portfolio
row and its own positions. Nothing in one portfolio can move a number in another:
they share only the signal, which is immutable. That isolation is asserted
directly in ``tests/integration/test_paper_isolation.py`` rather than assumed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.db.models import Instrument
from app.domain.enums import CandleAmbiguityPolicy, ExitReason, PriceSeriesAdjustment
from app.domain.quotes import Quote
from app.paper.engine import BarOutcome, EntryOutcome, PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.signals.models import SignalResult
from app.signals.repository import SignalRepository
from app.simulation.decisions import TradeDecision
from app.simulation.models import SimulationProfileConfig
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository
from app.simulation.service import SimulationEvaluationService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SignalRunResult:
    """What one signal did across every profile."""

    signal_id: int
    symbol: str
    decisions: tuple[TradeDecision, ...]
    entries: dict[str, EntryOutcome]
    """Entry outcome per profile name, for decisions that said TRADE."""

    @property
    def positions_opened(self) -> int:
        return sum(1 for e in self.entries.values() if e.accepted)

    @property
    def entries_rejected(self) -> int:
        return sum(1 for e in self.entries.values() if not e.accepted)


class PaperTradingService:
    """Runs signals and candles through every enabled simulation profile."""

    def __init__(
        self,
        *,
        repository: PaperTradingRepository,
        profiles: SimulationProfileRepository,
        signals: SignalRepository,
        decisions: TradeDecisionRepository,
        ambiguity_policy: CandleAmbiguityPolicy = CandleAmbiguityPolicy.CONSERVATIVE,
    ) -> None:
        self._repository = repository
        self._profiles = profiles
        self._signals = signals
        self._decisions = decisions
        self._policy = ambiguity_policy
        self._evaluation = SimulationEvaluationService(signals, profiles, decisions)

    async def engine_for(self, profile: SimulationProfileConfig) -> PaperTradingEngine:
        """Build an engine bound to one profile's portfolio, creating it if new."""
        portfolio = await self._repository.ensure_portfolio(profile)
        return PaperTradingEngine(
            self._repository, profile, portfolio, ambiguity_policy=self._policy
        )

    async def run_signal(
        self,
        *,
        signal: SignalResult,
        instrument: Instrument,
        adjustment: PriceSeriesAdjustment,
        execution_timestamp: datetime,
        execution_price: Decimal,
        quote: Quote | None = None,
        atr: Decimal | None = None,
        now: datetime | None = None,
    ) -> SignalRunResult:
        """Evaluate one signal against every profile and act on the acceptances.

        Args:
            signal: the signal, shared unchanged by every profile.
            instrument: what it refers to.
            adjustment: which price series produced the features.
            execution_timestamp: when orders are placed. Must be strictly after
                the signal's bar -- the engine enforces it.
            execution_price: reference mid at execution.
            quote: live top-of-book at execution.
            atr: ATR at signal time, for stop placement.
            now: decision timestamp, injectable for determinism.
        """
        evaluation = await self._evaluation.evaluate_signal(
            result=signal,
            instrument_id=instrument.id,
            adjustment=adjustment,
            quote=quote,
            now=now,
        )

        entries: dict[str, EntryOutcome] = {}
        by_name = {p.name: p for p in await self._profiles.list_profiles(enabled_only=True)}

        for decision in evaluation.decisions:
            if not decision.is_trade:
                continue
            profile = by_name.get(decision.profile_name)
            if profile is None:
                continue

            decision_id = await self._decision_id(evaluation.signal_id, profile)
            engine = await self.engine_for(profile)
            entries[profile.name] = await engine.open_from_decision(
                instrument=instrument,
                trade_decision_id=decision_id,
                signal_id=evaluation.signal_id,
                signal_bar_timestamp=signal.timestamp,
                execution_timestamp=execution_timestamp,
                execution_price=execution_price,
                quote=quote,
                atr=atr,
            )

        opened = sum(1 for e in entries.values() if e.accepted)
        logger.info(
            "signal run complete",
            symbol=signal.symbol,
            signal_id=evaluation.signal_id,
            trade_decisions=len(entries),
            positions_opened=opened,
        )
        return SignalRunResult(
            signal_id=evaluation.signal_id,
            symbol=signal.symbol,
            decisions=evaluation.decisions,
            entries=entries,
        )

    async def process_bar(
        self,
        *,
        instrument_id: int,
        bar: BarPrices,
        quote: Quote | None = None,
        advance_clock: bool = True,
    ) -> list[BarOutcome]:
        """Push one candle through every enabled profile.

        Each profile is processed independently. Idempotent per (position, bar).
        """
        outcomes: list[BarOutcome] = []
        for profile in await self._profiles.list_profiles(enabled_only=True):
            engine = await self.engine_for(profile)
            outcomes.append(
                await engine.process_bar(
                    instrument_id=instrument_id,
                    bar=bar,
                    quote=quote,
                    advance_clock=advance_clock,
                )
            )
        return outcomes

    async def close_all(
        self,
        *,
        timestamp: datetime,
        marks: dict[int, Decimal],
        reason: ExitReason = ExitReason.SIMULATION_END,
    ) -> int:
        """Close every open position across every profile.

        Used at the end of a simulation run so no position is left dangling and
        unrealised P&L becomes realised -- otherwise final performance depends on
        an arbitrary mark.
        """
        closed = 0
        for profile in await self._profiles.list_profiles(enabled_only=True):
            engine = await self.engine_for(profile)
            for position in await self._repository.open_positions(_require_id(profile)):
                mark = marks.get(position.instrument_id) or position.current_mark_price
                if mark is None:
                    continue
                await engine.close_position(
                    position, exit_price=mark, timestamp=timestamp, reason=reason
                )
                closed += 1
        return closed

    async def close_on_signal_reversal(
        self,
        *,
        instrument_id: int,
        timestamp: datetime,
        mark_price: Decimal,
        quote: Quote | None = None,
    ) -> int:
        """Close positions in one instrument because the signal flipped.

        A distinct exit reason from a stop: "the thesis changed" and "the trade
        went against me" are different events, and a strategy that mostly exits
        one way behaves nothing like one that mostly exits the other.
        """
        closed = 0
        for profile in await self._profiles.list_profiles(enabled_only=True):
            engine = await self.engine_for(profile)
            positions = await self._repository.open_positions(
                _require_id(profile), instrument_id=instrument_id
            )
            for position in positions:
                await engine.close_position(
                    position,
                    exit_price=mark_price,
                    timestamp=timestamp,
                    reason=ExitReason.SIGNAL_REVERSAL,
                    quote=quote,
                )
                closed += 1
        return closed

    async def _decision_id(self, signal_id: int, profile: SimulationProfileConfig) -> int:
        """Find the persisted decision row id for a (signal, profile) pair."""
        rows = await self._decisions.list_for_signal(signal_id)
        for row in rows:
            if row.simulation_profile_id == profile.id:
                return row.id
        msg = (
            f"no persisted decision for signal {signal_id} and profile "
            f"{profile.name!r}; the evaluation should have created one"
        )
        raise ValueError(msg)


def profile_names(profiles: Sequence[SimulationProfileConfig]) -> tuple[str, ...]:
    return tuple(p.name for p in profiles)


def _require_id(profile: SimulationProfileConfig) -> int:
    if profile.id is None:
        msg = f"profile {profile.name!r} must be persisted before trading"
        raise ValueError(msg)
    return profile.id
