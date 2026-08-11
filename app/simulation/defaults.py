"""Default profile catalogue.

Seed data, not hardcoded behaviour. Nothing in the engine reads these constants;
they exist so ``tradabot seed-profiles`` produces a useful starting set, and every
value can be edited in the database or replaced wholesale.

The catalogue is the cartesian product of three capital sizes and three risk
appetites -- **nine portfolios sharing three risk rows**. That is the
normalisation the design requires: changing "conservative" changes it everywhere,
in one place.

    50 EUR   x  {conservative, balanced, aggressive}
    500 EUR  x  {conservative, balanced, aggressive}
    5000 EUR x  {conservative, balanced, aggressive}

The capital sizes are illustrative. 50 EUR is included deliberately because it is
the case where a flat per-order fee dominates everything else -- a portfolio that
mostly *should* decline trades, and a useful control for whether the cost gate
works.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig

DEFAULT_CURRENCY: Final = "EUR"

# --------------------------------------------------------------------------
# Broker cost profiles
# --------------------------------------------------------------------------
# Illustrative values. They are NOT measured against any named broker, and are
# not claimed to represent one -- calibration against real fills is phase 5.
FLAT_FEE_BROKER: Final = BrokerCostConfig(
    name="flat-fee-retail",
    description="Illustrative flat per-order fee, no percentage commission. Not calibrated.",
    order_fee=Decimal("1.00"),
    variable_fee_rate=Decimal("0"),
    slippage_spread_multiple=Decimal("0.5"),
    default_spread_bps=Decimal("10"),
    min_order_notional=Decimal("0"),
)

PERCENTAGE_BROKER: Final = BrokerCostConfig(
    name="percentage-retail",
    description="Illustrative percentage commission with a small minimum. Not calibrated.",
    order_fee=Decimal("0.25"),
    variable_fee_rate=Decimal("0.0010"),
    slippage_spread_multiple=Decimal("0.5"),
    default_spread_bps=Decimal("10"),
    min_order_notional=Decimal("25"),
)

ZERO_COST_BROKER: Final = BrokerCostConfig(
    name="zero-cost-reference",
    description=(
        "No fees, no slippage. Not a real broker -- a control that isolates how "
        "much of a strategy's result is destroyed by costs alone."
    ),
    order_fee=Decimal("0"),
    variable_fee_rate=Decimal("0"),
    slippage_spread_multiple=Decimal("0"),
    default_spread_bps=Decimal("10"),
    min_order_notional=Decimal("0"),
)

DEFAULT_COST_PROFILES: Final[tuple[BrokerCostConfig, ...]] = (
    FLAT_FEE_BROKER,
    PERCENTAGE_BROKER,
    ZERO_COST_BROKER,
)

# --------------------------------------------------------------------------
# Risk profiles
#
# **These are engineering defaults, not validated trading recommendations.**
# They exist so a fresh install has something coherent to run and so the three
# appetites are visibly different from one another. No value here has been
# backtested, optimised, or measured against any outcome. Treat every number as
# a placeholder awaiting the evidence phase 5 will produce.
#
# The ATR multiples and R multiples are the newest and least justified: a 2 ATR
# stop is a convention, not a finding.
# --------------------------------------------------------------------------
CONSERVATIVE: Final = RiskConfig(
    name="conservative",
    description="Small positions, high conviction required, no shorts.",
    risk_per_trade=Decimal("0.005"),
    max_position_percent=Decimal("0.20"),
    max_total_exposure=Decimal("0.50"),
    max_open_positions=3,
    max_daily_loss=Decimal("0.02"),
    max_drawdown=Decimal("0.10"),
    min_signal_score=Decimal("75"),
    min_confidence=Decimal("0.60"),
    require_positive_net_edge=True,
    allow_short=False,
    stop_loss_atr_multiple=Decimal("2.0"),
    take_profit_r_multiple=Decimal("2.0"),
    max_holding_bars=10,
    require_stop_loss=True,
    allow_pyramiding=False,
)

BALANCED: Final = RiskConfig(
    name="balanced",
    description="Moderate sizing and a moderate conviction threshold.",
    risk_per_trade=Decimal("0.01"),
    max_position_percent=Decimal("0.30"),
    max_total_exposure=Decimal("1.00"),
    max_open_positions=5,
    max_daily_loss=Decimal("0.04"),
    max_drawdown=Decimal("0.20"),
    min_signal_score=Decimal("65"),
    min_confidence=Decimal("0.45"),
    require_positive_net_edge=True,
    allow_short=False,
    stop_loss_atr_multiple=Decimal("2.0"),
    take_profit_r_multiple=Decimal("2.5"),
    max_holding_bars=15,
    require_stop_loss=True,
    allow_pyramiding=False,
)

AGGRESSIVE: Final = RiskConfig(
    name="aggressive",
    description="Larger positions, lower conviction threshold, more concurrent risk.",
    risk_per_trade=Decimal("0.02"),
    max_position_percent=Decimal("0.40"),
    max_total_exposure=Decimal("1.00"),
    max_open_positions=8,
    max_daily_loss=Decimal("0.08"),
    max_drawdown=Decimal("0.35"),
    min_signal_score=Decimal("55"),
    min_confidence=Decimal("0.30"),
    require_positive_net_edge=True,
    allow_short=False,
    stop_loss_atr_multiple=Decimal("2.5"),
    take_profit_r_multiple=Decimal("3.0"),
    max_holding_bars=20,
    require_stop_loss=True,
    allow_pyramiding=False,
)

DEFAULT_RISK_PROFILES: Final[tuple[RiskConfig, ...]] = (CONSERVATIVE, BALANCED, AGGRESSIVE)

# --------------------------------------------------------------------------
# Portfolios
# --------------------------------------------------------------------------
DEFAULT_CAPITAL_SIZES: Final[tuple[Decimal, ...]] = (
    Decimal("50"),
    Decimal("500"),
    Decimal("5000"),
)


def build_default_profiles(
    *,
    capital_sizes: tuple[Decimal, ...] = DEFAULT_CAPITAL_SIZES,
    risk_profiles: tuple[RiskConfig, ...] = DEFAULT_RISK_PROFILES,
    costs: BrokerCostConfig = FLAT_FEE_BROKER,
    currency: str = DEFAULT_CURRENCY,
) -> tuple[SimulationProfileConfig, ...]:
    """Every combination of capital size and risk appetite.

    Each argument is overridable, so a caller wanting only 1000 EUR balanced gets
    it without touching this module.
    """
    return tuple(
        SimulationProfileConfig(
            name=f"{capital:.0f}{currency.lower()}-{risk.name}",
            description=f"{capital:.0f} {currency} portfolio, {risk.name} risk profile.",
            initial_capital=capital,
            currency=currency,
            risk=risk,
            costs=costs,
        )
        for capital in capital_sizes
        for risk in risk_profiles
    )
