"""Counterfactual evaluation of trade decisions.

Answers the question a system that only records its trades can never ask:

    **Would the trades we declined have worked?**

A cost gate that rejects everything looks identical, in a P&L report, to a cost
gate that is correctly protecting the portfolio. The difference is only visible by
measuring what the rejected trades *would* have done -- and if the skipped set
would have been profitable net of the costs that caused the skip, the gate is
wrong.

Scope
-----
Deliberately small. This computes a forward price return and the excursions
around it, for both TRADE and SKIP decisions. It is **not** an analytics engine:
there is no aggregation, no hit-rate, no attribution. Those belong in phase 5,
once there is enough data for them to mean anything.

Two properties are load-bearing:

1. **It never touches portfolio state.** Counterfactuals are observations about
   decisions, not events in a portfolio. A bug here can produce a wrong number in
   a report; it cannot corrupt a balance.
2. **The returns are GROSS.** No costs are applied, because the counterfactual
   position was never sized -- and costs depend on size. Comparing a gross
   counterfactual return against a net realised return is an error, and the field
   name says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Candle, DecisionOutcome, TradeDecisionRow
from app.domain.enums import Timeframe

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ForwardWindow:
    """Price behaviour over a horizon following a decision."""

    bars: int
    horizon_end: datetime
    reference_price: Decimal
    horizon_close: Decimal
    forward_return: float
    max_favorable_excursion: float
    """Best fractional gain reachable during the window -- reveals targets set
    too far away to ever be hit."""
    max_adverse_excursion: float
    """Worst fractional loss during the window -- reveals stops set so tight that
    an eventually-profitable trade would have been stopped out first."""


def measure_forward_window(
    *, reference_price: Decimal, candles: Sequence[Candle]
) -> ForwardWindow | None:
    """Measure what happened over the bars following a decision.

    Args:
        reference_price: the price at decision time.
        candles: bars **after** the decision, ascending. Bars at or before the
            decision must already be excluded by the caller's query -- including
            them would make the counterfactual look ahead exactly as a bad
            backtest does.

    Returns:
        A :class:`ForwardWindow`, or ``None`` if there are no bars yet. Absent
        data is not a zero return.
    """
    if not candles or reference_price <= 0:
        return None

    highs = max(c.high for c in candles)
    lows = min(c.low for c in candles)
    last = candles[-1]

    return ForwardWindow(
        bars=len(candles),
        horizon_end=last.timestamp,
        reference_price=reference_price,
        horizon_close=last.close,
        forward_return=float((last.close - reference_price) / reference_price),
        max_favorable_excursion=float((highs - reference_price) / reference_price),
        max_adverse_excursion=float((lows - reference_price) / reference_price),
    )


class CounterfactualService:
    """Evaluates decisions against what the market subsequently did."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate_decision(
        self,
        *,
        decision: TradeDecisionRow,
        timeframe: Timeframe,
        horizon_bars: int,
        now: datetime,
    ) -> DecisionOutcome | None:
        """Measure and store the forward window for one decision.

        Idempotent on ``trade_decision_id``: re-running refreshes the row rather
        than accumulating duplicates.

        Returns ``None`` when not enough forward data exists yet -- a decision
        made yesterday cannot have a 20-bar outcome, and inventing one would be
        worse than waiting.
        """
        candles = await self._forward_candles(
            instrument_id=decision.instrument_id,
            after=decision.decided_at,
            timeframe=timeframe,
            limit=horizon_bars,
        )
        window = measure_forward_window(reference_price=decision.reference_price, candles=candles)
        if window is None or window.bars < horizon_bars:
            return None

        stmt = select(DecisionOutcome).where(DecisionOutcome.trade_decision_id == decision.id)
        outcome = (await self._session.execute(stmt)).scalar_one_or_none()
        if outcome is None:
            outcome = DecisionOutcome(
                trade_decision_id=decision.id, instrument_id=decision.instrument_id
            )
            self._session.add(outcome)

        outcome.evaluated_at = now
        outcome.horizon_end = window.horizon_end
        outcome.bars_evaluated = window.bars
        outcome.reference_price = window.reference_price
        outcome.horizon_close = window.horizon_close
        outcome.forward_return = window.forward_return
        outcome.max_favorable_excursion = window.max_favorable_excursion
        outcome.max_adverse_excursion = window.max_adverse_excursion
        await self._session.flush()
        return outcome

    async def _forward_candles(
        self, *, instrument_id: int, after: datetime, timeframe: Timeframe, limit: int
    ) -> Sequence[Candle]:
        """Bars strictly after ``after``.

        ``>`` not ``>=``: the decision bar itself is not part of the forward
        window. Including it would measure a return the decision maker already
        knew about.
        """
        stmt = (
            select(Candle)
            .where(
                Candle.instrument_id == instrument_id,
                Candle.timeframe == timeframe,
                Candle.timestamp > after,
            )
            .order_by(Candle.timestamp)
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def outcomes_for_decisions(
        self, decision_ids: Sequence[int]
    ) -> Sequence[DecisionOutcome]:
        if not decision_ids:
            return []
        stmt = select(DecisionOutcome).where(
            DecisionOutcome.trade_decision_id.in_(list(decision_ids))
        )
        return (await self._session.execute(stmt)).scalars().all()
