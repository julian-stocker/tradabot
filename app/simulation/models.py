"""Simulation-profile domain models.

Validated, immutable views of the three profile tables. The ORM rows are storage;
these carry the invariants and the behaviour.

The central separation, stated once:

    **Capital size is not a risk profile.**

A 50 EUR portfolio and a 5000 EUR portfolio running "balanced" share one
:class:`RiskConfig`. What differs between them is :attr:`SimulationProfileConfig.
initial_capital` -- and, consequentially, how badly a fixed per-order fee hurts.
That consequence is computed, never configured.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import CostSettings


class BrokerCostConfig(BaseModel):
    """Named broker cost assumptions.

    Converts to :class:`~app.core.config.CostSettings` so the phase 1 cost
    calculator is reused unchanged -- there is exactly one cost model in the
    system, and this is a stored parameterisation of it, not a second one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | None = None
    name: str = Field(min_length=1, max_length=64)
    description: str = ""

    order_fee: Decimal = Field(ge=0)
    variable_fee_rate: Decimal = Field(ge=0, le=1)
    slippage_spread_multiple: Decimal = Field(ge=0)
    default_spread_bps: Decimal = Field(ge=0)
    min_order_notional: Decimal = Field(ge=0)

    def to_cost_settings(self) -> CostSettings:
        """Adapt to the settings object the cost calculator consumes."""
        return CostSettings(
            order_fee=self.order_fee,
            variable_fee_rate=self.variable_fee_rate,
            slippage_spread_multiple=self.slippage_spread_multiple,
            default_spread_bps=float(self.default_spread_bps),
            min_order_notional=self.min_order_notional,
        )


class RiskConfig(BaseModel):
    """Named risk appetite, expressed entirely in fractions of equity.

    Every limit scales with the portfolio, which is what lets one row serve every
    capital size. An absolute limit here (``max_position_eur = 250``) would
    silently mean "aggressive" on a 500 EUR account and "unusable" on a 50 EUR
    one, and would force a duplicate row per size.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | None = None
    name: str = Field(min_length=1, max_length=64)
    description: str = ""

    risk_per_trade: Decimal = Field(gt=0, le=1, description="Fraction of equity risked per trade.")
    max_position_percent: Decimal = Field(gt=0, le=1)
    max_total_exposure: Decimal = Field(gt=0)
    max_open_positions: int = Field(ge=1)
    max_daily_loss: Decimal = Field(gt=0, le=1)
    max_drawdown: Decimal = Field(gt=0, le=1)
    min_signal_score: Decimal = Field(ge=0, le=100)
    min_confidence: Decimal = Field(ge=0, le=1)
    require_positive_net_edge: bool = True
    allow_short: bool = False

    # --- Execution policy (phase 3) ------------------------------------
    stop_loss_atr_multiple: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "Stop placed this many ATRs below entry. Scales with the instrument's "
            "own volatility, so one setting is sane on a quiet and a wild name alike."
        ),
    )
    take_profit_r_multiple: Decimal | None = Field(
        default=None,
        gt=0,
        description=(
            "Target as a multiple of the risk distance (R). Expressing it in R "
            "makes the reward:risk ratio a configured number rather than an "
            "accident of two unrelated price settings."
        ),
    )
    max_holding_bars: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Close after this many bars. Counted in bars, not calendar days -- "
            "see app.paper.exits.holding_period_expired."
        ),
    )
    require_stop_loss: bool = Field(
        default=True,
        description=(
            "Refuse a trade with no usable stop rather than sizing it by notional. "
            "A stop is the sizing denominator; inventing one produces an arbitrary "
            "position size that looks principled."
        ),
    )
    allow_pyramiding: bool = Field(
        default=False,
        description="Whether a second position may be opened in an instrument already held.",
    )
    max_quote_age_seconds: int = Field(
        default=900,
        ge=0,
        description=(
            "Quotes older than this are refused as stale. Trading on a stale quote "
            "means the modelled spread bears no relation to the real one."
        ),
    )

    @model_validator(mode="after")
    def _risk_within_position_cap(self) -> RiskConfig:
        if self.risk_per_trade > self.max_position_percent:
            msg = (
                f"risk profile {self.name!r}: risk_per_trade ({self.risk_per_trade}) "
                f"exceeds max_position_percent ({self.max_position_percent}); "
                f"a trade cannot risk more than the position is allowed to be"
            )
            raise ValueError(msg)
        return self


class SimulationProfileConfig(BaseModel):
    """One virtual portfolio: capital, plus a risk and a cost configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int | None = None
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    initial_capital: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    enabled: bool = True

    owner_id: int | None = Field(
        default=None, description="Owning TradabotUser. None on a legacy profile."
    )
    notification_channel: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Notification routing key, e.g. 'paper-100'. Persistent portfolio "
            "identity: notification routing looks this up rather than inspecting "
            "message content. None means no portfolio-channel messages."
        ),
    )

    risk: RiskConfig
    costs: BrokerCostConfig

    @property
    def max_position_notional(self) -> Decimal:
        """Largest position this portfolio may hold, in currency.

        Where capital size and risk appetite finally meet. The same "balanced"
        20% cap is 10 EUR here and 1000 EUR there, and the fixed order fee is
        unmoved by either -- which is the whole reason small portfolios reject
        trades large ones accept.
        """
        return self.initial_capital * self.risk.max_position_percent

    @property
    def risk_budget(self) -> Decimal:
        """Currency amount at risk on a single trade."""
        return self.initial_capital * self.risk.risk_per_trade

    def describe(self) -> str:
        return (
            f"{self.name} ({self.initial_capital:.0f} {self.currency}, "
            f"risk={self.risk.name}, costs={self.costs.name})"
        )
