"""Persistence for the forward paper experiment. **The database is the boundary.**

Idempotency is enforced by the unique constraints migration 0014 declared, not by
application checks. Every write here is "get or create": it reads first, and if a
row already exists it is returned untouched. A restart that re-evaluates a
completed session, re-fans a candidate, or retries a submission therefore reaches
an existing row instead of creating a second one — and if two processes race, the
constraint refuses the loser rather than the application silently duplicating.

Transaction boundary
--------------------
Nothing here commits. The caller owns the transaction, for the same reason
``PaperBroker`` does: "evaluation persisted + candidates persisted" must be one
atomic unit, and only the caller knows where its unit ends. A partially written
evaluation that claimed ``CANDIDATES`` while its candidate rows were missing
would be worse than no record at all.

No credential is read, written or logged by this module.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    PaperAccountDecision,
    PaperBrokerOrder,
    StrategyCandidate,
    StrategyEvaluation,
)
from app.paper.fanout import AccountDecision
from app.strategy.match_b import MatchBCandidate, SessionEvaluation


class ForwardExperimentRepository:
    """Reads and writes the four 0014 tables. Commits nothing."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # -- Evaluations -------------------------------------------------------

    async def get_evaluation(
        self, *, strategy_version: str, universe_version: str, session: datetime
    ) -> StrategyEvaluation | None:
        """The stored evaluation for one session, or None if never run.

        ``None`` means ``NOT_EVALUATED`` -- the fourth state, which exists only
        as the absence of a row and must never be confused with a stored
        ``NO_OPPORTUNITY``.
        """
        stmt = select(StrategyEvaluation).where(
            StrategyEvaluation.strategy_version == strategy_version,
            StrategyEvaluation.universe_version == universe_version,
            StrategyEvaluation.session == session,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_evaluation(
        self,
        evaluation: SessionEvaluation,
        *,
        strategy_version: str,
        universe_version: str,
        universe_hash: str,
    ) -> tuple[StrategyEvaluation, bool]:
        """Persist one session's evaluation, once.

        Returns:
            The row and whether it was newly created. A caller that gets
            ``False`` has already done this work and must not re-fan its
            candidates.
        """
        existing = await self.get_evaluation(
            strategy_version=strategy_version,
            universe_version=universe_version,
            session=evaluation.session,
        )
        if existing is not None:
            return existing, False

        row = StrategyEvaluation(
            strategy_version=strategy_version,
            universe_version=universe_version,
            universe_hash=universe_hash,
            session=evaluation.session,
            data_cutoff=evaluation.session,
            evaluated_at=_now_of(evaluation),
            outcome=evaluation.outcome.value,
            universe_size=evaluation.universe_size,
            eligible_symbols=evaluation.eligible_symbols,
            candidate_count=evaluation.candidate_count,
            error=evaluation.error,
        )
        self._session.add(row)
        await self._session.flush()
        return row, True

    # -- Candidates --------------------------------------------------------

    async def get_candidate(self, candidate_id: str) -> StrategyCandidate | None:
        stmt = select(StrategyCandidate).where(StrategyCandidate.candidate_id == candidate_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_candidates(
        self, evaluation_row: StrategyEvaluation, candidates: tuple[MatchBCandidate, ...]
    ) -> list[StrategyCandidate]:
        """Persist an evaluation's candidates, skipping any already stored."""
        stored: list[StrategyCandidate] = []
        for candidate in candidates:
            existing = await self.get_candidate(candidate.candidate_id)
            if existing is not None:
                stored.append(existing)
                continue
            row = StrategyCandidate(
                candidate_id=candidate.candidate_id,
                evaluation_id=evaluation_row.id,
                symbol=candidate.symbol,
                session=candidate.session,
                rank=Decimal(str(candidate.rank)),
                sector=candidate.sector,
                sector_etf_return_20d=Decimal(str(candidate.sector_etf_return_20d)),
                movement_to_cost=Decimal(str(candidate.movement_to_cost)),
                atr_pct=Decimal(str(candidate.atr_pct)),
                reference_price=candidate.reference_price,
            )
            self._session.add(row)
            stored.append(row)
        await self._session.flush()
        return stored

    async def candidates_for(self, evaluation_id: int) -> list[StrategyCandidate]:
        stmt = (
            select(StrategyCandidate)
            .where(StrategyCandidate.evaluation_id == evaluation_id)
            .order_by(StrategyCandidate.rank.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- Account decisions -------------------------------------------------

    async def get_decision(self, candidate_id: str, slot: str) -> PaperAccountDecision | None:
        stmt = select(PaperAccountDecision).where(
            PaperAccountDecision.candidate_id == candidate_id,
            PaperAccountDecision.slot == slot,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_decision(
        self, decision: AccountDecision, *, decided_at: datetime
    ) -> tuple[PaperAccountDecision, bool]:
        """Persist one account's answer, once per (candidate, slot).

        Re-running the fan-out after a restart returns the stored decision
        rather than writing a second one -- otherwise a restart would double
        every coverage statistic the experiment exists to produce.
        """
        existing = await self.get_decision(decision.candidate_id, decision.slot.value)
        if existing is not None:
            return existing, False

        row = PaperAccountDecision(
            candidate_id=decision.candidate_id,
            slot=decision.slot.value,
            account_role=decision.role.value,
            decided_at=decided_at,
            equity=decision.equity,
            cash=decision.cash,
            effective_capital=decision.effective_capital,
            current_exposure=decision.current_exposure,
            risk_budget=decision.risk_budget,
            risk_regime=decision.risk_regime,
            risk_available=decision.risk_available,
            stop_distance=decision.stop_distance,
            risk_sized_quantity=decision.risk_sized_quantity,
            proposed_quantity=decision.proposed_quantity,
            proposed_notional=decision.proposed_notional,
            whole_share_feasible=decision.whole_share_feasible,
            outcome=decision.outcome.value,
            rejection_reason=(None if decision.is_actionable else decision.outcome.value),
            binding_constraint=decision.binding_constraint,
            detail=decision.detail[:512] or None,
        )
        self._session.add(row)
        await self._session.flush()
        return row, True

    async def decisions_for_slot(
        self, slot: str, *, limit: int = 1000
    ) -> list[PaperAccountDecision]:
        stmt = (
            select(PaperAccountDecision)
            .where(PaperAccountDecision.slot == slot)
            .order_by(PaperAccountDecision.decided_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- Broker orders -----------------------------------------------------

    async def get_order(self, client_order_id: str) -> PaperBrokerOrder | None:
        """Look an order up by the key the **broker** also knows it by.

        ``client_order_id`` is the recovery boundary for the hardest failure in
        this system: the broker accepted an order and local persistence then
        died. After a restart the same key is queryable at Alpaca, so the
        correct response is to recover broker truth -- never to submit again
        because the local row is missing a ``broker_order_id``.
        """
        stmt = select(PaperBrokerOrder).where(PaperBrokerOrder.client_order_id == client_order_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_order_intent(
        self,
        *,
        client_order_id: str,
        slot: str,
        candidate_id: str,
        symbol: str,
        quantity: Decimal,
        order_class: str,
        stop_price: Decimal | None,
        target_price: Decimal | None,
    ) -> tuple[PaperBrokerOrder, bool]:
        """Persist the intent to submit, **before** anything is sent.

        Written first on purpose. If the process dies between this write and the
        broker call, recovery finds an ``ORDER_READY`` row and knows to ask the
        broker whether that ``client_order_id`` exists. Writing only after a
        successful submission would leave the opposite, unrecoverable gap: an
        order at the broker that nothing locally knows about.

        No fabricated ``broker_order_id`` is stored -- it stays NULL until the
        broker supplies one.
        """
        existing = await self.get_order(client_order_id)
        if existing is not None:
            return existing, False

        row = PaperBrokerOrder(
            client_order_id=client_order_id,
            slot=slot,
            candidate_id=candidate_id,
            symbol=symbol,
            side="BUY",
            order_class=order_class,
            requested_quantity=quantity,
            status="ORDER_READY",
            stop_price=stop_price,
            target_price=target_price,
        )
        self._session.add(row)
        await self._session.flush()
        return row, True

    async def apply_reconciliation(
        self,
        client_order_id: str,
        *,
        broker_order_id: str | None,
        status: str,
        filled_quantity: Decimal,
        filled_avg_price: Decimal | None,
        filled_at: datetime | None = None,
        submitted_at: datetime | None = None,
        rejection_reason: str | None = None,
        protected_quantity: Decimal | None = None,
    ) -> PaperBrokerOrder | None:
        """Update one order from broker truth. Never invents a fill."""
        row = await self.get_order(client_order_id)
        if row is None:
            return None
        row.broker_order_id = broker_order_id or row.broker_order_id
        row.status = status
        row.filled_quantity = filled_quantity
        row.filled_avg_price = filled_avg_price
        row.filled_at = filled_at or row.filled_at
        row.submitted_at = submitted_at or row.submitted_at
        row.rejection_reason = rejection_reason or row.rejection_reason
        if protected_quantity is not None:
            row.protected_quantity = protected_quantity
        await self._session.flush()
        return row

    async def open_orders_for_slot(self, slot: str) -> list[PaperBrokerOrder]:
        """Orders that are not in a terminal state, for reconciliation."""
        terminal = ("FILLED", "CANCELED", "REJECTED", "EXPIRED")
        stmt = select(PaperBrokerOrder).where(
            PaperBrokerOrder.slot == slot,
            PaperBrokerOrder.status.notin_(terminal),
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def unprotected_positions(self, slot: str) -> list[PaperBrokerOrder]:
        """Filled quantity the broker is not holding protection for.

        The invariant that must never sit silently healthy. A non-empty result
        means a position is partly or wholly naked and the slot needs freezing
        until reconciled.
        """
        stmt = select(PaperBrokerOrder).where(
            PaperBrokerOrder.slot == slot,
            PaperBrokerOrder.filled_quantity > PaperBrokerOrder.protected_quantity,
            PaperBrokerOrder.status.in_(("FILLED", "PARTIALLY_FILLED")),
        )
        return list((await self._session.execute(stmt)).scalars().all())


def _now_of(evaluation: SessionEvaluation) -> datetime:
    """Evaluation timestamp, taken from the candidates when present.

    Uses the decision instant the strategy itself recorded rather than wall
    clock, so a replayed evaluation persists the time the decision was made.
    """
    if evaluation.candidates:
        return evaluation.candidates[0].decided_at
    return evaluation.session
