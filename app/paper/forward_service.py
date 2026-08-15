"""The wiring. Every component already exists; this connects them safely.

No new policy lives here. Sizing is ``size_position``, risk is ``risk_gate``,
cost is ``estimate_round_trip_cost``, the race is ``lifecycle``, the clock is
``holding_clock``, persistence is ``ForwardExperimentRepository``, submission is
``PaperOrderSubmitter``. This module decides only *order of operations* — and in
a trading system that ordering is itself the safety property.

Two orderings carry the whole design
------------------------------------
**Intent is persisted before the broker is called.** If the process dies between
the write and the call, recovery finds an ``ORDER_READY`` row and asks the broker
whether that ``client_order_id`` exists. Writing after submission would leave the
unrecoverable case instead: an order at the broker that nothing locally knows
about.

**Broker truth is read before anything is believed.** A local position opens only
on a confirmed fill quantity and price. A sell is sized only from a confirmed
broker position. Nothing here infers a fill from an acknowledgement.

Failure is refusal
------------------
Every method returns an outcome. Ambiguity — an unreadable broker, an unknown
order state — produces a refusal plus a slot freeze, never a guess and never a
second order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from app.broker.paper_accounts import PaperAccountSlot, PaperExecutionRefusedError
from app.broker.paper_orders import (
    BrokerOrderStatus,
    OrderReconciliation,
    PaperOrderRequest,
    ProtectedEntryPlan,
    build_protected_entry,
    normalise_status,
)
from app.core.logging import get_logger
from app.paper.lifecycle import (
    BrokerView,
    ExitStage,
    SlotState,
    SyncStatus,
    plan_protected_exit,
    reconcile_slot,
)
from app.strategy.holding_clock import HoldingState, holding_age

logger = get_logger(__name__)


class ActionOutcome(StrEnum):
    """What one wiring step concluded."""

    ORDER_READY = "ORDER_READY"
    """Constructed and persisted, not sent. The terminal state while the
    execution flag is false."""

    SUBMITTED = "SUBMITTED"
    RECOVERED = "RECOVERED"
    """A prior submission was found at the broker. **Never resubmitted.**"""

    REFUSED = "REFUSED"
    FROZEN = "FROZEN"
    NOTHING_TO_DO = "NOTHING_TO_DO"


@dataclass(frozen=True, slots=True)
class ActionResult:
    slot: PaperAccountSlot
    outcome: ActionOutcome
    client_order_id: str | None = None
    quantity: Decimal = Decimal(0)
    detail: str = ""

    @property
    def sent_to_broker(self) -> bool:
        return self.outcome is ActionOutcome.SUBMITTED


class OrderLookup(Protocol):
    """Broker-side lookup by the key we chose, for crash recovery."""

    def find_by_client_order_id(self, client_order_id: str) -> Any | None: ...


def entry_key(slot: PaperAccountSlot, candidate_id: str) -> str:
    return f"paper:{slot.value}:{candidate_id}"


def exit_key(slot: PaperAccountSlot, candidate_id: str, reason: str) -> str:
    """Deterministic, and distinct from the entry key.

    Reason-scoped so a stop-driven close and a max-hold close cannot collide,
    while a retry of the *same* close reuses the same key and is deduplicated by
    the broker.
    """
    return f"paper:{slot.value}:{candidate_id}:exit:{reason}"


@dataclass(slots=True)
class ForwardPaperService:
    """Orchestrates one slot's lifecycle. Bound to a slot; never crosses.

    Every collaborator is injected so tests exercise the **production** services
    against broker doubles rather than a parallel fake pipeline.
    """

    slot: PaperAccountSlot
    repository: Any
    submitter: Any
    broker: BrokerView
    lookup: OrderLookup
    execution_enabled: bool = False
    slot_state: SlotState = SlotState.ACTIVE
    consecutive_clean: int = 0
    events: list[tuple[str, str]] = field(default_factory=list)

    # -- Entry -------------------------------------------------------------

    async def prepare_entry(  # noqa: PLR0911 -- one return per way an entry can
        # stop; each is a different operational situation and must stay distinct
        self,
        *,
        candidate_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        target_price: Decimal | None,
        state: Any,
        capital: Any,
    ) -> ActionResult:
        """Persist intent, recover any prior submission, then submit or stop.

        The recovery check happens **before** any submission, always — not only
        after a known crash. A restart cannot tell whether it crashed before or
        after the broker call, so the only safe assumption is that it might have.
        """
        if not self.slot_state.may_open_new_positions:
            return self._refuse(ActionOutcome.FROZEN, f"slot is {self.slot_state.value}")

        key = entry_key(self.slot, candidate_id)

        try:
            plan = ProtectedEntryPlan(
                slot=self.slot,
                symbol=symbol,
                quantity=quantity,
                stop_price=stop_price,
                take_profit_price=target_price,
                idempotency_key=key,
                candidate_id=candidate_id,
            )
            build_protected_entry(plan)  # constructed to validate; never sent here
        except PaperExecutionRefusedError as exc:
            return self._refuse(ActionOutcome.REFUSED, str(exc))

        _row, created = await self.repository.record_order_intent(
            client_order_id=key,
            slot=self.slot.value,
            candidate_id=candidate_id,
            symbol=symbol,
            quantity=quantity,
            order_class="bracket" if target_price else "oto",
            stop_price=stop_price,
            target_price=target_price,
        )

        recovered = self._recover(key)
        if recovered is not None:
            await self._apply(key, recovered)
            return ActionResult(
                self.slot,
                ActionOutcome.RECOVERED,
                key,
                quantity,
                f"broker already holds this order ({recovered.status.value})",
            )
        if recovered is None and self._lookup_failed:
            self._freeze("broker lookup failed; cannot rule out an existing order")
            return self._refuse(ActionOutcome.FROZEN, "broker lookup failed")

        if not self.execution_enabled:
            return ActionResult(
                self.slot,
                ActionOutcome.ORDER_READY,
                key,
                quantity,
                "execution disabled; order constructed and persisted, not sent",
            )

        try:
            reconciliation = self.submitter.submit_entry(
                PaperOrderRequest(
                    slot=self.slot,
                    symbol=symbol,
                    quantity=quantity,
                    decision_id=0,
                    idempotency_key=key,
                    notional=quantity * stop_price,
                    fractional=False,
                ),
                state=state,
                capital=capital,
                risk_permits=True,
                already_submitted=not created,
                asset_fractionable=False,
            )
        except PaperExecutionRefusedError as exc:
            return self._refuse(ActionOutcome.REFUSED, str(exc))

        await self._apply(key, reconciliation)
        self.events.append(("PAPER_ENTRY", f"{symbol} x{quantity}"))
        return ActionResult(self.slot, ActionOutcome.SUBMITTED, key, quantity, "submitted")

    # -- Exit --------------------------------------------------------------

    async def close_on_expiry(  # noqa: PLR0911 -- one return per way a close can
        # stop; collapsing any two would lose which check refused
        self,
        *,
        candidate_id: str,
        symbol: str,
        entry_filled_at: datetime,
        now: datetime,
        local_quantity: Decimal,
        reason: str = "MAX_HOLDING_PERIOD",
    ) -> ActionResult:
        """Close a position whose frozen horizon has elapsed.

        Delegates the dangerous part wholesale to
        :func:`~app.paper.lifecycle.plan_protected_exit`, which reads the broker
        twice around the cancel. Nothing is sized from local belief.
        """
        age = holding_age(entry_filled_at=entry_filled_at, now=now)
        if age.state is HoldingState.UNKNOWN:
            self._freeze("holding age unresolvable")
            return self._refuse(ActionOutcome.FROZEN, "holding clock UNKNOWN")
        if not age.should_exit:
            return ActionResult(
                self.slot,
                ActionOutcome.NOTHING_TO_DO,
                detail=f"held {age.sessions_held}/{age.horizon_sessions} sessions",
            )

        plan = plan_protected_exit(
            self.broker, slot=self.slot, symbol=symbol, intended_quantity=local_quantity
        )
        if plan.stage is ExitStage.NOTHING_TO_CLOSE:
            return ActionResult(
                self.slot, ActionOutcome.NOTHING_TO_DO, detail=plan.detail or "already closed"
            )
        if plan.stage is ExitStage.AMBIGUOUS:
            self._freeze(f"exit ambiguous: {plan.detail}")
            return self._refuse(ActionOutcome.FROZEN, plan.detail)
        if not plan.may_sell:
            return self._refuse(ActionOutcome.REFUSED, plan.detail)

        key = exit_key(self.slot, candidate_id, reason)
        await self.repository.record_order_intent(
            client_order_id=key,
            slot=self.slot.value,
            candidate_id=candidate_id,
            symbol=symbol,
            quantity=plan.sell_quantity,
            order_class="simple",
            stop_price=None,
            target_price=None,
        )

        recovered = self._recover(key)
        if recovered is not None:
            await self._apply(key, recovered)
            return ActionResult(
                self.slot,
                ActionOutcome.RECOVERED,
                key,
                plan.sell_quantity,
                "exit already submitted at the broker",
            )
        if self._lookup_failed:
            self._freeze("broker lookup failed before exit")
            return self._refuse(ActionOutcome.FROZEN, "broker lookup failed")

        if not self.execution_enabled:
            return ActionResult(
                self.slot,
                ActionOutcome.ORDER_READY,
                key,
                plan.sell_quantity,
                "execution disabled; exit constructed and persisted, not sent",
            )

        reconciliation = self.submitter.submit_exit(
            slot=self.slot, symbol=symbol, quantity=plan.sell_quantity, client_order_id=key
        )
        await self._apply(key, reconciliation)
        self.events.append(("PAPER_EXIT", f"{symbol} x{plan.sell_quantity} {reason}"))
        return ActionResult(self.slot, ActionOutcome.SUBMITTED, key, plan.sell_quantity, reason)

    # -- Reconciliation ----------------------------------------------------

    async def reconcile(
        self,
        *,
        broker_positions: dict[str, Decimal],
        local_positions: dict[str, Decimal],
        broker_reachable: bool = True,
    ) -> SyncStatus:
        """Run the validated reconciliation policy and act on its verdict."""
        unprotected = await self.repository.unprotected_positions(self.slot.value)
        result = reconcile_slot(
            slot=self.slot,
            broker_positions=broker_positions,
            local_positions=local_positions,
            unprotected_symbols=tuple(o.symbol for o in unprotected),
            broker_reachable=broker_reachable,
        )
        if result.status is SyncStatus.IN_SYNC:
            self.consecutive_clean += 1
            if self.slot_state is not SlotState.ACTIVE and self.consecutive_clean >= 2:  # noqa: PLR2004
                self.slot_state = SlotState.ACTIVE
                self.events.append(("PAPER_SLOT_RECOVERED", self.slot.value))
        else:
            self.consecutive_clean = 0
            if result.slot_state is not self.slot_state:
                self.slot_state = result.slot_state
                self.events.append(("PAPER_RECONCILIATION_ERROR", result.detail))
        return result.status

    def may_open_positions(self) -> bool:
        return self.slot_state.may_open_new_positions

    # -- Internals ---------------------------------------------------------

    _lookup_failed: bool = False

    def _recover(self, client_order_id: str) -> OrderReconciliation | None:
        """Ask the broker whether it already holds this order.

        A lookup *failure* is recorded separately from a lookup *miss*: "the
        broker says no such order" permits a first submission, while "we could
        not ask" does not, because resubmitting on an unanswered question is
        exactly how a duplicate BUY happens.
        """
        self._lookup_failed = False
        try:
            found = self.lookup.find_by_client_order_id(client_order_id)
        except Exception as exc:
            self._lookup_failed = True
            logger.warning(
                "broker order lookup failed", slot=self.slot.value, error=type(exc).__name__
            )
            return None
        if found is None:
            return None
        return OrderReconciliation(
            slot=self.slot,
            broker_order_id=str(getattr(found, "id", "") or ""),
            decision_id=0,
            symbol=str(getattr(found, "symbol", "")),
            side=str(getattr(found, "side", "buy")),
            requested_quantity=Decimal(str(getattr(found, "qty", 0) or 0)),
            order_type=str(getattr(found, "order_type", "market")),
            submitted_at=getattr(found, "submitted_at", None),
            status=normalise_status(getattr(found, "status", None)),
            filled_quantity=Decimal(str(getattr(found, "filled_qty", 0) or 0)),
            filled_avg_price=(
                Decimal(str(found.filled_avg_price))
                if getattr(found, "filled_avg_price", None) is not None
                else None
            ),
            filled_at=getattr(found, "filled_at", None),
        )

    async def _apply(self, client_order_id: str, reconciliation: OrderReconciliation) -> None:
        """Write broker truth back, and protect only what actually filled.

        ``protected_quantity`` is set from the **filled** quantity, never the
        requested one. A partial fill on a bracket leaves the broker protecting
        the filled portion; recording the request instead would claim protection
        over shares that were never bought.
        """
        protected = (
            reconciliation.filled_quantity
            if reconciliation.status
            in (BrokerOrderStatus.FILLED, BrokerOrderStatus.PARTIALLY_FILLED)
            else Decimal(0)
        )
        await self.repository.apply_reconciliation(
            client_order_id,
            broker_order_id=reconciliation.broker_order_id or None,
            status=reconciliation.status.value,
            filled_quantity=reconciliation.filled_quantity,
            filled_avg_price=reconciliation.filled_avg_price,
            filled_at=reconciliation.filled_at,
            submitted_at=reconciliation.submitted_at,
            rejection_reason=reconciliation.rejection_reason,
            protected_quantity=protected,
        )
        if reconciliation.status is BrokerOrderStatus.UNKNOWN:
            self._freeze(f"unknown broker state for {client_order_id}")

    def _freeze(self, reason: str) -> None:
        if self.slot_state is SlotState.ACTIVE:
            self.events.append(("PAPER_SLOT_FROZEN", reason))
        self.slot_state = SlotState.RECONCILIATION_REQUIRED
        self.consecutive_clean = 0

    def _refuse(self, outcome: ActionOutcome, detail: str) -> ActionResult:
        if outcome is ActionOutcome.REFUSED:
            self.events.append(("PAPER_ORDER_ERROR", detail))
        return ActionResult(self.slot, outcome, detail=detail)


def may_open_local_position(
    reconciliation: OrderReconciliation, protected_quantity: Decimal
) -> bool:
    """Whether broker truth justifies opening a local position.

    Requires a real filled quantity, a real price, and protection covering the
    whole filled amount. A partially protected fill returns False: the position
    exists at the broker and must be reconciled, but the local ledger must not
    record it as a healthy protected position.
    """
    return (
        reconciliation.opens_position
        and protected_quantity >= reconciliation.filled_quantity
        and reconciliation.filled_quantity > 0
    )
