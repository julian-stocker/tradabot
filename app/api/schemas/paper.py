"""Paper-trading wire schemas.

Read-only. There are no endpoints that place an order or move cash: the simulation
is driven by the CLI and the engine, not by HTTP. Exposing a generic "create
position" endpoint would make the portfolio's history editable from outside the
engine that maintains its invariants.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.protocols import OrderStatus
from app.domain.enums import (
    ExitReason,
    OrderRejectionReason,
    OrderType,
    PositionStatus,
    Side,
    TradeOutcome,
)


class PortfolioResponse(BaseModel):
    """A virtual portfolio's current state."""

    model_config = ConfigDict(extra="forbid")

    simulation_profile_id: int
    profile_name: str
    currency: str
    initial_capital: Decimal
    cash: Decimal
    positions_value: Decimal = Field(
        description="Open positions marked at the BID -- a liquidation estimate, not a mid."
    )
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_position_count: int
    gross_exposure: Decimal
    net_exposure: Decimal
    peak_equity: Decimal
    drawdown: float = Field(description="Fraction below peak equity; <= 0.")
    max_drawdown: float
    total_fees: Decimal
    total_spread_cost: Decimal
    total_slippage_cost: Decimal
    trade_count: int
    bars_processed: int
    halted_reason: str | None = Field(
        default=None, description="Set when a risk limit stopped this portfolio trading."
    )


class PositionResponse(BaseModel):
    """One virtual position, open or closed."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    instrument_id: int
    originating_signal_id: int | None
    originating_trade_decision_id: int | None
    side: Side
    status: PositionStatus
    quantity: Decimal
    average_entry_price: Decimal
    entry_timestamp: datetime
    current_mark_price: Decimal | None
    unrealized_pnl: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    entry_costs: Decimal
    exit_costs: Decimal
    realized_pnl: Decimal
    exit_price: Decimal | None
    exit_timestamp: datetime | None
    exit_reason: ExitReason | None
    exit_was_gap: bool
    exit_was_ambiguous: bool = Field(
        description="True when stop and target were both touched in the exit bar."
    )


class OrderResponse(BaseModel):
    """One virtual order, filled or rejected."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    instrument_id: int
    position_id: int | None
    trade_decision_id: int | None
    side: Side
    order_type: OrderType
    status: OrderStatus
    quantity: Decimal
    requested_at: datetime
    filled_at: datetime | None
    requested_price: Decimal | None = Field(description="Reference mid, not the fill.")
    touch_price: Decimal | None = Field(description="Ask (buy) or bid (sell), before slippage.")
    executed_price: Decimal | None = Field(description="Effective fill after spread and slippage.")
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    rejection_reason: OrderRejectionReason | None
    rejection_detail: str
    used_live_quote: bool


class TradeResponse(BaseModel):
    """A completed round trip."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    instrument_id: int
    originating_signal_id: int | None
    side: Side
    quantity: Decimal
    entry_timestamp: datetime
    entry_price: Decimal
    exit_timestamp: datetime
    exit_price: Decimal
    exit_reason: ExitReason
    holding_bars: int
    gross_pnl: Decimal = Field(description="Mid-to-mid move, before costs.")
    total_fees: Decimal
    total_spread_cost: Decimal
    total_slippage_cost: Decimal
    net_pnl: Decimal = Field(description="After every cost. The only figure that means anything.")
    net_return: float
    max_favorable_excursion: Decimal | None
    max_adverse_excursion: Decimal | None
    outcome: TradeOutcome = Field(description="Classified on NET P&L, not gross.")


class PerformanceResponse(BaseModel):
    """Derived performance for one portfolio.

    ``win_rate`` and ``profit_factor`` are null below a minimum trade count: a win
    rate from three trades is noise with a decimal point.

    No Sharpe ratio -- it needs a regularly spaced return series, which
    event-driven snapshots are not. See docs/paper-trading.md.
    """

    model_config = ConfigDict(extra="forbid")

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
    profit_factor: float | None
    average_winner: Decimal | None
    average_loser: Decimal | None

    total_fees: Decimal
    total_spread_cost: Decimal
    total_slippage_cost: Decimal
    total_costs: Decimal
    cost_drag_pct: float = Field(description="Total costs as a percentage of starting capital.")

    max_drawdown: float
    peak_equity: Decimal
    open_position_count: int
    bars_processed: int
    halted_reason: str | None


class OverviewRow(BaseModel):
    """One portfolio's headline numbers."""

    model_config = ConfigDict(extra="forbid")

    profile_name: str
    currency: str
    initial_capital: Decimal
    equity: Decimal
    return_pct: float
    realized_pnl: Decimal
    open_position_count: int
    trade_count: int
    total_costs: Decimal
    max_drawdown: float
    halted_reason: str | None


class OverviewResponse(BaseModel):
    """Every portfolio side by side.

    The disclaimer is part of the payload, not just the docs: this endpoint is the
    one most likely to be read as a performance report, and on synthetic data it
    is not one.
    """

    model_config = ConfigDict(extra="forbid")

    count: int
    portfolios: list[OverviewRow]
    disclaimer: str = Field(
        default=(
            "Synthetic data validates execution and accounting mechanics only. It "
            "says nothing about trading profitability, signal quality or predictive "
            "power. Do not read these returns as evidence the strategy works."
        )
    )
