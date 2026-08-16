"""Reading the three paper accounts, and only reading them.

Where this sits
---------------
:mod:`app.broker.paper_accounts` already owns credentials, paper verification
and the no-leverage capital rule. This module adds one thing on top: the
*holdings* behind the position count that ``verify_account`` already returns, so
a portfolio can be described rather than merely counted.

It is deliberately not a second credential owner. The registry resolves keys,
:func:`~app.broker.paper_accounts.verify_account` proves the account is paper,
and :func:`~app.broker.paper_accounts.effective_capital` applies the equity cap.
This module calls those three and adds no policy of its own.

Read-only, structurally
-----------------------
Three vendor calls are made -- account, positions, open orders -- and no other.
The vendor client is capable of more; the guarantee here is not that the extra
capability is unused by habit but that it is unreachable from the analysis
layer, which cannot import this module's dependencies at all. A test parses this
file and asserts no mutating vendor call appears in it.

Isolation
---------
Each slot gets its own credentials and its own client, and a failure is recorded
against that slot alone. There is no fallback path: a slot with no credentials
returns an unavailable snapshot rather than borrowing another slot's keys, which
is the one failure mode that would silently mix two accounts' money.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.broker.paper_accounts import (
    PaperAccountRegistry,
    PaperAccountSlot,
    classify_endpoint,
    effective_capital,
    verify_account,
)
from app.core.logging import get_logger
from app.portfolio_fit.accounts import (
    AccountSnapshot,
    SnapshotPosition,
    safe_account_reference,
    unavailable,
)

logger = get_logger(__name__)

UNKNOWN_SLOT = "UNKNOWN_SLOT"
NOT_CONFIGURED = "SLOT_NOT_CONFIGURED"
NOT_VERIFIED_PAPER = "ACCOUNT_NOT_VERIFIED_AS_PAPER"

DEFAULT_DOTENV = Path(".env")
"""Where the paper credentials live. Read, never written."""


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    """``None`` stays ``None``. An unreported figure is never defaulted to zero,
    because a zero cost basis renders as a 100% gain."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PaperAccountSnapshotReader:
    """Read-only snapshots of the three Alpaca paper accounts.

    Satisfies :class:`~app.portfolio_fit.accounts.PortfolioAccountReader`.

    Args:
        registry: resolved credentials. Loaded from the environment if omitted.
        clients: injected vendor clients per slot, for tests.
    """

    def __init__(
        self,
        registry: PaperAccountRegistry | None = None,
        *,
        clients: dict[PaperAccountSlot, Any] | None = None,
        dotenv: Path | None = DEFAULT_DOTENV,
    ) -> None:
        # The registry parses the dotenv itself rather than exporting it, so no
        # credential is placed into the process environment where a subprocess
        # or a crash dump would pick it up.
        self._registry = (
            registry
            if registry is not None
            else PaperAccountRegistry.load(dotenv=dotenv)
        )
        self._clients = clients or {}

    @property
    def slots(self) -> tuple[str, ...]:
        return tuple(s.value for s in PaperAccountSlot)

    @property
    def configured_slots(self) -> tuple[str, ...]:
        return tuple(s.value for s in self._registry.accounts)

    def snapshot(self, slot: str) -> AccountSnapshot:
        """Balances, holdings and open-order count for one slot.

        Every ordinary failure -- an unknown name, missing credentials, an
        authentication error, an account the broker will not confirm is paper --
        comes back as an unavailable snapshot naming its own reason.
        """
        as_of = datetime.now(UTC).isoformat(timespec="seconds")
        name = str(slot).upper()
        try:
            resolved = PaperAccountSlot(name)
        except ValueError:
            return unavailable(name, as_of, f"{UNKNOWN_SLOT}: {name}")

        if resolved not in self._registry.accounts:
            return unavailable(resolved.value, as_of, NOT_CONFIGURED)
        if classify_endpoint(self._registry.base_url) == "LIVE":
            return unavailable(resolved.value, as_of, "REFUSED_LIVE_ENDPOINT")

        credentials = self._registry.credentials(resolved)
        client = self._clients.get(resolved)
        state = verify_account(
            credentials, base_url=self._registry.base_url, client=client
        )
        if state.error is not None:
            return unavailable(resolved.value, as_of, state.error)
        if not state.verified_paper:
            return unavailable(resolved.value, as_of, NOT_VERIFIED_PAPER)

        capital = effective_capital(state)
        positions = self._positions(resolved, credentials, client)
        return AccountSnapshot(
            slot=resolved.value,
            as_of=as_of,
            available=True,
            equity=float(state.equity),
            cash=float(state.cash),
            broker_buying_power=float(state.buying_power),
            usable_capital=float(capital.max_exposure),
            leverage_withheld=float(capital.leverage_withheld),
            positions=positions,
            open_orders=state.open_orders,
            account_reference=safe_account_reference(state.account_number),
            verified_paper=True,
        )

    def snapshots(self, slots: tuple[str, ...] | None = None) -> dict[str, AccountSnapshot]:
        """Every requested slot, each read independently of the others."""
        wanted = slots if slots is not None else self.slots
        return {name: self.snapshot(name) for name in wanted}

    # ------------------------------------------------------------- positions
    def _positions(
        self, slot: PaperAccountSlot, credentials: Any, client: Any
    ) -> tuple[SnapshotPosition, ...]:
        """Open holdings for one account. One vendor read, no mutation."""
        try:
            if client is None:
                # Imported here so this module stays importable without the SDK
                # installed, and so no credential is constructed unless an
                # account is genuinely being read.
                from alpaca.trading.client import TradingClient  # noqa: PLC0415

                client = TradingClient(
                    api_key=credentials.api_key.get_secret_value(),
                    secret_key=credentials.api_secret.get_secret_value(),
                    paper=True,
                )
            raw = client.get_all_positions()
        except Exception as exc:
            logger.warning(
                "positions unavailable", slot=slot.value, reason=type(exc).__name__
            )
            return ()
        return tuple(_position(item) for item in (raw or []))


def _position(item: Any) -> SnapshotPosition:
    quantity = _float(getattr(item, "qty", 0))
    market_value = _float(getattr(item, "market_value", 0))
    price = _optional_float(getattr(item, "current_price", None))
    if price is None and quantity:
        price = market_value / quantity
    return SnapshotPosition(
        symbol=str(getattr(item, "symbol", "") or "").upper(),
        quantity=quantity,
        market_value=market_value,
        price=price,
        average_entry_price=_optional_float(getattr(item, "avg_entry_price", None)),
        unrealised=_optional_float(getattr(item, "unrealized_pl", None)),
    )


def capital_summary(snapshot: AccountSnapshot) -> dict[str, Decimal | str | bool]:
    """Compact capital view for reporting. Derives nothing new."""
    return {
        "slot": snapshot.slot,
        "available": snapshot.available,
        "equity": Decimal(str(snapshot.equity)),
        "cash": Decimal(str(snapshot.cash)),
        "usable_capital": Decimal(str(snapshot.usable_capital)),
        "leverage_withheld": Decimal(str(snapshot.leverage_withheld)),
    }
