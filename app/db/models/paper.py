"""Paper-trading ORM models.

**The database is the source of truth, not process memory.** Every mutation --
cash, positions, orders, realised P&L -- is persisted, so an application restart
loses nothing and an open position keeps being monitored (see docs/paper-trading.md
on restart recovery).

Five tables:

``virtual_portfolios``
    Mutable per-profile state: cash, realised P&L, running cost totals, peak
    equity. One row per simulation profile.
``virtual_orders``
    Every order, including rejections. An idempotency key makes replaying a
    signal or a candle a no-op.
``virtual_positions``
    Open and closed positions, with full provenance back to the originating
    signal and decision.
``virtual_trades``
    A closed position's outcome, denormalised for analysis.
``portfolio_snapshots``
    The equity curve: one row per valuation point.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.broker.protocols import OrderStatus
from app.db.base import Base, TimestampMixin
from app.db.types import Money, UTCDateTime
from app.domain.enums import (
    ExitReason,
    OrderRejectionReason,
    OrderType,
    PositionStatus,
    Side,
    TradeOutcome,
)

PRICE_PRECISION = 18
PRICE_SCALE = 6
QTY_PRECISION = 24
QTY_SCALE = 8
MONEY_PRECISION = 18
MONEY_SCALE = 6


class VirtualPortfolio(Base, TimestampMixin):
    """Mutable accounting state for one simulation profile.

    Deliberately *not* derived on the fly from orders and positions. Recomputing
    cash from an event log on every read is both slow and fragile: one missed
    event type and the balance silently drifts. This row is the ledger balance,
    updated inside the same transaction as the events that move it.

    Equity and unrealised P&L are **not** stored here -- they depend on current
    market prices and would be stale the moment they were written. They are
    computed at valuation time (see ``app.paper.portfolio``) and captured in
    :class:`PortfolioSnapshot` when a valuation is worth keeping.
    """

    __tablename__ = "virtual_portfolios"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    cash: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Free cash. Reduced by notional + fee on entry, increased on exit.",
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Cumulative net P&L of closed trades, after all costs.",
    )

    total_fees: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    total_spread_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    total_slippage_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )

    peak_equity: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Highest equity ever observed. The denominator of drawdown.",
    )
    max_drawdown: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        server_default="0",
        doc=(
            "Worst drawdown ever observed, as a non-positive fraction of peak "
            "equity. A dimensionless ratio, so Float rather than Money -- and a "
            "Float compares correctly against a numeric literal on every dialect, "
            "which a text-encoded Money column does not."
        ),
    )

    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    bars_processed: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        doc="Bars this portfolio has seen. Drives bar-counted holding periods.",
    )
    last_valued_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    session_date: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        doc=(
            "Trading session the daily-loss limit is currently measured against. "
            "A session, not a UTC calendar day: a 21:00 UTC fill on a US venue "
            "belongs to that day's session, and resetting at UTC midnight would "
            "split one trading day in two."
        ),
    )
    session_start_equity: Mapped[Decimal | None] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=True,
        doc="Equity at the start of `session_date`; the daily-loss denominator.",
    )
    halted_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        doc="Set when a drawdown or daily-loss limit stopped this portfolio trading.",
    )

    __table_args__ = (
        CheckConstraint("cash >= 0", name="cash_non_negative"),
        CheckConstraint("initial_capital > 0", name="initial_capital_positive"),
        CheckConstraint("peak_equity >= 0", name="peak_equity_non_negative"),
        CheckConstraint("max_drawdown <= 0", name="max_drawdown_non_positive"),
        CheckConstraint("total_fees >= 0", name="total_fees_non_negative"),
        CheckConstraint("trade_count >= 0", name="trade_count_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<VirtualPortfolio profile={self.simulation_profile_id} cash={self.cash}>"


class VirtualOrder(Base, TimestampMixin):
    """One order, filled or refused.

    Rejections are stored as orders rather than discarded. "How often does this
    portfolio ask for something it cannot have, and why" is a property of the
    strategy, and a system that only records successes cannot answer it.
    """

    __tablename__ = "virtual_orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    trade_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_decisions.id", ondelete="SET NULL"),
        nullable=True,
        doc="The decision that caused this order. NULL for exit orders.",
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("virtual_positions.id", ondelete="SET NULL"),
        nullable=True,
        doc="The position this order opened or closed.",
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
        doc=(
            "Deterministic key -- 'entry:<decision_id>' or "
            "'exit:<position_id>:<bar_timestamp>'. Makes replaying a signal or a "
            "candle a no-op rather than a duplicate trade."
        ),
    )

    side: Mapped[Side] = mapped_column(
        Enum(Side, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False, length=20, validate_strings=True), nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(Money(QTY_PRECISION, QTY_SCALE), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    requested_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Reference mid at request time. Not the fill.",
    )
    executed_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Effective fill price, after crossing the spread and slippage.",
    )
    touch_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Ask (buy) or bid (sell) before slippage, for measuring realised slippage.",
    )

    fees: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )
    spread_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )
    slippage_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )

    rejection_reason: Mapped[OrderRejectionReason | None] = mapped_column(
        Enum(OrderRejectionReason, native_enum=False, length=32, validate_strings=True),
        nullable=True,
    )
    rejection_detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    used_live_quote: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        doc="False when the touch was reconstructed from the configured default spread.",
    )

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("fees >= 0", name="fees_non_negative"),
        CheckConstraint(
            "status <> 'FILLED' OR (executed_price IS NOT NULL AND filled_at IS NOT NULL)",
            name="filled_requires_price",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR rejection_reason IS NOT NULL",
            name="rejected_requires_reason",
        ),
        Index("ix_virtual_orders_profile_requested_at", "simulation_profile_id", "requested_at"),
        Index("ix_virtual_orders_position_id", "position_id"),
    )

    def __repr__(self) -> str:
        return f"<VirtualOrder {self.side} {self.quantity} {self.status}>"


class VirtualPosition(Base, TimestampMixin):
    """An open or closed virtual position.

    Carries full provenance -- ``originating_signal_id`` and
    ``originating_trade_decision_id`` -- so every position traces back to the
    evidence that opened it. Without that link a closed trade is an unexplained
    number in a table.

    ``side`` is persisted even though only LONG is implemented, so short support
    is a code change rather than a migration.
    """

    __tablename__ = "virtual_positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    originating_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )
    originating_trade_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("trade_decisions.id", ondelete="SET NULL"), nullable=True
    )

    side: Mapped[Side] = mapped_column(
        Enum(Side, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    status: Mapped[PositionStatus] = mapped_column(
        Enum(PositionStatus, native_enum=False, length=8, validate_strings=True), nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(Money(QTY_PRECISION, QTY_SCALE), nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=False,
        doc="Effective entry price including spread and slippage.",
    )
    entry_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    entry_bar_index: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default="0",
        doc="Portfolio bar counter at entry; holding period is measured against it.",
    )

    current_mark_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="Last mark. Bid when a quote existed -- what the position could realise.",
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )

    stop_loss: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    take_profit: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    maximum_holding_until_bar: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        doc="Bar index at which a time exit triggers. Bars, not calendar days.",
    )
    corporate_actions_applied_through: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        doc=(
            "Splits effective at or before this instant have already been applied "
            "to this position. Makes adjustment idempotent: re-importing a "
            "provider's action history must not halve a holding twice."
        ),
    )

    highest_price_seen: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="For maximum favourable excursion.",
    )
    lowest_price_seen: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=True,
        doc="For maximum adverse excursion.",
    )

    entry_costs: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        server_default="0",
        doc="Entry spread + slippage + fee.",
    )
    entry_fee: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        server_default="0",
        doc=(
            "Entry fee alone. Kept separate because the exact cash outflow is "
            "`entry_price x quantity + fee` -- spread and slippage are already "
            "inside the fill price and must not be subtracted twice."
        ),
    )
    exit_costs: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False, server_default="0"
    )

    exit_price: Mapped[Decimal | None] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=True
    )
    exit_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    exit_reason: Mapped[ExitReason | None] = mapped_column(
        Enum(ExitReason, native_enum=False, length=24, validate_strings=True), nullable=True
    )
    exit_was_gap: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    exit_was_ambiguous: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        doc=(
            "True when stop and target were both touched in the exit bar and the "
            "ambiguity policy decided. Persisted so results resting on an "
            "unresolvable guess can be identified and quantified."
        ),
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("average_entry_price > 0", name="entry_price_positive"),
        CheckConstraint(
            "status <> 'CLOSED' OR "
            "(exit_price IS NOT NULL AND exit_timestamp IS NOT NULL AND exit_reason IS NOT NULL)",
            name="closed_requires_exit",
        ),
        CheckConstraint("stop_loss IS NULL OR stop_loss > 0", name="stop_loss_positive"),
        Index("ix_virtual_positions_profile_status", "simulation_profile_id", "status"),
        Index("ix_virtual_positions_instrument_status", "instrument_id", "status"),
        # At most one OPEN position per (profile, instrument) unless pyramiding is
        # enabled. A partial unique index enforces it in the database rather than
        # relying on a check the engine might skip under concurrency.
        Index(
            "uq_virtual_positions_open_per_instrument",
            "simulation_profile_id",
            "instrument_id",
            unique=True,
            sqlite_where=(status == "OPEN"),
            postgresql_where=(status == "OPEN"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<VirtualPosition {self.side} {self.quantity} @ {self.average_entry_price} "
            f"{self.status}>"
        )


class VirtualTrade(Base, TimestampMixin):
    """A completed round trip, denormalised for analysis.

    Duplicates fields available by joining position and orders, on purpose:
    performance queries scan this table alone, and a closed trade is an immutable
    historical fact that must not change when a profile is edited.
    """

    __tablename__ = "virtual_trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    position_id: Mapped[int] = mapped_column(
        ForeignKey("virtual_positions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )
    originating_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), nullable=True
    )

    side: Mapped[Side] = mapped_column(
        Enum(Side, native_enum=False, length=8, validate_strings=True), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Money(QTY_PRECISION, QTY_SCALE), nullable=False)

    entry_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    exit_timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    exit_reason: Mapped[ExitReason] = mapped_column(
        Enum(ExitReason, native_enum=False, length=24, validate_strings=True), nullable=False
    )
    holding_bars: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    gross_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="(exit - entry) x quantity, before costs.",
    )
    total_fees: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    total_spread_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    total_slippage_cost: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    net_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Gross P&L minus every cost. The only figure that means anything.",
    )
    net_return: Mapped[float] = mapped_column(
        Float, nullable=False, doc="net_pnl as a fraction of entry notional. Dimensionless."
    )

    max_favorable_excursion: Mapped[Decimal | None] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=True,
        doc="Best unrealised gain seen while open. Reveals targets set too far.",
    )
    max_adverse_excursion: Mapped[Decimal | None] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=True,
        doc="Worst unrealised loss seen while open. Reveals stops set too tight.",
    )

    outcome: Mapped[TradeOutcome] = mapped_column(
        Enum(TradeOutcome, native_enum=False, length=12, validate_strings=True),
        nullable=False,
        doc="Classified on NET P&L: a gross winner that paid more in fees is a loss.",
    )

    __table_args__ = (
        Index("ix_virtual_trades_profile_exit", "simulation_profile_id", "exit_timestamp"),
        Index("ix_virtual_trades_outcome", "outcome"),
    )

    def __repr__(self) -> str:
        return f"<VirtualTrade {self.outcome} net={self.net_pnl}>"


class PortfolioSnapshot(Base):
    """One point on a portfolio's equity curve.

    Stored rather than reconstructed: drawdown depends on the *path* of equity,
    and a path cannot be recovered from a list of closed trades once mark-to-market
    movement on open positions is involved.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_profile_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_profiles.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    cash: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    positions_value: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Marked at the bid -- a liquidation estimate, not a mid valuation.",
    )
    equity: Mapped[Decimal] = mapped_column(Money(MONEY_PRECISION, MONEY_SCALE), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    open_position_count: Mapped[int] = mapped_column(Integer, nullable=False)
    gross_exposure: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    drawdown: Mapped[float] = mapped_column(
        Float, nullable=False, doc="Fraction below peak equity; <= 0. Dimensionless."
    )

    __table_args__ = (
        # One snapshot per profile per timestamp: replaying a candle overwrites
        # rather than appending a duplicate point to the equity curve.
        UniqueConstraint(
            "simulation_profile_id", "timestamp", name="simulation_profile_id_timestamp"
        ),
        CheckConstraint("drawdown <= 0", name="drawdown_non_positive"),
        Index("ix_portfolio_snapshots_profile_timestamp", "simulation_profile_id", "timestamp"),
    )

    def __repr__(self) -> str:
        return f"<PortfolioSnapshot profile={self.simulation_profile_id} equity={self.equity}>"


class DecisionOutcome(Base, TimestampMixin):
    """Counterfactual follow-up on a trade decision.

    Records what happened *after* a decision, for both TRADE and SKIP. The SKIP
    case is the point: "would that rejected trade have worked?" is unanswerable
    without measuring it, and a system that only tracks what it did can never
    learn that its cost gate is too strict.

    A separate table rather than columns on ``trade_decisions`` because a decision
    is an immutable record of a moment, and this is a later observation about it.
    Mixing the two would make the decision row mutable and destroy its audit value.

    **This records evidence only.** Nothing reads it back into the scoring rules --
    see docs/simulation-design.md on the feedback constraint.
    """

    __tablename__ = "decision_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_decision_id: Mapped[int] = mapped_column(
        ForeignKey("trade_decisions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False
    )

    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    horizon_end: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, doc="End of the evaluation window."
    )
    bars_evaluated: Mapped[int] = mapped_column(Integer, nullable=False)

    reference_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE),
        nullable=False,
        doc="Price at decision time, the baseline for the forward return.",
    )
    horizon_close: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    forward_return: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Fractional price return over the horizon. GROSS -- no costs applied.",
    )
    max_favorable_excursion: Mapped[float] = mapped_column(
        Float, nullable=False, doc="Best fractional excursion during the horizon."
    )
    max_adverse_excursion: Mapped[float] = mapped_column(
        Float, nullable=False, doc="Worst fractional excursion during the horizon."
    )

    __table_args__ = (
        CheckConstraint("bars_evaluated >= 1", name="bars_evaluated_positive"),
        Index("ix_decision_outcomes_instrument_id", "instrument_id"),
    )

    def __repr__(self) -> str:
        return f"<DecisionOutcome decision={self.trade_decision_id} fwd={self.forward_return}>"
