"""Trading-day horizons, the historical cost model and quote quality.

Three concerns that all fail quietly if they are wrong: a horizon that lands on a
Saturday produces a missing label rather than a wrong one, but a horizon that
lands on the *wrong* Wednesday produces a wrong one; a cost model that uses
today's spread for a February trade produces plausible numbers that are pure
look-ahead; and a spread classifier that calls every wide quote broken deletes
legitimate extended-hours data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.config import CostSettings
from app.domain.enums import CostBasis, Horizon, SpreadQuality
from app.market_data.calendars import get_trading_calendar
from app.research.costs import (
    BASE_SPREAD_BPS,
    COST_MODEL_VERSION,
    MAX_MODELLED_SPREAD_BPS,
    historical_round_trip,
    model_spread_bps,
)
from app.research.horizons import (
    LABEL_POLICY_VERSION,
    SUPPORTED_HORIZONS,
    TRADING_DAY_HORIZONS,
    resolve,
)
from app.research.quality import SUSPICIOUS_SPREAD_BPS, classify_spread
from app.scanner.enums import SessionPhase


@pytest.fixture
def calendar() -> object:
    return get_trading_calendar("XNYS")


# ---------------------------------------------------------------------------
# 36-38. Calendar-aware horizons
# ---------------------------------------------------------------------------
def test_every_required_horizon_is_supported() -> None:
    """Part E's list, exactly."""
    assert [h.value for h in SUPPORTED_HORIZONS] == ["15m", "1h", "4h", "1d", "3d", "5d", "20d"]


def test_day_horizons_are_trading_days_not_calendar_days() -> None:
    assert set(TRADING_DAY_HORIZONS) == {Horizon.D1, Horizon.D3, Horizon.D5, Horizon.D20}


def test_a_friday_one_day_horizon_lands_on_monday(calendar: object) -> None:
    """The weekend test. ``+1 day`` from a Friday is a Saturday with no price."""
    friday = datetime(2024, 6, 7, 15, 0, tzinfo=UTC)

    resolved = resolve(Horizon.D1, reference=friday, calendar=calendar)  # type: ignore[arg-type]

    assert resolved is not None
    assert resolved.target.date() == date(2024, 6, 10), "Monday"


def test_a_five_day_horizon_skips_a_holiday(calendar: object) -> None:
    """Independence Day 2024 falls on a Thursday; five sessions must step over it."""
    before = datetime(2024, 7, 1, 15, 0, tzinfo=UTC)

    resolved = resolve(Horizon.D5, reference=before, calendar=calendar)  # type: ignore[arg-type]

    assert resolved is not None
    # 1 Jul + 5 sessions = 2,3,5,8,9 July (4 July is a holiday).
    assert resolved.target.date() == date(2024, 7, 9)


def test_twenty_trading_days_is_about_a_calendar_month(calendar: object) -> None:
    start = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)

    resolved = resolve(Horizon.D20, reference=start, calendar=calendar)  # type: ignore[arg-type]

    assert resolved is not None
    span = (resolved.target.date() - start.date()).days
    assert 26 <= span <= 32, f"20 sessions spanned {span} calendar days"


def test_an_intraday_horizon_past_the_close_rolls_forward(calendar: object) -> None:
    """A 4-hour horizon from 18:00 UTC ends after the close, where no price exists."""
    late = datetime(2024, 6, 3, 18, 0, tzinfo=UTC)

    resolved = resolve(Horizon.H4, reference=late, calendar=calendar)  # type: ignore[arg-type]

    assert resolved is not None
    assert resolved.rolled_to_next_session
    assert resolved.target > late, "a horizon must never resolve backwards"


def test_an_intraday_horizon_inside_the_session_does_not_roll(calendar: object) -> None:
    midday = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)

    resolved = resolve(Horizon.H1, reference=midday, calendar=calendar)  # type: ignore[arg-type]

    assert resolved is not None
    assert not resolved.rolled_to_next_session
    assert resolved.target == datetime(2024, 6, 3, 16, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 30-35. The historical cost model
# ---------------------------------------------------------------------------
def test_a_modelled_spread_is_never_labelled_observed() -> None:
    """**The claim the whole cost model rests on.**

    This database stores no historical quotes, so no backtested cost was ever
    measured. Presenting one as observed would be the same error as quoting a
    simulated fill as a real one.
    """
    spread = model_spread_bps(price=Decimal(200))

    assert spread.basis is CostBasis.MODELLED
    assert spread.basis is not CostBasis.OBSERVED
    assert spread.model_version == COST_MODEL_VERSION


def test_the_cost_model_is_versioned() -> None:
    assert COST_MODEL_VERSION
    assert LABEL_POLICY_VERSION


def test_a_calm_liquid_name_gets_the_base_spread() -> None:
    spread = model_spread_bps(price=Decimal(300), volatility=0.0, relative_volume=2.0)

    assert spread.spread_bps == pytest.approx(BASE_SPREAD_BPS)


def test_volatility_widens_the_modelled_spread() -> None:
    calm = model_spread_bps(price=Decimal(200), volatility=0.01)
    wild = model_spread_bps(price=Decimal(200), volatility=0.05)

    assert wild.spread_bps > calm.spread_bps


def test_a_low_priced_share_pays_a_tick_penalty() -> None:
    """A one-cent tick is 5 bps on a 20 EUR share and 0.2 bps on a 500 EUR one."""
    cheap = model_spread_bps(price=Decimal(10))
    dear = model_spread_bps(price=Decimal(500))

    assert cheap.spread_bps > dear.spread_bps
    assert "low_price" in cheap.components


def test_thin_participation_widens_the_spread() -> None:
    thin = model_spread_bps(price=Decimal(200), relative_volume=0.25)
    normal = model_spread_bps(price=Decimal(200), relative_volume=1.5)

    assert thin.spread_bps > normal.spread_bps


def test_extended_hours_is_assumed_more_expensive() -> None:
    session = model_spread_bps(price=Decimal(200), session=SessionPhase.REGULAR)
    after = model_spread_bps(price=Decimal(200), session=SessionPhase.AFTER_HOURS)

    assert after.spread_bps > session.spread_bps


def test_a_typical_large_cap_gets_a_single_digit_spread() -> None:
    """**Regression: the units of `volatility_20`.**

    It is annualised and fractional -- 0.22 is 22% a year, which is ordinary. An
    earlier version read it as a per-bar fraction and scaled by 10,000, producing
    ~1,100 bps on every observation. Every estimate then pinned to the cap, and
    the benchmark's entire net-P&L column measured the ceiling rather than the
    market. Nothing failed; the numbers were just quietly meaningless.
    """
    spread = model_spread_bps(price=Decimal(250), volatility=0.22, relative_volume=1.2)

    assert spread.spread_bps < 20.0, f"{spread.spread_bps} bps on a large-cap is not plausible"
    assert spread.spread_bps < MAX_MODELLED_SPREAD_BPS, "the estimate saturated the cap"


def test_a_realistic_universe_never_saturates_the_cap() -> None:
    """Across the plausible range, the cap must be a guard rail -- not the answer."""
    for volatility in (0.10, 0.25, 0.50, 0.80):
        spread = model_spread_bps(price=Decimal(150), volatility=volatility)
        assert spread.spread_bps < MAX_MODELLED_SPREAD_BPS


def test_the_modelled_spread_is_capped() -> None:
    """A volatility spike must not produce an absurd cost."""
    extreme = model_spread_bps(price=Decimal(5), volatility=10.0, relative_volume=0.001)

    assert extreme.spread_bps <= MAX_MODELLED_SPREAD_BPS


def test_missing_inputs_degrade_rather_than_raise() -> None:
    """An absent volatility reading should widen uncertainty, not delete the row."""
    spread = model_spread_bps(price=None, volatility=None, relative_volume=None)

    assert spread.spread_bps == pytest.approx(BASE_SPREAD_BPS)


def test_gross_and_net_reconcile_through_the_itemised_costs() -> None:
    """Part 34: the difference between gross and net *is* the cost breakdown."""
    cost, spread = historical_round_trip(
        entry_mid=Decimal(100),
        exit_mid=Decimal(110),
        quantity=Decimal(10),
        settings=CostSettings(),
    )

    assert cost.gross_pnl > cost.net_pnl, "costs must reduce the result"
    assert cost.gross_pnl - cost.net_pnl == pytest.approx(cost.breakdown.total, abs=Decimal("0.01"))
    assert spread.basis is CostBasis.MODELLED


# ---------------------------------------------------------------------------
# 33. Quote quality
# ---------------------------------------------------------------------------
def test_a_normal_session_spread_is_reliable(calendar: object) -> None:
    assessment = classify_spread(
        spread_bps=4.0,
        observed_at=datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        quote_age_seconds=2.0,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.quality is SpreadQuality.REGULAR_SESSION
    assert assessment.is_reliable


def test_the_after_hours_spread_that_started_this_is_not_called_broken(
    calendar: object,
) -> None:
    """**The phase 4 observation, classified correctly.**

    900 bps on a mega-cap at 21:30 is not a malfunction -- it is an accurate
    report of a nearly empty book. Labelling it SUSPICIOUS would be wrong; the
    session is what makes it uninformative, so that is what gets recorded.
    """
    assessment = classify_spread(
        spread_bps=1118.0,
        observed_at=datetime(2024, 6, 3, 21, 30, tzinfo=UTC),
        quote_age_seconds=5.0,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.quality is SpreadQuality.EXTENDED_HOURS
    assert assessment.quality is not SpreadQuality.SUSPICIOUS_SPREAD
    assert not assessment.is_reliable, "real, but not usable as an executable cost"


def test_an_implausible_session_spread_is_suspicious(calendar: object) -> None:
    assessment = classify_spread(
        spread_bps=SUSPICIOUS_SPREAD_BPS + 500,
        observed_at=datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        quote_age_seconds=1.0,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.quality is SpreadQuality.SUSPICIOUS_SPREAD
    assert not assessment.is_reliable


def test_a_stale_quote_is_stale_before_it_is_judged_wide(calendar: object) -> None:
    """An old quote's width describes the wrong instant, whatever its value."""
    assessment = classify_spread(
        spread_bps=4.0,
        observed_at=datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        quote_age_seconds=600.0,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.quality is SpreadQuality.STALE


def test_no_quote_is_missing_not_zero(calendar: object) -> None:
    """The normal case for every historical observation."""
    assessment = classify_spread(
        spread_bps=None,
        observed_at=datetime(2024, 6, 3, 15, 0, tzinfo=UTC),
        quote_age_seconds=None,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.quality is SpreadQuality.MISSING
    assert assessment.spread_bps is None


def test_the_raw_observation_is_preserved_whatever_the_verdict(calendar: object) -> None:
    """Part K: classify, never delete."""
    assessment = classify_spread(
        spread_bps=980.0,
        observed_at=datetime(2024, 6, 3, 22, 0, tzinfo=UTC),
        quote_age_seconds=3.0,
        calendar=calendar,  # type: ignore[arg-type]
    )

    assert assessment.spread_bps == 980.0
