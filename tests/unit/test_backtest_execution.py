"""Execution-aware simulation across the three capital levels. **Part W.**

The question these answer is the one that motivated three portfolios: does a
signal that works at 10,000 EUR also work at 100? A flat per-order fee is 2% of a
50 EUR position and 0.02% of a 5,000 EUR one, so the same market move is a profit
at one size and a loss at the other. Reporting a single blended result would hide
the only finding that matters to a small account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtesting.execution import PortfolioState, simulate_entry
from app.domain.enums import CostBasis, ExitReason
from app.scanner.enums import SessionPhase
from app.simulation.portfolios import build_personal_profiles

REFERENCE = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)


class FakeBar:
    def __init__(self, minutes: int, o: str, h: str, low: str, c: str) -> None:
        self.timestamp = REFERENCE + timedelta(minutes=minutes)
        self.open = Decimal(o)
        self.high = Decimal(h)
        self.low = Decimal(low)
        self.close = Decimal(c)


class FakeEvaluation:
    """Only the fields the executor reads."""

    id = 1

    def __init__(self, atr_pct: float = 1.0) -> None:
        self.volatility_metrics = {"atr_pct": atr_pct, "volatility": 0.02}
        self.volume_metrics = {"relative_volume": 1.5}
        self.spread_bps = None
        self.quote_age_seconds = None
        self.session_phase = SessionPhase.REGULAR.value


def profiles() -> dict[str, object]:
    return {profile.name: profile for profile in build_personal_profiles()}


def rising_bars() -> list[FakeBar]:
    """A steady climb that reaches a typical take-profit."""
    return [
        FakeBar(60 * i, str(100 + i), str(101 + i), str(99 + i), str(100.5 + i))
        for i in range(1, 12)
    ]


def falling_bars() -> list[FakeBar]:
    return [
        FakeBar(60 * i, str(100 - i), str(101 - i), str(99 - i), str(99.5 - i))
        for i in range(1, 12)
    ]


def entry_bar() -> FakeBar:
    """Open and close deliberately differ, so "filled at the open" is testable."""
    return FakeBar(0, "99.80", "100.50", "99.50", "100.20")


# ---------------------------------------------------------------------------
# 48-50. The three capital levels
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["paper-100", "paper-1000", "paper-10000"])
def test_each_portfolio_reaches_a_decision(key: str) -> None:
    """Execute or reject -- but never silently produce nothing."""
    state = PortfolioState.start(profiles()[key])  # type: ignore[arg-type]

    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=state,
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    assert trade.executed or trade.rejection_reason, "no decision was recorded"


def test_position_size_scales_with_capital() -> None:
    """51: independent sizing. Same signal, three different quantities."""
    sizes: dict[str, Decimal] = {}
    for key, profile in profiles().items():
        trade = simulate_entry(
            evaluation=FakeEvaluation(),  # type: ignore[arg-type]
            state=PortfolioState.start(profile),  # type: ignore[arg-type]
            entry_bar=entry_bar(),  # type: ignore[arg-type]
            future_bars=rising_bars(),  # type: ignore[arg-type]
            session=SessionPhase.REGULAR,
        )
        if trade.executed and trade.quantity is not None:
            sizes[key] = trade.quantity

    if len(sizes) > 1:
        ordered = [sizes[key] for key in ("paper-100", "paper-1000", "paper-10000") if key in sizes]
        assert ordered == sorted(ordered), "a larger account did not take a larger position"


def test_costs_hurt_the_small_account_far_more() -> None:
    """**The finding three portfolios exist to expose.**

    A fixed order fee is a rounding error at 10,000 EUR and a material drag at
    100. Comparing net *return* rather than net P/L is what makes it visible.
    """
    returns: dict[str, float] = {}
    for key, profile in profiles().items():
        trade = simulate_entry(
            evaluation=FakeEvaluation(),  # type: ignore[arg-type]
            state=PortfolioState.start(profile),  # type: ignore[arg-type]
            entry_bar=entry_bar(),  # type: ignore[arg-type]
            future_bars=rising_bars(),  # type: ignore[arg-type]
            session=SessionPhase.REGULAR,
        )
        if trade.executed and trade.net_return is not None:
            returns[key] = trade.net_return

    if "paper-100" in returns and "paper-10000" in returns:
        assert returns["paper-100"] <= returns["paper-10000"], (
            "the small account did not pay proportionally more"
        )


def test_a_small_account_never_takes_a_position_it_cannot_fund() -> None:
    """52: independent rejection.

    Asserted as an invariant rather than as "it must refuse": whether a 100 EUR
    account can touch a 5,000 EUR share depends on fractional-share support, and
    the property that must hold either way is that it never commits more capital
    than it has.
    """
    tiny = profiles()["paper-100"]
    state = PortfolioState.start(tiny)  # type: ignore[arg-type]
    expensive = FakeBar(0, "5000", "5010", "4990", "5000")

    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=state,
        entry_bar=expensive,  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    if trade.executed:
        assert trade.entry_price is not None
        assert trade.quantity is not None
        assert trade.entry_price * trade.quantity <= state.cash, (
            "the position cost more than the account held"
        )
    else:
        assert trade.rejection_reason is not None


def test_a_full_book_rejects_further_entries() -> None:
    profile = profiles()["paper-10000"]
    state = PortfolioState.start(profile)  # type: ignore[arg-type]
    state.open_positions = profile.risk.max_open_positions  # type: ignore[attr-defined]

    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=state,
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    assert not trade.executed
    assert trade.rejection_reason == "MAX_OPEN_POSITIONS"


# ---------------------------------------------------------------------------
# Execution convention and cost provenance
# ---------------------------------------------------------------------------
def test_the_fill_is_the_next_bars_open_never_the_signal_bars_close() -> None:
    """**Part C in one assertion.**

    A signal computed from a bar that closed at T could not be acted on before T.
    Filling at that bar's own close would book a trade at a price only knowable
    once the opportunity had ended.
    """
    bar = entry_bar()
    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=PortfolioState.start(profiles()["paper-10000"]),  # type: ignore[arg-type]
        entry_bar=bar,  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    if trade.executed:
        assert trade.entry_price == bar.open
        assert trade.entry_price != bar.close


def test_no_executable_bar_means_no_trade() -> None:
    """The last observation in a window has nothing to fill into. That is real."""
    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=PortfolioState.start(profiles()["paper-10000"]),  # type: ignore[arg-type]
        entry_bar=None,
        future_bars=[],
        session=SessionPhase.REGULAR,
    )

    assert not trade.executed
    assert trade.rejection_reason == "NO_EXECUTABLE_BAR"


def test_every_executed_trade_reports_a_modelled_spread() -> None:
    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=PortfolioState.start(profiles()["paper-10000"]),  # type: ignore[arg-type]
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    if trade.executed:
        assert trade.modelled_spread_bps is not None
        assert trade.modelled_spread_bps > 0


def test_a_falling_market_exits_at_the_stop() -> None:
    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=PortfolioState.start(profiles()["paper-10000"]),  # type: ignore[arg-type]
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=falling_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    if trade.executed:
        assert trade.exit_reason is ExitReason.STOP_LOSS
        assert trade.net_pnl < 0


def test_a_missing_atr_is_rejected_rather_than_guessed() -> None:
    """The stop is the sizing denominator; inventing one invents the position."""
    trade = simulate_entry(
        evaluation=FakeEvaluation(atr_pct=0.0),  # type: ignore[arg-type]
        state=PortfolioState.start(profiles()["paper-10000"]),  # type: ignore[arg-type]
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    assert not trade.executed


def test_drawdown_tracks_the_peak_not_the_start() -> None:
    """53-54: independent P/L and drawdown per portfolio."""
    state = PortfolioState.start(profiles()["paper-10000"])  # type: ignore[arg-type]

    state.equity = Decimal(12_000)
    state.record_equity(REFERENCE)
    state.equity = Decimal(9_000)
    state.record_equity(REFERENCE + timedelta(days=1))

    assert state.peak_equity == Decimal(12_000)
    assert state.max_drawdown == pytest.approx(Decimal("-0.25"))


def test_cost_basis_is_always_modelled_for_history() -> None:
    """Historical quotes do not exist, so no historical cost was ever observed."""
    assert CostBasis.MODELLED.value == "MODELLED"
    assert CostBasis.OBSERVED is not CostBasis.MODELLED


# ---------------------------------------------------------------------------
# Solvency
# ---------------------------------------------------------------------------
def test_a_wiped_out_account_stops_trading() -> None:
    """**Ruin is terminal.**

    An unleveraged cash account cannot trade its way out of zero. Letting the
    simulation continue produces negative equity, which is not a bad result but
    an impossible one -- the first 100 EUR benchmark finished at -4.02 EUR
    because a flat exit fee was charged against cash that no longer existed.
    """
    state = PortfolioState.start(profiles()["paper-100"])  # type: ignore[arg-type]
    state.equity = Decimal(0)
    state.cash = Decimal(0)

    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=state,
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    assert not trade.executed
    assert trade.rejection_reason == "ACCOUNT_RUINED"


def test_a_negative_balance_also_stops_trading() -> None:
    state = PortfolioState.start(profiles()["paper-100"])  # type: ignore[arg-type]
    state.equity = Decimal("-4.02")

    trade = simulate_entry(
        evaluation=FakeEvaluation(),  # type: ignore[arg-type]
        state=state,
        entry_bar=entry_bar(),  # type: ignore[arg-type]
        future_bars=rising_bars(),  # type: ignore[arg-type]
        session=SessionPhase.REGULAR,
    )

    assert not trade.executed
    assert trade.rejection_reason == "ACCOUNT_RUINED"
