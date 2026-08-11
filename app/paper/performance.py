"""Portfolio performance metrics.

Derived from stored trades and snapshots. Nothing here is stored: a metric is a
function of the record, and caching it invites the cache and the record to
disagree.

What is deliberately absent
---------------------------
**Sharpe ratio.** It needs a return *series* with a defined periodicity, and
tradabot's snapshots are event-driven -- one per processed bar, per instrument.
Computing a Sharpe from irregularly spaced observations produces a number that
looks authoritative and means nothing. It arrives when a proper return-series
design does (docs/roadmap.md).

**Annualised return.** Same objection, plus the additional problem of
annualising a handful of days of synthetic data.

``profit_factor`` and ``win_rate`` are computed but return ``None`` on samples too
small to support them. A win rate from three trades is not a win rate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.db.models import PortfolioSnapshot, VirtualPortfolio, VirtualTrade
from app.domain.enums import TradeOutcome

ZERO = Decimal(0)
MIN_TRADES_FOR_RATES = 5
"""Below this, rates are reported as ``None`` rather than as noise with a decimal
point."""


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    """Everything derivable about a portfolio's record so far."""

    profile_name: str
    currency: str

    starting_capital: Decimal
    ending_equity: Decimal
    net_pnl: Decimal
    return_pct: float

    realized_pnl: Decimal
    unrealized_pnl: Decimal

    trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float | None

    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: float | None
    average_winner: Decimal | None
    average_loser: Decimal | None

    total_fees: Decimal
    total_spread_cost: Decimal
    total_slippage_cost: Decimal
    total_costs: Decimal

    max_drawdown: float
    peak_equity: Decimal
    open_position_count: int
    bars_processed: int
    halted_reason: str | None

    @property
    def cost_drag_pct(self) -> float:
        """Costs as a percentage of starting capital.

        Reported next to the return because the two together answer the only
        question that matters for a small portfolio: was the edge real, or did
        the fees simply eat it?
        """
        if self.starting_capital <= 0:
            return 0.0
        return float(self.total_costs / self.starting_capital) * 100.0


def summarise(
    *,
    profile_name: str,
    portfolio: VirtualPortfolio,
    trades: Sequence[VirtualTrade],
    snapshots: Sequence[PortfolioSnapshot],
    open_position_count: int = 0,
    unrealized_pnl: Decimal = ZERO,
) -> PerformanceSummary:
    """Summarise one portfolio's performance.

    Args:
        portfolio: the ledger row.
        trades: closed trades, any order.
        snapshots: equity curve, for the ending equity. Falls back to
            ``cash + unrealised`` when empty.
        open_position_count: currently open positions.
        unrealized_pnl: mark-to-market P&L on those positions.
    """
    wins = [t for t in trades if t.outcome is TradeOutcome.WIN]
    losses = [t for t in trades if t.outcome is TradeOutcome.LOSS]
    breakevens = [t for t in trades if t.outcome is TradeOutcome.BREAKEVEN]

    gross_profit = sum((t.net_pnl for t in wins), ZERO)
    gross_loss = abs(sum((t.net_pnl for t in losses), ZERO))

    ending_equity = snapshots[-1].equity if snapshots else portfolio.cash + unrealized_pnl
    net_pnl = ending_equity - portfolio.initial_capital
    return_pct = (
        float(net_pnl / portfolio.initial_capital) * 100.0 if portfolio.initial_capital > 0 else 0.0
    )

    total_costs = portfolio.total_fees + portfolio.total_spread_cost + portfolio.total_slippage_cost

    enough = len(trades) >= MIN_TRADES_FOR_RATES
    win_rate = (len(wins) / len(trades)) if enough and trades else None
    profit_factor = float(gross_profit / gross_loss) if enough and gross_loss > 0 else None

    return PerformanceSummary(
        profile_name=profile_name,
        currency=portfolio.currency,
        starting_capital=portfolio.initial_capital,
        ending_equity=ending_equity,
        net_pnl=net_pnl,
        return_pct=return_pct,
        realized_pnl=portfolio.realized_pnl,
        unrealized_pnl=unrealized_pnl,
        trade_count=len(trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=len(breakevens),
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        average_winner=(gross_profit / len(wins)) if wins else None,
        average_loser=(-gross_loss / len(losses)) if losses else None,
        total_fees=portfolio.total_fees,
        total_spread_cost=portfolio.total_spread_cost,
        total_slippage_cost=portfolio.total_slippage_cost,
        total_costs=total_costs,
        max_drawdown=portfolio.max_drawdown,
        peak_equity=portfolio.peak_equity,
        open_position_count=open_position_count,
        bars_processed=portfolio.bars_processed,
        halted_reason=portfolio.halted_reason,
    )
