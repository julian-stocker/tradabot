"""Backtesting data structures.

**No engine is implemented in phase 1** -- deliberately. A backtester that is
wrong is worse than no backtester, because it manufactures confidence. These
types exist so the engine can be built later against a settled vocabulary, and so
the constraints in docs/backtesting.md have something concrete to attach to.

Every monetary field is :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.costs.models import CostBreakdown
from app.domain.enums import ExitReason, Side, Timeframe


class Order(BaseModel):
    """An intent to trade, before it is filled.

    Orders and fills are separate types on purpose. Collapsing them is the
    structural mistake behind most unrealistic backtests: it lets code assume the
    intended price *is* the achieved price, which quietly deletes slippage,
    spread and partial fills from the results.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    created_at: datetime = Field(description="Bar timestamp at which the order was decided.")
    limit_price: Decimal | None = Field(default=None, gt=0)
    reason: str = Field(default="", description="Signal or rule that produced the order.")


class Fill(BaseModel):
    """An executed order.

    ``price`` is the *effective* fill price including spread and slippage; it is
    not the mid, and not the signal bar's close.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0, description="Effective fill price after spread and slippage.")
    timestamp: datetime = Field(
        description=(
            "When the fill occurred. Must be strictly after the signal bar's close "
            "-- see docs/backtesting.md on execution lag."
        )
    )
    fee: Decimal = Field(default=Decimal(0), ge=0)
    mid_at_fill: Decimal | None = Field(
        default=None, gt=0, description="Reference mid, for measuring realised slippage."
    )


class Position(BaseModel):
    """An open position."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0, description="Effective entry price after costs.")
    entry_time: datetime
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        """Mark-to-market P&L, ignoring the cost of closing.

        Exit costs are excluded because they are not yet incurred. Equity curves
        built on this number are therefore optimistic by roughly one round trip's
        exit cost -- a real engine must account for that when reporting drawdown.
        """
        direction = Decimal(1) if self.side is Side.LONG else Decimal(-1)
        return (mark_price - self.entry_price) * self.quantity * direction


class Trade(BaseModel):
    """A completed round trip, with full cost accounting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)

    entry_time: datetime
    entry_price: Decimal = Field(gt=0)
    exit_time: datetime
    exit_price: Decimal = Field(gt=0)
    exit_reason: ExitReason

    costs: CostBreakdown
    gross_pnl: Decimal = Field(description="P&L on mid prices, before costs.")
    net_pnl: Decimal = Field(description="P&L after spread, fees and slippage.")

    @property
    def holding_period(self) -> float:
        """Holding period in days."""
        return (self.exit_time - self.entry_time).total_seconds() / 86_400.0

    @property
    def net_return(self) -> Decimal:
        """Net return as a fraction of the entry notional."""
        notional = self.entry_price * self.quantity
        return self.net_pnl / notional if notional else Decimal(0)


class EquityPoint(BaseModel):
    """One point on the equity curve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    equity: Decimal
    cash: Decimal
    drawdown: Decimal = Field(le=0, description="Fraction below the running peak; <= 0.")


class BacktestMetrics(BaseModel):
    """Summary statistics of a backtest.

    Both gross and net figures are reported side by side, always. Reporting only
    gross is how a strategy that loses money looks profitable; reporting only net
    hides how much of the edge the costs ate.

    ``benchmark_return`` is mandatory rather than optional: a 12% return means
    nothing until you know the index returned 25% over the same window.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: datetime
    end: datetime
    timeframe: Timeframe

    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)

    gross_return: Decimal
    net_return: Decimal
    benchmark_return: Decimal = Field(description="Buy-and-hold over the same window.")
    excess_return: Decimal = Field(description="net_return - benchmark_return.")

    total_costs: Decimal = Field(ge=0)
    cost_drag: Decimal = Field(ge=0, description="gross_return - net_return.")

    max_drawdown: Decimal = Field(le=0)
    volatility: Decimal = Field(ge=0, description="Annualised standard deviation of returns.")
    sharpe_ratio: float | None = Field(
        default=None,
        description=(
            "Annualised, net of costs. Null when there are too few trades to be "
            "meaningful -- a Sharpe from 6 trades is noise with a decimal point."
        ),
    )
    win_rate: float | None = Field(default=None, ge=0, le=1)
    profit_factor: float | None = Field(
        default=None, ge=0, description="Gross profit / gross loss; null when there are no losses."
    )
    average_holding_days: float | None = Field(default=None, ge=0)

    @property
    def beat_benchmark(self) -> bool:
        return self.excess_return > 0


class BacktestResult(BaseModel):
    """Everything a backtest produced.

    The trade list and equity curve are retained, not just the summary: a headline
    number cannot be audited, and reproducing a suspicious Sharpe requires the
    individual fills that produced it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: str
    symbols: tuple[str, ...]
    initial_capital: Decimal = Field(gt=0)
    metrics: BacktestMetrics
    trades: tuple[Trade, ...]
    equity_curve: tuple[EquityPoint, ...]
    engine_version: str
    parameters: dict[str, str] = Field(
        default_factory=dict, description="Strategy parameters, for reproducibility."
    )
