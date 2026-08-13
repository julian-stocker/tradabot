"""Volatility regime transitions in #market-trends.

A regime is a *condition*, not an event, so the rules that keep the trends
channel readable do not transfer: a four-hour cooldown would re-announce the same
elevated state six times a day, and one message per elevated symbol would produce
thirty-four posts on the days that matter most.

These tests pin the state machine and the flood control, and one of them pins the
thing most likely to be got wrong under pressure -- that an escalation is not
swallowed by a timer meant for something else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.market_data.volatility import (
    MAX_BAR_AGE,
    ExpectedMovement,
    VolatilityRegime,
)
from app.notifications.volatility_events import (
    TOP_N,
    VolatilityTransition,
    assert_no_recommendation_language,
    build_section,
    classify_transition,
    detect_events,
    next_state,
)

NOW = datetime(2026, 8, 13, 15, 30, tzinfo=UTC)


def movement(
    symbol: str = "NVDA",
    regime: VolatilityRegime = VolatilityRegime.HIGH,
    *,
    percentile: float = 0.80,
    bar_age: timedelta = timedelta(minutes=10),
) -> ExpectedMovement:
    return ExpectedMovement(
        symbol=symbol,
        calculated_at=NOW,
        bar_timestamp=NOW - bar_age,
        regime=regime,
        percentile=percentile,
        atr_pct=1.2,
        recent_range_pct=2.5,
    )


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (None, VolatilityRegime.HIGH, VolatilityTransition.ELEVATED),
        (VolatilityRegime.NORMAL, VolatilityRegime.HIGH, VolatilityTransition.ELEVATED),
        (VolatilityRegime.LOW, VolatilityRegime.EXTREME, VolatilityTransition.ELEVATED),
        (VolatilityRegime.HIGH, VolatilityRegime.HIGH, VolatilityTransition.UNCHANGED),
        (VolatilityRegime.HIGH, VolatilityRegime.EXTREME, VolatilityTransition.ESCALATED),
        (VolatilityRegime.EXTREME, VolatilityRegime.EXTREME, VolatilityTransition.UNCHANGED),
        (VolatilityRegime.EXTREME, VolatilityRegime.HIGH, VolatilityTransition.UNCHANGED),
        (VolatilityRegime.EXTREME, VolatilityRegime.NORMAL, VolatilityTransition.NORMALISED),
        (VolatilityRegime.NORMAL, VolatilityRegime.LOW, VolatilityTransition.UNCHANGED),
        (None, VolatilityRegime.NORMAL, VolatilityTransition.UNCHANGED),
    ],
)
def test_transition_table(
    previous: VolatilityRegime | None, current: VolatilityRegime, expected: str
) -> None:
    assert classify_transition(current, previous) == expected


def test_a_persisting_elevated_state_is_silent() -> None:
    """**The reason a cooldown is the wrong tool here.**

    A regime lasts days. Time-based dedup would re-announce it every four hours.
    """
    events = detect_events(
        [movement(regime=VolatilityRegime.HIGH)],
        {"NVDA": VolatilityRegime.HIGH},
        now=NOW,
    )

    assert events == []


def test_an_escalation_publishes_immediately() -> None:
    """A stronger regime must not be hidden by dedup meant for repeats."""
    events = detect_events(
        [movement(regime=VolatilityRegime.EXTREME, percentile=0.95)],
        {"NVDA": VolatilityRegime.HIGH},
        now=NOW,
    )

    assert len(events) == 1
    assert events[0].transition == VolatilityTransition.ESCALATED


def test_normal_to_high_publishes() -> None:
    events = detect_events(
        [movement(regime=VolatilityRegime.HIGH)], {"NVDA": VolatilityRegime.NORMAL}, now=NOW
    )

    assert len(events) == 1
    assert events[0].transition == VolatilityTransition.ELEVATED


def test_calming_down_reports_a_normalisation() -> None:
    events = detect_events(
        [movement(regime=VolatilityRegime.NORMAL, percentile=0.4)],
        {"NVDA": VolatilityRegime.EXTREME},
        now=NOW,
    )

    assert len(events) == 1
    assert events[0].transition == VolatilityTransition.NORMALISED
    assert "normalised" in events[0].headline


def test_a_quiet_universe_produces_nothing() -> None:
    estimates = [movement(f"S{i}", regime=VolatilityRegime.LOW, percentile=0.1) for i in range(20)]

    assert detect_events(estimates, {}, now=NOW) == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
def test_a_stale_estimate_raises_no_alert() -> None:
    stale = movement(regime=VolatilityRegime.EXTREME, bar_age=MAX_BAR_AGE + timedelta(minutes=5))

    assert detect_events([stale], {"NVDA": VolatilityRegime.NORMAL}, now=NOW) == []


def test_a_stale_estimate_never_fabricates_a_normalisation() -> None:
    """**A stalled feed is not a calm market.**

    Reporting one as the other would describe the infrastructure while appearing
    to describe the market.
    """
    stale = movement(regime=VolatilityRegime.NORMAL, bar_age=MAX_BAR_AGE + timedelta(hours=1))

    assert detect_events([stale], {"NVDA": VolatilityRegime.EXTREME}, now=NOW) == []


def test_a_stale_estimate_leaves_the_stored_regime_untouched() -> None:
    """Otherwise the feed recovering would fire a spurious transition."""
    fresh = movement("AMD", regime=VolatilityRegime.HIGH)
    stale = movement("NVDA", bar_age=MAX_BAR_AGE * 3)

    state = next_state([fresh, stale], now=NOW)

    assert state == {"AMD": "HIGH_VOL"}


# ---------------------------------------------------------------------------
# Flood control
# ---------------------------------------------------------------------------
def test_a_market_wide_spike_becomes_one_post_not_thirty_four() -> None:
    """Phase 8.1 measured 34 of 52 elevated at once. That is one message."""
    estimates = [
        movement(f"S{i}", regime=VolatilityRegime.EXTREME, percentile=0.90 + i * 0.001)
        for i in range(34)
    ]

    events = detect_events(estimates, {}, now=NOW)
    section = build_section(events, elevated_total=34)

    assert len(events) == 34
    assert len(section["events"]) == TOP_N
    assert section["more"] == 34 - TOP_N
    assert any("more symbol" in line for line in section["lines"])


def test_the_most_unusual_names_are_the_ones_shown() -> None:
    estimates = [
        movement("CALM", regime=VolatilityRegime.HIGH, percentile=0.71),
        movement("WILD", regime=VolatilityRegime.EXTREME, percentile=0.99),
    ]

    section = build_section(detect_events(estimates, {}, now=NOW), elevated_total=2)

    assert section["events"][0]["symbol"] == "WILD"


def test_an_escalation_outranks_a_first_elevation() -> None:
    estimates = [
        movement("NEW", regime=VolatilityRegime.HIGH, percentile=0.88),
        movement("UP", regime=VolatilityRegime.EXTREME, percentile=0.91),
    ]

    events = detect_events(estimates, {"UP": VolatilityRegime.HIGH}, now=NOW)

    assert events[0].symbol == "UP"
    assert events[0].transition == VolatilityTransition.ESCALATED


def test_the_section_says_how_much_it_is_not_showing() -> None:
    estimates = [movement(f"S{i}", regime=VolatilityRegime.HIGH, percentile=0.8) for i in range(2)]

    section = build_section(detect_events(estimates, {}, now=NOW), elevated_total=30)

    assert any("30 symbols currently elevated" in line for line in section["lines"])


# ---------------------------------------------------------------------------
# Language boundary
# ---------------------------------------------------------------------------
def test_no_rendered_line_can_contain_recommendation_language() -> None:
    estimates = [movement(regime=VolatilityRegime.EXTREME, percentile=0.97)]

    section = build_section(detect_events(estimates, {}, now=NOW), elevated_total=1)

    assert_no_recommendation_language(" ".join(section["lines"]))
    assert "not a direction forecast" in section["disclaimer"].lower()


@pytest.mark.parametrize("word", ["buy", "bullish", "target", "probability"])
def test_the_guard_rejects_directional_vocabulary(word: str) -> None:
    with pytest.raises(ValueError, match="forbidden language"):
        assert_no_recommendation_language(f"NVDA looks {word} here")


def test_the_headline_uses_magnitude_vocabulary_only() -> None:
    events = detect_events([movement(regime=VolatilityRegime.EXTREME)], {}, now=NOW)

    assert events[0].headline == "EXTREME expected movement"
    assert_no_recommendation_language(events[0].headline)
