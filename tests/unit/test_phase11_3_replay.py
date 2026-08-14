"""The replay harness must not become a second implementation of the engine.

A research module that quietly re-derives sizing, costs or stops will drift from
production and then report confident numbers about a system that does not exist.
These tests pin the two things that keep it honest: it calls the production
functions, and its breach classifier says what it claims to say.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.enums import ExitReason
from app.market_data.risk import K_BAND, assess
from app.market_data.volatility import ExpectedMovement, VolatilityRegime
from app.paper.sizing import ExecutionFractionality
from app.research import phase11_3
from app.research.phase11_3 import (
    BreachClass,
    ClosedTrade,
    ReplayConfig,
    build_profile,
    classify_breach,
)

ENTRY = datetime(2025, 6, 2, 14, 0, tzinfo=UTC)


def trade(
    *,
    net_pnl: str = "-10",
    entry: str = "100",
    exit_price: str = "96",
    band: str | None = "3.7",
    gapped: bool = False,
) -> ClosedTrade:
    return ClosedTrade(
        symbol="TEST",
        entered_at=ENTRY,
        exited_at=ENTRY + timedelta(hours=4),
        quantity=Decimal("10"),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_price),
        net_pnl=Decimal(net_pnl),
        costs=Decimal("2"),
        reason=ExitReason.STOP_LOSS,
        gapped=gapped,
        stop_excess=Decimal("0"),
        risk_band_1d=Decimal(band) if band is not None else None,
        regime="NORMAL_VOL",
        floor_bound=False,
    )


# ---------------------------------------------------------------------------
# One implementation, not two
# ---------------------------------------------------------------------------
def test_the_replay_imports_the_production_calculations() -> None:
    """**The gate.** Sizing, costs, stops and risk must come from production."""
    source = inspect.getsource(phase11_3)
    for required in (
        "from app.paper.sizing import",
        "from app.paper.execution import price_fill",
        "from app.paper.risk_gate import evaluate_entry",
        "from app.paper.exits import",
        "from app.market_data.risk import",
        "from app.market_data.volatility import estimate",
    ):
        assert required in source


def test_the_replay_defines_no_sizing_or_cost_function_of_its_own() -> None:
    """A local `def size_...` here is how a research number stops describing
    the system it claims to describe."""
    names = {
        name
        for name, obj in vars(phase11_3).items()
        if inspect.isfunction(obj) and obj.__module__ == phase11_3.__name__
    }
    for forbidden in ("size_position", "price_fill", "evaluate_entry", "derive_stop_and_target"):
        assert forbidden not in names


def test_the_replay_places_no_orders() -> None:
    source = inspect.getsource(phase11_3).lower()
    for forbidden in ("submit_order", "tradingclient", "place_order", "alpaca"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# The breach classifier
# ---------------------------------------------------------------------------
def test_a_profit_is_never_a_breach() -> None:
    """There is no such thing as under-warning about a gain."""
    assert classify_breach(trade(net_pnl="25")) is BreachClass.WITHIN_EXPECTED_RISK


def test_a_loss_inside_the_band_is_within_expected_risk() -> None:
    assert classify_breach(trade(exit_price="98", band="3.7")) is BreachClass.WITHIN_EXPECTED_RISK


def test_a_loss_beyond_the_band_with_the_stop_holding_is_a_normal_exceedance() -> None:
    assert classify_breach(trade(exit_price="95", band="3.7")) is BreachClass.NORMAL_EXCEEDANCE


def test_the_same_loss_reached_by_a_gap_is_classified_as_a_gap_exceedance() -> None:
    """The model quantifies gap risk; it must be visible as its own category."""
    assert (
        classify_breach(trade(exit_price="95", band="3.7", gapped=True))
        is BreachClass.GAP_EXCEEDANCE
    )


def test_a_loss_beyond_twice_the_band_is_extreme_however_it_happened() -> None:
    """Beyond 2x the band the gap/no-gap distinction stops being the story."""
    for gapped in (False, True):
        assert (
            classify_breach(trade(exit_price="90", band="3.7", gapped=gapped))
            is BreachClass.EXTREME_EXCEEDANCE
        )


def test_a_trade_with_no_risk_estimate_is_not_counted_as_a_breach() -> None:
    """The model cannot under-warn on a trade it never assessed."""
    assert classify_breach(trade(exit_price="80", band=None)) is BreachClass.WITHIN_EXPECTED_RISK


# ---------------------------------------------------------------------------
# The structural finding the replay exists to surface
# ---------------------------------------------------------------------------
def test_the_noise_floor_dominates_a_two_atr_stop_in_every_regime() -> None:
    """Phase 11.3's headline: with the layer on, the ATR multiple is inert.

    ``minimum_noise_distance`` is the full 1-day band, and every ``K_BAND``
    constant exceeds 2.0 -- so a ``2.0 x ATR`` structural stop is widened on
    every trade, in every regime. Pinned so that a future change to either the
    constants or the floor definition surfaces here instead of silently making
    the profile's configured stop meaningful again.
    """
    for regime in VolatilityRegime:
        risk = assess(
            ExpectedMovement(
                symbol="TEST",
                calculated_at=ENTRY,
                bar_timestamp=ENTRY,
                regime=regime,
                percentile=0.5,
                atr_pct=1.0,
                recent_range_pct=2.0,
            ),
            now=ENTRY,
        )
        assert risk.minimum_noise_distance(1) > 2.0
        assert K_BAND[regime] > 2.0


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------
def test_a_replay_profile_never_collides_with_a_stored_profile() -> None:
    """The stored profiles are the live paper book. A replay must not touch them."""
    profile = build_profile(
        ReplayConfig(
            capital=Decimal("1000"),
            risk_per_trade=Decimal("0.01"),
            fractionality=ExecutionFractionality.FRACTIONAL_ALLOWED,
            risk_layer_enabled=True,
        )
    )
    assert profile.id == 0
    assert "paper-1000" in profile.name
    assert profile.initial_capital == Decimal("1000")


def test_the_label_distinguishes_every_axis_of_the_grid() -> None:
    """48 cells sharing a label would silently overwrite each other."""
    labels = {
        ReplayConfig(cap, budget, mode, layer).label
        for cap in phase11_3.REPLAY_CAPITALS
        for budget in phase11_3.RISK_BUDGETS
        for mode in ExecutionFractionality
        for layer in (False, True)
    }
    expected = (
        len(phase11_3.REPLAY_CAPITALS)
        * len(phase11_3.RISK_BUDGETS)
        * len(ExecutionFractionality)
        * 2
    )
    assert len(labels) == expected
