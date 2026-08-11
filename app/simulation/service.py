"""Multi-profile signal evaluation.

Takes one signal, persists it, and records what every enabled simulation profile
decided about it. This is the fan-out the design brief describes::

    Signal #123
      +--> 50 EUR conservative   -> SKIP (fee dominates)
      +--> 500 EUR balanced      -> SKIP (net edge negative at this size)
      +--> 5000 EUR balanced     -> TRADE

**Not** the paper-trading engine. Nothing here opens a position, tracks equity or
computes P&L -- that is phase 3. What this does is make the decision *and its
reasoning* durable, so phase 3 has something to act on and the feedback system
has something to measure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.logging import get_logger
from app.domain.enums import PriceSeriesAdjustment, TradeDecisionType
from app.domain.quotes import Quote
from app.signals.models import SignalResult
from app.signals.repository import SignalRepository
from app.simulation.decisions import TradeDecision, evaluate_decision
from app.simulation.models import SimulationProfileConfig
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """One signal's verdicts across every profile."""

    signal_id: int
    symbol: str
    decisions: tuple[TradeDecision, ...]

    @property
    def trades(self) -> tuple[TradeDecision, ...]:
        return tuple(d for d in self.decisions if d.decision is TradeDecisionType.TRADE)

    @property
    def skips(self) -> tuple[TradeDecision, ...]:
        return tuple(d for d in self.decisions if d.decision is TradeDecisionType.SKIP)

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def is_unanimous(self) -> bool:
        """True if every profile reached the same verdict.

        Disagreement is the interesting case and the reason the system exists: it
        means the signal sits near an economic boundary where capital size or risk
        appetite decides the outcome.
        """
        if not self.decisions:
            return True
        first = self.decisions[0].decision
        return all(d.decision is first for d in self.decisions)


class SimulationEvaluationService:
    """Evaluates signals against every enabled simulation profile."""

    def __init__(
        self,
        signals: SignalRepository,
        profiles: SimulationProfileRepository,
        decisions: TradeDecisionRepository,
    ) -> None:
        self._signals = signals
        self._profiles = profiles
        self._decisions = decisions

    async def evaluate_signal(
        self,
        *,
        result: SignalResult,
        instrument_id: int,
        adjustment: PriceSeriesAdjustment,
        quote: Quote | None = None,
        available_capital: dict[str, Decimal] | None = None,
        now: datetime | None = None,
    ) -> EvaluationResult:
        """Persist ``result`` and record every profile's verdict on it.

        Args:
            result: the signal, evaluated once and shared by every profile.
            instrument_id: the instrument the signal belongs to.
            adjustment: which price series produced the features. Stored on the
                signal, because the same bar yields different features on raw and
                adjusted prices.
            quote: live top-of-book, if available.
            available_capital: per-profile free capital, keyed by profile name.
                Defaults to each profile's initial capital.
            now: decision timestamp, injectable for deterministic tests.

        Returns:
            The stored signal id and every decision, in profile-name order.
        """
        signal_id = await self._signals.record(
            result=result, instrument_id=instrument_id, adjustment=adjustment
        )
        profiles = await self._profiles.list_profiles(enabled_only=True)
        capital_by_name = available_capital or {}

        decisions: list[TradeDecision] = []
        for profile in profiles:
            decision = evaluate_decision(
                signal=result,
                profile=profile,
                quote=quote,
                available_capital=capital_by_name.get(profile.name),
                now=now,
            )
            await self._record(decision, profile, signal_id, instrument_id)
            decisions.append(decision)

        traded = sum(1 for d in decisions if d.decision is TradeDecisionType.TRADE)
        logger.info(
            "signal evaluated across profiles",
            symbol=result.symbol,
            signal_id=signal_id,
            profiles=len(profiles),
            trades=traded,
            skips=len(profiles) - traded,
        )
        return EvaluationResult(
            signal_id=signal_id, symbol=result.symbol, decisions=tuple(decisions)
        )

    async def _record(
        self,
        decision: TradeDecision,
        profile: SimulationProfileConfig,
        signal_id: int,
        instrument_id: int,
    ) -> None:
        if profile.id is None:
            msg = f"profile {profile.name!r} has no id; it must be persisted before evaluation"
            raise ValueError(msg)
        await self._decisions.record(
            decision=decision,
            signal_id=signal_id,
            simulation_profile_id=profile.id,
            instrument_id=instrument_id,
        )
