"""Broker abstraction.

**Interfaces only. No implementation, and no live trading.**

Relationship to ``app.backtesting.protocols``
---------------------------------------------
These two abstractions look similar and are not the same thing:

``ExecutionModel`` (backtesting)
    A *pure function*: given an order and a bar, what fill would occur? No state,
    no account, no order lifecycle. It answers "what price would I have got".

``Broker`` (here)
    A *stateful counterparty*: it holds cash and positions, accepts orders, and
    can reject or cancel them. It answers "what does my account look like".

A ``PaperBroker`` will be implemented in terms of an ``ExecutionModel`` -- it
delegates the fill-price question and keeps the bookkeeping. Merging them would
force a backtester's fill calculator to carry account state it has no use for.

Scope discipline
----------------
This interface is deliberately smaller than a real broker API. It has no bracket
orders, no trailing stops, no margin calls, no corporate-action handling on open
positions. Those are added when ``PaperBroker`` genuinely needs them, not in
anticipation -- an interface designed before its first implementation is a guess,
and every unused method is a constraint on the implementation that follows.

**A LiveBroker is not planned.** The absence of order routing is an architectural
boundary, not a missing feature (see docs/roadmap.md, permanent non-goals).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.backtesting.models import Fill, Order, Position


class OrderStatus(StrEnum):
    """Lifecycle state of a submitted order.

    ``REJECTED`` is distinct from ``CANCELLED``: the first is the broker refusing
    (insufficient capital, below minimum notional, market closed), the second is
    the caller withdrawing. Collapsing them would hide how often a strategy asks
    for something impossible.
    """

    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class OrderState:
    """A submitted order and what has become of it."""

    order_id: str
    order: Order
    status: OrderStatus
    submitted_at: datetime
    fills: tuple[Fill, ...] = ()
    reject_reason: str | None = None

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), Decimal(0))

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


@dataclass(frozen=True, slots=True)
class AccountState:
    """A point-in-time view of an account.

    ``cash`` and ``equity`` are separate because their difference is the whole
    story: equity includes unrealised P&L on open positions, cash does not. A
    system reporting only equity cannot tell you whether it has the money to
    place the next order.
    """

    cash: Decimal
    equity: Decimal
    currency: str
    open_positions: int
    total_exposure: Decimal
    as_of: datetime

    @property
    def exposure_ratio(self) -> Decimal:
        """Exposure as a fraction of equity; what risk limits are checked against."""
        return self.total_exposure / self.equity if self.equity > 0 else Decimal(0)


@runtime_checkable
class Broker(Protocol):
    """A counterparty that accepts orders and holds positions.

    Implementations: ``PaperBroker`` (phase 3). Nothing else is planned.

    Every method is async because a real broker would be network-bound, and a
    synchronous interface would have to be rewritten to accommodate one. That is
    the single piece of anticipation here, and it costs nothing.
    """

    @property
    def name(self) -> str:
        """Stable identifier, e.g. ``"paper"``."""
        ...

    async def place_order(self, order: Order) -> OrderState:
        """Submit an order.

        Must return a state rather than raising for ordinary refusals: "rejected,
        below minimum notional" is an outcome to record, not an exception. Raise
        only for genuine faults (the broker is unreachable, the request is
        malformed).
        """
        ...

    async def cancel_order(self, order_id: str) -> OrderState:
        """Withdraw an unfilled order.

        Raises:
            KeyError: no such order.
        """
        ...

    async def get_order(self, order_id: str) -> OrderState:
        """Current state of one order.

        Raises:
            KeyError: no such order.
        """
        ...

    async def get_positions(self) -> Sequence[Position]:
        """Every open position."""
        ...

    async def get_account_state(self) -> AccountState:
        """Cash, equity and exposure right now."""
        ...
