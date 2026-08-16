"""Reading a real account, and nothing else.

What this file is
-----------------
The shape of a broker account as Portfolio Fit needs to see it, and a protocol
with exactly three read capabilities: which slots exist, what one slot holds,
and how many orders are open (informational only -- an open order is a fact
about the account, and counting facts is not acting on them).

There is no method here that could change anything. That is not a convention
kept by discipline; the protocol has no such member, so an implementation that
grew one would satisfy nothing this package calls, and a structural test asserts
the package imports no broker module at all.

Why the concrete adapter lives elsewhere
----------------------------------------
The Alpaca implementation needs credentials and the vendor SDK -- and that SDK's
client is capable of order submission whether or not anyone calls it. Keeping it
outside this package means the capability is never *importable* from here, which
is a stronger guarantee than never invoking it.

So: this package defines the shape, ``app.broker.paper_snapshots`` fills it in,
and the dependency points inward. Portfolio Fit does not know Alpaca exists.

Absence is a state, not an exception
------------------------------------
An unconfigured slot, an authentication failure and an empty account are three
different ordinary situations. Each produces a snapshot describing itself, so a
caller cannot forget to handle one, and so a single failing account can never
substitute another account's data for its own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.portfolio_fit.schemas import Portfolio, Position


def safe_account_reference(account_number: str) -> str:
    """A stable, non-identifying handle for one account.

    Enough to prove two snapshots came from *different* accounts, which is the
    whole point of the isolation check, without carrying the account number into
    a report, an artifact or a log line.
    """
    if not account_number:
        return "unknown"
    return "acct-" + hashlib.sha256(account_number.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class SnapshotPosition:
    """One holding as the broker reports it.

    ``average_entry_price`` and ``unrealised`` are optional because not every
    broker reports them, and an assumed cost basis produces a P&L figure that
    looks authoritative and is invented.
    """

    symbol: str
    quantity: float
    market_value: float
    price: float | None = None
    average_entry_price: float | None = None
    unrealised: float | None = None

    @property
    def cost_basis(self) -> float | None:
        if self.average_entry_price is None:
            return None
        return self.quantity * self.average_entry_price


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """One account at one moment. Balances and holdings, never credentials.

    ``usable_capital`` is not the broker's buying power. Two of the three paper
    accounts are margin accounts offering four times equity; the experiment they
    belong to has never used leverage, so capital is capped at equity and the
    difference is reported as ``leverage_withheld`` rather than silently
    discarded.
    """

    slot: str
    as_of: str
    available: bool
    equity: float = 0.0
    cash: float = 0.0
    broker_buying_power: float = 0.0
    usable_capital: float = 0.0
    leverage_withheld: float = 0.0
    positions: tuple[SnapshotPosition, ...] = ()
    open_orders: int = 0
    account_reference: str | None = None
    verified_paper: bool = False
    error: str | None = None

    @property
    def is_flat(self) -> bool:
        """No holdings. A valid state, and the state all three began in."""
        return not self.positions

    def to_portfolio(self) -> Portfolio:
        """The snapshot as a portfolio the analysis layer can describe.

        Cash comes from the account's cash balance rather than usable capital,
        because exposure percentages must add up to what the account actually
        holds. The leverage cap is a constraint on hypotheticals, not a claim
        that the money is missing.
        """
        positions = tuple(
            Position(
                symbol=p.symbol,
                quantity=p.quantity,
                price=p.price if p.price is not None else _implied_price(p),
                cost_basis=p.cost_basis,
            )
            for p in self.positions
        )
        return Portfolio(self.slot, self.cash, positions, self.as_of)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "as_of": self.as_of,
            "available": self.available,
            "verified_paper": self.verified_paper,
            "account_reference": self.account_reference,
            "equity": self.equity,
            "cash": self.cash,
            "broker_buying_power": self.broker_buying_power,
            "usable_capital": self.usable_capital,
            "leverage_withheld": self.leverage_withheld,
            "open_orders": self.open_orders,
            "position_count": len(self.positions),
            "flat": self.is_flat,
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "market_value": p.market_value,
                    "price": p.price,
                    "average_entry_price": p.average_entry_price,
                    "unrealised": p.unrealised,
                }
                for p in self.positions
            ],
            "error": self.error,
        }


def _implied_price(position: SnapshotPosition) -> float:
    if position.quantity:
        return position.market_value / position.quantity
    return 0.0


def unavailable(slot: str, as_of: str, reason: str) -> AccountSnapshot:
    """A refusal shaped like a snapshot, so no caller can skip checking it."""
    return AccountSnapshot(slot=slot, as_of=as_of, available=False, error=reason)


@runtime_checkable
class PortfolioAccountReader(Protocol):
    """Read access to broker accounts. Three capabilities, all read.

    Implementations must not raise for an ordinary absence -- an unconfigured
    slot, a network failure or an empty account are all described by the
    returned snapshot instead.
    """

    @property
    def slots(self) -> tuple[str, ...]:
        """Every slot this reader knows about, configured or not."""
        ...

    def snapshot(self, slot: str) -> AccountSnapshot:
        """Balances, holdings and open-order count for one slot."""
        ...
