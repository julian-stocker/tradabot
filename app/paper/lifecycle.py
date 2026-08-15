"""Closing a protected paper position without ever going short.

The danger
----------
A protected position carries a **live broker stop**. When the 3-session horizon
expires, tradabot wants to sell — but the stop can fill at any moment, including
while the cancel request is in flight. Sell first and the stop may fill after,
leaving the account **net short** in a system that has never validated a short.
Cancel first and trust it, and a stop that filled during cancellation leaves a
sell for shares that no longer exist.

The rule
--------
Broker position quantity is the only truth, and it is read **twice**:

    reconcile → cancel protection if any → reconcile again → sell the confirmed
    remainder only

The second reconcile is the whole safety argument. Everything between the two
reads — a stop filling, a partial fill, a rejected cancel — shows up as a
smaller confirmed position, and the sell shrinks to match. If the second read
cannot be obtained, the outcome is ``AMBIGUOUS`` and **nothing is sent**.

Every path that cannot establish the position with certainty refuses. There is
no branch here that guesses a quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.broker.paper_accounts import PaperAccountSlot
from app.broker.paper_orders import BrokerOrderStatus


class ExitStage(StrEnum):
    """How far a close attempt got, and why it stopped."""

    NOTHING_TO_CLOSE = "NOTHING_TO_CLOSE"
    """The broker reports no position. Common and safe: the stop already filled."""

    READY_TO_SELL = "READY_TO_SELL"
    PROTECTION_CANCEL_REQUIRED = "PROTECTION_CANCEL_REQUIRED"
    AMBIGUOUS = "AMBIGUOUS"
    """Broker truth could not be established. **Nothing is sent.**"""

    BLOCKED_SHORT_RISK = "BLOCKED_SHORT_RISK"
    """The requested quantity exceeds the confirmed position. Never sent."""


class SlotState(StrEnum):
    """Operational state of one account."""

    ACTIVE = "ACTIVE"
    ENTRY_FROZEN = "ENTRY_FROZEN"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

    @property
    def may_open_new_positions(self) -> bool:
        return self is SlotState.ACTIVE


class SyncStatus(StrEnum):
    IN_SYNC = "IN_SYNC"
    RECOVERABLE = "RECOVERABLE"
    AMBIGUOUS = "AMBIGUOUS"
    ERROR = "ERROR"

    @property
    def requires_freeze(self) -> bool:
        return self in (SyncStatus.AMBIGUOUS, SyncStatus.ERROR)


@dataclass(frozen=True, slots=True)
class BrokerPositionView:
    """What the broker says it holds, right now."""

    symbol: str
    quantity: Decimal
    available_quantity: Decimal
    """Quantity not reserved by a working order. Selling more than this is how a
    duplicate sell becomes a short."""


@dataclass(frozen=True, slots=True)
class ProtectiveOrderView:
    """A working protective child order."""

    order_id: str
    symbol: str
    quantity: Decimal
    status: BrokerOrderStatus

    @property
    def is_working(self) -> bool:
        return self.status in (
            BrokerOrderStatus.NEW,
            BrokerOrderStatus.ACCEPTED,
            BrokerOrderStatus.PARTIALLY_FILLED,
        )


class BrokerView(Protocol):
    """The minimum read/cancel surface a safe close needs.

    Deliberately narrow: this module can read positions, read protective orders
    and cancel one. It cannot submit — submission stays in
    :class:`~app.broker.paper_orders.PaperOrderSubmitter`, so the race logic and
    the order path cannot drift into each other.
    """

    def position(self, symbol: str) -> BrokerPositionView | None: ...
    def protective_orders(self, symbol: str) -> list[ProtectiveOrderView]: ...
    def cancel(self, order_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ExitPlan:
    """The outcome of planning a close. A quantity, or a refusal with a reason."""

    slot: PaperAccountSlot
    symbol: str
    stage: ExitStage
    sell_quantity: Decimal = Decimal(0)
    cancelled_orders: tuple[str, ...] = ()
    detail: str = ""

    @property
    def may_sell(self) -> bool:
        return self.stage is ExitStage.READY_TO_SELL and self.sell_quantity > 0


def plan_protected_exit(  # noqa: PLR0911 -- one return per way a close can be unsafe;
    # collapsing any two would lose which check refused, and each refusal is a
    # different operational situation
    broker: BrokerView,
    *,
    slot: PaperAccountSlot,
    symbol: str,
    intended_quantity: Decimal,
) -> ExitPlan:
    """Plan a safe close: read, cancel, **read again**, sell the remainder.

    Args:
        broker: read/cancel surface for this slot only.
        intended_quantity: what local state believes is held. Used purely as an
            upper bound -- the broker's number always wins, and a larger local
            belief never widens the sell.

    Returns:
        An :class:`ExitPlan`. ``may_sell`` is true only when a confirmed
        position remains, all protection is cancelled, and the quantity is fully
        available.
    """

    def refuse(stage: ExitStage, detail: str, cancelled: tuple[str, ...] = ()) -> ExitPlan:
        return ExitPlan(
            slot=slot, symbol=symbol, stage=stage, cancelled_orders=cancelled, detail=detail
        )

    # --- First read -------------------------------------------------------
    try:
        before = broker.position(symbol)
    except Exception as exc:
        return refuse(ExitStage.AMBIGUOUS, f"position read failed: {type(exc).__name__}")

    if before is None or before.quantity <= 0:
        return refuse(ExitStage.NOTHING_TO_CLOSE, "broker reports no position")

    # --- Cancel working protection ---------------------------------------
    cancelled: list[str] = []
    try:
        working = [o for o in broker.protective_orders(symbol) if o.is_working]
    except Exception as exc:
        return refuse(ExitStage.AMBIGUOUS, f"protective read failed: {type(exc).__name__}")

    for order in working:
        try:
            if broker.cancel(order.order_id):
                cancelled.append(order.order_id)
            else:
                # A refused cancel is not fatal by itself -- the stop may have
                # just filled, which the second read will reveal as a smaller
                # position. It is fatal only if protection is still working then.
                pass
        except Exception as exc:
            return refuse(
                ExitStage.AMBIGUOUS,
                f"cancel of {order.order_id} failed: {type(exc).__name__}",
                tuple(cancelled),
            )

    # --- Second read: the safety argument ---------------------------------
    try:
        after = broker.position(symbol)
        still_working = [o for o in broker.protective_orders(symbol) if o.is_working]
    except Exception as exc:
        return refuse(
            ExitStage.AMBIGUOUS, f"re-read failed: {type(exc).__name__}", tuple(cancelled)
        )

    if after is None or after.quantity <= 0:
        # The stop filled during cancellation. Correct and safe -- the position
        # is closed and no sell may be sent.
        return ExitPlan(
            slot=slot,
            symbol=symbol,
            stage=ExitStage.NOTHING_TO_CLOSE,
            cancelled_orders=tuple(cancelled),
            detail="position closed by protection during cancellation",
        )

    if still_working:
        return refuse(
            ExitStage.PROTECTION_CANCEL_REQUIRED,
            f"{len(still_working)} protective order(s) still working; refusing to sell "
            f"into live protection",
            tuple(cancelled),
        )

    sellable = min(after.quantity, after.available_quantity, intended_quantity)
    if sellable <= 0:
        return refuse(
            ExitStage.BLOCKED_SHORT_RISK,
            f"confirmed {after.quantity} available {after.available_quantity}",
            tuple(cancelled),
        )
    if sellable != sellable.to_integral_value():
        return refuse(
            ExitStage.BLOCKED_SHORT_RISK,
            f"confirmed quantity {sellable} is not whole",
            tuple(cancelled),
        )

    return ExitPlan(
        slot=slot,
        symbol=symbol,
        stage=ExitStage.READY_TO_SELL,
        sell_quantity=sellable,
        cancelled_orders=tuple(cancelled),
        detail=f"confirmed {after.quantity}, selling {sellable}",
    )


@dataclass(frozen=True, slots=True)
class SlotReconciliation:
    """One account's local state measured against broker truth."""

    slot: PaperAccountSlot
    status: SyncStatus
    broker_positions: int
    local_positions: int
    unprotected: int
    orphan_orders: int
    detail: str = ""

    @property
    def slot_state(self) -> SlotState:
        if self.status is SyncStatus.ERROR:
            return SlotState.BROKER_UNAVAILABLE
        if self.status is SyncStatus.AMBIGUOUS:
            return SlotState.RECONCILIATION_REQUIRED
        if self.status is SyncStatus.RECOVERABLE:
            return SlotState.ENTRY_FROZEN
        return SlotState.ACTIVE


def reconcile_slot(
    *,
    slot: PaperAccountSlot,
    broker_positions: dict[str, Decimal],
    local_positions: dict[str, Decimal],
    unprotected_symbols: tuple[str, ...] = (),
    broker_reachable: bool = True,
) -> SlotReconciliation:
    """Compare one slot's local beliefs to the broker's. **Freezes nothing else.**

    Each slot is judged alone; a mismatch in PAPER_1K says nothing about
    PAPER_3K, and no candidate or order is ever rerouted between them.

    A mismatch never triggers liquidation. Broker-native protection stays in
    place, which is precisely what makes freezing safe rather than urgent.
    """
    if not broker_reachable:
        return SlotReconciliation(
            slot=slot,
            status=SyncStatus.ERROR,
            broker_positions=0,
            local_positions=len(local_positions),
            unprotected=0,
            orphan_orders=0,
            detail="broker unreachable",
        )

    if unprotected_symbols:
        return SlotReconciliation(
            slot=slot,
            status=SyncStatus.AMBIGUOUS,
            broker_positions=len(broker_positions),
            local_positions=len(local_positions),
            unprotected=len(unprotected_symbols),
            orphan_orders=0,
            detail=f"unprotected position(s): {', '.join(sorted(unprotected_symbols))}",
        )

    unknown = set(broker_positions) - set(local_positions)
    missing = set(local_positions) - set(broker_positions)
    mismatched = {
        s
        for s in set(broker_positions) & set(local_positions)
        if broker_positions[s] != local_positions[s]
    }

    if unknown:
        # A position at the broker that local state does not know about is the
        # dangerous direction: nothing would ever close it.
        return SlotReconciliation(
            slot=slot,
            status=SyncStatus.AMBIGUOUS,
            broker_positions=len(broker_positions),
            local_positions=len(local_positions),
            unprotected=0,
            orphan_orders=len(unknown),
            detail=f"broker holds unknown position(s): {', '.join(sorted(unknown))}",
        )
    if missing or mismatched:
        return SlotReconciliation(
            slot=slot,
            status=SyncStatus.RECOVERABLE,
            broker_positions=len(broker_positions),
            local_positions=len(local_positions),
            unprotected=0,
            orphan_orders=len(missing) + len(mismatched),
            detail=f"missing={sorted(missing)} mismatched={sorted(mismatched)}",
        )

    return SlotReconciliation(
        slot=slot,
        status=SyncStatus.IN_SYNC,
        broker_positions=len(broker_positions),
        local_positions=len(local_positions),
        unprotected=0,
        orphan_orders=0,
    )


def may_recover(reconciliation: SlotReconciliation, *, consecutive_clean: int) -> bool:
    """Whether a frozen slot may return to ``ACTIVE``.

    Requires the slot to be ``IN_SYNC`` on **two** consecutive reconciliations.
    One successful call is not evidence of recovery: the failure that froze the
    slot was itself often a transient read, and unfreezing on the first clean
    poll would flap between states while a real inconsistency persisted.
    """
    return reconciliation.status is SyncStatus.IN_SYNC and consecutive_clean >= 2  # noqa: PLR2004
