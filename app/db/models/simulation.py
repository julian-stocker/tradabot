"""Simulation-profile ORM models.

Three tables rather than one, because **capital size, risk appetite and broker
costs are independent dimensions**:

``broker_cost_profiles``
    What execution costs. Shared by every portfolio at the same broker.

``risk_profiles``
    How much to risk and when to bother. "Conservative" means the same thing on a
    50 EUR portfolio as on a 5000 EUR one.

``simulation_profiles``
    A portfolio: an amount of capital, pointing at one risk profile and one cost
    profile.

Collapsing these into a single table is the obvious first design and the wrong
one. Three capital sizes x three risk appetites would then store nine copies of
the risk parameters, and correcting "conservative risk_per_trade" would mean nine
consistent updates -- the classic update anomaly. Normalised, it is one row.

The database enforces this: risk parameters exist in exactly one place.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import Money

MONEY_PRECISION = 18
MONEY_SCALE = 6
RATE_PRECISION = 9
RATE_SCALE = 6


class BrokerCostProfile(Base, TimestampMixin):
    """Named broker cost assumptions.

    Mirrors :class:`~app.core.config.CostSettings` field for field, so a stored
    profile converts to the in-memory settings the phase 1 cost calculator
    already consumes. That reuse is the point: no second cost model.
    """

    __tablename__ = "broker_cost_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    order_fee: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Fixed fee per order. The reason small positions are often uneconomic.",
    )
    variable_fee_rate: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Commission as a fraction of notional (0.0005 == 5 bps).",
    )
    slippage_spread_multiple: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Assumed adverse slippage per side, as a multiple of the half-spread.",
    )
    default_spread_bps: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Fallback spread when no quote is available.",
    )
    min_order_notional: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE),
        nullable=False,
        doc="Smallest order the broker accepts; 0 disables the check.",
    )

    __table_args__ = (
        CheckConstraint("order_fee >= 0", name="order_fee_non_negative"),
        CheckConstraint(
            "variable_fee_rate >= 0 AND variable_fee_rate <= 1", name="variable_fee_rate_fraction"
        ),
        CheckConstraint("slippage_spread_multiple >= 0", name="slippage_non_negative"),
        CheckConstraint("default_spread_bps >= 0", name="default_spread_non_negative"),
        CheckConstraint("min_order_notional >= 0", name="min_order_notional_non_negative"),
    )

    def __repr__(self) -> str:
        return f"<BrokerCostProfile {self.name}>"


class RiskProfile(Base, TimestampMixin):
    """Named risk appetite, independent of capital.

    Every limit is expressed as a **fraction of equity**, never an absolute
    amount. That is what makes a risk profile reusable across portfolio sizes: a
    2% risk-per-trade means 1 EUR on a 50 EUR account and 100 EUR on a 5000 EUR
    one, with no duplicated configuration.

    The one deliberately absolute-ish knob is :attr:`min_signal_score`, which is a
    property of conviction rather than of money.
    """

    __tablename__ = "risk_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    risk_per_trade: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Fraction of equity risked on one trade (0.01 == 1%).",
    )
    max_position_percent: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Largest single position as a fraction of equity.",
    )
    max_total_exposure: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Largest combined exposure as a fraction of equity. May exceed 1 only with margin.",
    )
    max_open_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, doc="Concurrent position count limit."
    )
    max_daily_loss: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Fraction of equity lost in a day before trading halts.",
    )
    max_drawdown: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Fraction below peak equity before the simulation stops.",
    )
    min_signal_score: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Absolute signal score required to consider a trade (0-100).",
    )
    min_confidence: Mapped[Decimal] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=False,
        doc="Minimum signal confidence (0-1).",
    )
    require_positive_net_edge: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        doc=(
            "Reject candidates whose expected move does not survive costs. "
            "Configurable so the counterfactual value of taking them can itself "
            "be measured -- but the default is on."
        ),
    )
    allow_short: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        doc="Whether bearish signals may open short positions.",
    )

    # --- Execution policy (phase 3) ------------------------------------
    stop_loss_atr_multiple: Mapped[Decimal | None] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=True,
        doc="Stop distance in ATRs below entry. NULL disables ATR-based stops.",
    )
    take_profit_r_multiple: Mapped[Decimal | None] = mapped_column(
        Money(RATE_PRECISION, RATE_SCALE),
        nullable=True,
        doc="Target as a multiple of the risk distance.",
    )
    max_holding_bars: Mapped[int | None] = mapped_column(
        Integer, nullable=True, doc="Bars after which a position is closed."
    )
    require_stop_loss: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        doc="Refuse trades without a usable stop rather than inventing a distance.",
    )
    allow_pyramiding: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        doc="Allow a second position in an instrument already held.",
    )
    max_quote_age_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="900", doc="Staleness threshold for quotes."
    )

    __table_args__ = (
        CheckConstraint(
            "risk_per_trade > 0 AND risk_per_trade <= 1", name="risk_per_trade_fraction"
        ),
        CheckConstraint(
            "max_position_percent > 0 AND max_position_percent <= 1",
            name="max_position_percent_fraction",
        ),
        CheckConstraint("max_total_exposure > 0", name="max_total_exposure_positive"),
        CheckConstraint("max_open_positions >= 1", name="max_open_positions_positive"),
        CheckConstraint(
            "max_daily_loss > 0 AND max_daily_loss <= 1", name="max_daily_loss_fraction"
        ),
        CheckConstraint("max_drawdown > 0 AND max_drawdown <= 1", name="max_drawdown_fraction"),
        CheckConstraint(
            "min_signal_score >= 0 AND min_signal_score <= 100", name="min_signal_score_range"
        ),
        CheckConstraint("min_confidence >= 0 AND min_confidence <= 1", name="min_confidence_range"),
        # Risking more on one trade than the position cap allows is incoherent.
        CheckConstraint("risk_per_trade <= max_position_percent", name="risk_within_position_cap"),
    )

    def __repr__(self) -> str:
        return f"<RiskProfile {self.name}>"


class SimulationProfile(Base, TimestampMixin):
    """One virtual portfolio: capital + risk appetite + cost assumptions.

    The join row. "500 EUR balanced at a flat-fee broker" is a
    :class:`SimulationProfile`; "balanced" is a :class:`RiskProfile` shared with
    the 50 EUR and 5000 EUR portfolios.
    """

    __tablename__ = "simulation_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    initial_capital: Mapped[Decimal] = mapped_column(
        Money(MONEY_PRECISION, MONEY_SCALE), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    risk_profile_id: Mapped[int] = mapped_column(
        ForeignKey("risk_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    broker_cost_profile_id: Mapped[int] = mapped_column(
        ForeignKey("broker_cost_profiles.id", ondelete="RESTRICT"), nullable=False
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        doc="Disabled profiles are skipped by evaluation but keep their history.",
    )

    risk_profile: Mapped[RiskProfile] = relationship(lazy="joined")
    broker_cost_profile: Mapped[BrokerCostProfile] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("initial_capital > 0", name="initial_capital_positive"),
        CheckConstraint("length(currency) = 3", name="currency_iso4217"),
    )

    def __repr__(self) -> str:
        return f"<SimulationProfile {self.name} {self.initial_capital} {self.currency}>"
