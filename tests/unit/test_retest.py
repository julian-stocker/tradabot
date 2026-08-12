"""Retest sequence: causal labelling, role reversal, and entry timing.

The sequence exists to solve a measured problem -- after a breakout confirms,
the structural invalidation sits a median 497 bps away, too far to size around.
Waiting for price to return to the broken level puts entry beside support again
and cuts that to 141 bps.

It works. Whether it is *profitable* is a separate question these tests do not
answer, and the historical answer was no -- see docs/trade-plans.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.scanner.levels import LevelType, Zone
from app.scanner.plans import (
    MIN_EPISODES_FOR_LIVE_BUY,
    PREFERRED_EPISODES_FOR_LIVE_BUY,
    RetestState,
    retest_invalidation,
    track_retest,
)

START = datetime(2026, 8, 1, tzinfo=UTC)
ATR = 1.0


class Bar:
    def __init__(self, i: int, close: float, low: float | None = None) -> None:
        self.timestamp = START + timedelta(hours=i)
        self.close = close
        self.low = close - 0.5 if low is None else low
        self.high = close + 0.5
        self.open = close


def zone(lower: float = 99.0, upper: float = 101.0) -> Zone:
    return Zone(
        type=LevelType.RESISTANCE,
        lower_bound=lower,
        upper_bound=upper,
        timeframe="1h",
        touch_count=3,
        first_seen=START,
        last_seen=START,
        strength=0.8,
        confidence=0.8,
    )


def test_no_breakout_means_no_sequence() -> None:
    bars = [Bar(i, 95.0) for i in range(10)]

    assert track_retest(zone=zone(), bars=bars, atr=ATR).state is RetestState.NONE


def test_one_close_above_is_not_a_breakout() -> None:
    """**Causality.** One close is an attempt; confirmation needs a second."""
    bars = [Bar(0, 95.0), Bar(1, 103.0)]

    assert track_retest(zone=zone(), bars=bars, atr=ATR).state is RetestState.NONE


def test_two_consecutive_closes_above_start_the_watch() -> None:
    bars = [Bar(0, 95.0), Bar(1, 103.0), Bar(2, 104.0)]

    context = track_retest(zone=zone(), bars=bars, atr=ATR)

    assert context.state is RetestState.WATCH_FOR_RETEST
    assert context.breakout_timestamp is not None


def test_returning_to_the_zone_is_a_retest_in_progress() -> None:
    bars = [Bar(0, 95.0), Bar(1, 103.0), Bar(2, 104.0), Bar(3, 101.5, low=100.8)]

    context = track_retest(zone=zone(), bars=bars, atr=ATR)

    assert context.state is RetestState.RETEST_IN_PROGRESS
    assert context.retest_first_touch is not None


def test_reclaiming_the_zone_confirms_the_retest() -> None:
    """**Role reversal**: broken resistance now acting as support."""
    bars = [
        Bar(0, 95.0),
        Bar(1, 103.0),
        Bar(2, 104.0),
        Bar(3, 101.2, low=100.5),
        Bar(4, 103.5),
    ]

    context = track_retest(zone=zone(), bars=bars, atr=ATR)

    assert context.state is RetestState.RETEST_CONFIRMED
    assert context.state.is_entry_ready
    assert context.retest_confirmation_timestamp is not None


def test_losing_the_zone_is_a_failed_retest() -> None:
    """Broken resistance that does not hold is not support."""
    bars = [
        Bar(0, 95.0),
        Bar(1, 103.0),
        Bar(2, 104.0),
        Bar(3, 101.2, low=100.5),
        Bar(4, 96.0),
    ]

    context = track_retest(zone=zone(), bars=bars, atr=ATR)

    assert context.state is RetestState.FAILED_RETEST
    assert not context.state.is_entry_ready


def test_a_confirmed_retest_can_still_fail_later() -> None:
    bars = [
        Bar(0, 95.0),
        Bar(1, 103.0),
        Bar(2, 104.0),
        Bar(3, 101.2, low=100.5),
        Bar(4, 103.5),
        Bar(5, 95.0),
    ]

    assert track_retest(zone=zone(), bars=bars, atr=ATR).state is RetestState.FAILED_RETEST


def test_the_sequence_uses_only_bars_already_seen() -> None:
    """**The look-ahead test.**

    The state after N bars must not change because later bars exist. If it did,
    every historical retest label would depend on how much data was loaded.
    """
    full = [
        Bar(0, 95.0),
        Bar(1, 103.0),
        Bar(2, 104.0),
        Bar(3, 101.2, low=100.5),
        Bar(4, 103.5),
        Bar(5, 90.0),
    ]

    early = track_retest(zone=zone(), bars=full[:5], atr=ATR)
    late = track_retest(zone=zone(), bars=full, atr=ATR)

    assert early.state is RetestState.RETEST_CONFIRMED
    assert late.state is RetestState.FAILED_RETEST, "later bars change only the later state"
    assert early.retest_confirmation_timestamp == late.retest_confirmation_timestamp


def test_entry_is_never_ready_before_confirmation() -> None:
    """Entry may only follow a confirmed retest, never the touch itself."""
    for state in (
        RetestState.NONE,
        RetestState.WATCH_FOR_RETEST,
        RetestState.RETEST_IN_PROGRESS,
        RetestState.FAILED_RETEST,
    ):
        assert not state.is_entry_ready


def test_invalidation_sits_below_the_reclaimed_zone() -> None:
    """Structural, not a percentage: the thesis is 'this level holds'."""
    target = zone()
    price, reason = retest_invalidation(target, ATR)

    assert price < target.lower_bound
    assert "reclaimed" in reason


def test_retest_entry_has_a_tighter_risk_than_immediate_entry() -> None:
    """**The mechanical claim the hypothesis rests on.**

    Measured historically: 497 bps immediate vs 141 bps after retest.
    """
    target = zone(lower=99.0, upper=101.0)
    invalidation, _ = retest_invalidation(target, ATR)

    retest_entry = target.upper_bound
    ran_away_entry = 108.0

    retest_risk = (retest_entry - invalidation) / retest_entry
    immediate_risk = (ran_away_entry - invalidation) / ran_away_entry

    assert retest_risk < immediate_risk


def test_the_live_sample_floor_is_declared_and_conservative() -> None:
    """Declared before the experiment so it could not be relaxed afterwards."""
    assert MIN_EPISODES_FOR_LIVE_BUY == 30
    assert PREFERRED_EPISODES_FOR_LIVE_BUY == 50
    assert PREFERRED_EPISODES_FOR_LIVE_BUY > MIN_EPISODES_FOR_LIVE_BUY
