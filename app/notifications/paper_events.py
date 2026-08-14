"""Notification payloads for the three-account paper experiment. **Nothing sends.**

This module builds strings. It holds no webhook, no client and no transport, so
importing it cannot emit anything — a test asserts the absence. Phase 12.4 wires
these into the existing notification service once execution is authorised.

Two rules shape everything here
-------------------------------
**Every payload is marked PAPER and names its account slot.** Three accounts run
the same strategy simultaneously; a message that does not say which account it
came from is worse than no message, and one that could be mistaken for live
trading is dangerous.

**Rejections are aggregated, never streamed.** The dry run refuses far more
candidates than it accepts — that is the capital-adaptive behaviour working. One
Discord message per rejection would be hundreds a day and would train the reader
to mute the channel that also carries entries and risk events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.broker.paper_accounts import PaperAccountSlot

PAPER_MARKER: Final = "PAPER"
"""Prefixed to every payload. Never omitted, never abbreviated."""


class PaperEvent(StrEnum):
    """The four event types. **None of them is a recommendation.**"""

    ENTRY = "PAPER_ENTRY"
    EXIT = "PAPER_EXIT"
    RISK_EVENT = "PAPER_RISK_EVENT"
    DAILY_SUMMARY = "PAPER_DAILY_SUMMARY"


def _header(event: PaperEvent, slot: PaperAccountSlot, symbol: str | None = None) -> str:
    tail = f" {symbol}" if symbol else ""
    return f"[{PAPER_MARKER}] [{slot.value}] {event.value}{tail}"


@dataclass(frozen=True, slots=True)
class EntryPayload:
    """A paper position was opened. Reports what happened, advises nothing."""

    slot: PaperAccountSlot
    symbol: str
    quantity: Decimal
    notional: Decimal
    fill_price: Decimal
    risk_regime: str
    risk_budget: Decimal
    stop_distance: Decimal
    estimated_cost: Decimal

    def render(self) -> str:
        return "\n".join(
            [
                _header(PaperEvent.ENTRY, self.slot, self.symbol),
                f"  qty {self.quantity} @ {self.fill_price}  notional {self.notional}",
                f"  regime {self.risk_regime}  risk budget {self.risk_budget}",
                f"  stop distance {self.stop_distance}  modelled cost {self.estimated_cost}",
            ]
        )


@dataclass(frozen=True, slots=True)
class ExitPayload:
    """A paper position closed.

    ``broker_pnl`` and ``economic_pnl`` are carried separately and never summed:
    Alpaca paper charges no commission and fills optimistically, while tradabot's
    own accounting adds modelled spread and slippage. Merging them would produce
    a number that is neither what the broker reported nor what the strategy is
    believed to have earned.
    """

    slot: PaperAccountSlot
    symbol: str
    fill_price: Decimal
    broker_pnl: Decimal
    economic_pnl: Decimal
    costs: Decimal
    holding_duration: str
    exit_reason: str

    def render(self) -> str:
        return "\n".join(
            [
                _header(PaperEvent.EXIT, self.slot, self.symbol),
                f"  exit {self.fill_price}  reason {self.exit_reason}  "
                f"held {self.holding_duration}",
                f"  broker P&L {self.broker_pnl}  economic P&L {self.economic_pnl}",
                f"  costs {self.costs}",
            ]
        )


@dataclass(frozen=True, slots=True)
class RiskEventPayload:
    """A risk flag changed on an open position. **Never an instruction to exit.**"""

    slot: PaperAccountSlot
    symbol: str
    flag: str
    regime: str
    band_1d: Decimal
    detail: str = ""

    def render(self) -> str:
        return "\n".join(
            [
                _header(PaperEvent.RISK_EVENT, self.slot, self.symbol),
                f"  {self.flag}  regime {self.regime}  1d band {self.band_1d}%",
                f"  {self.detail}" if self.detail else "  descriptive only; no action implied",
            ]
        )


@dataclass(frozen=True, slots=True)
class DailySummaryPayload:
    """One message per account per day, carrying the aggregated rejections."""

    slot: PaperAccountSlot
    trading_date: datetime
    equity: Decimal
    cash: Decimal
    gross_exposure: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_cost: Decimal
    drawdown_pct: float
    open_positions: int
    trades_today: int
    rejections: dict[str, int]

    @property
    def rejected_total(self) -> int:
        return sum(self.rejections.values())

    def render(self) -> str:
        lines = [
            _header(PaperEvent.DAILY_SUMMARY, self.slot),
            f"  {self.trading_date.date().isoformat()}",
            f"  equity {self.equity}  cash {self.cash}  exposure {self.gross_exposure}",
            f"  realised {self.realized_pnl}  unrealised {self.unrealized_pnl}  "
            f"costs {self.total_cost}",
            f"  drawdown {self.drawdown_pct:.2%}  open {self.open_positions}  "
            f"trades {self.trades_today}",
        ]
        if self.rejections:
            top = sorted(self.rejections.items(), key=lambda kv: -kv[1])
            detail = ", ".join(f"{reason} x{count}" for reason, count in top)
            lines.append(f"  rejected {self.rejected_total}: {detail}")
        else:
            lines.append("  rejected 0")
        return "\n".join(lines)


@dataclass(slots=True)
class RejectionAggregator:
    """Counts refusals for the daily summary instead of announcing each one.

    Keyed by account so three accounts refusing the same candidate for three
    different reasons stay distinguishable.
    """

    counts: dict[PaperAccountSlot, dict[str, int]]

    @classmethod
    def empty(cls) -> RejectionAggregator:
        return cls(counts={slot: {} for slot in PaperAccountSlot})

    def record(self, slot: PaperAccountSlot, reason: str) -> None:
        bucket = self.counts.setdefault(slot, {})
        bucket[reason] = bucket.get(reason, 0) + 1

    def drain(self, slot: PaperAccountSlot) -> dict[str, int]:
        """Take and clear one account's tally, ready for its daily message."""
        out = dict(self.counts.get(slot, {}))
        self.counts[slot] = {}
        return out
