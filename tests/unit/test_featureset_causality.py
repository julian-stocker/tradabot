"""Part C: every research feature must have been knowable at observation time.

These tests are the gate the phase-6 brief puts before any analysis. They are
deliberately mechanical -- each one constructs a series, changes only the
**future**, and asserts the feature at an earlier row does not move. A feature
that fails this is not "slightly optimistic"; it is a result about information
nobody had.

The strongest test here is :func:`test_no_feature_reacts_to_a_future_bar`, which
does this for every continuous feature at once rather than for the handful
someone remembered to check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.research.featureset import (
    BOOLEAN_FEATURES,
    CONTINUOUS_FEATURES,
    NOT_CAUSALLY_AVAILABLE,
    per_symbol_features,
)

START = datetime(2021, 1, 4, 14, 0, tzinfo=UTC)


def series(closes: list[float], *, volumes: list[float] | None = None) -> pl.DataFrame:
    """An hourly OHLCV frame with a controllable close path."""
    volumes = volumes or [1_000_000.0] * len(closes)
    return pl.DataFrame(
        {
            "instrument_id": [1] * len(closes),
            "timestamp": [START + timedelta(hours=i) for i in range(len(closes))],
            "open": [c * 0.999 for c in closes],
            "high": [c * 1.004 for c in closes],
            "low": [c * 0.996 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


def rising(n: int) -> list[float]:
    return [100.0 + i * 0.35 for i in range(n)]


# ---------------------------------------------------------------------------
# The blanket test
# ---------------------------------------------------------------------------
def test_no_feature_reacts_to_a_future_bar() -> None:
    """**The gate.** Append a violent future bar; nothing earlier may change.

    Run over every continuous and boolean feature, so a newly added one is
    covered the moment it appears in the registry rather than when someone
    remembers to write its test.
    """
    base = rising(400)
    without = per_symbol_features(series(base))
    # A +40% spike, one bar into the future.
    with_future = per_symbol_features(series([*base, base[-1] * 1.4]))

    checked = 0
    for name in (*CONTINUOUS_FEATURES, *BOOLEAN_FEATURES):
        if name not in without.columns:
            continue  # context features are added by attach_context, not here
        left = without[name].to_list()
        right = with_future[name].to_list()[: len(left)]
        assert left == right, f"{name} changed when a future bar was appended"
        checked += 1

    assert checked >= 15, "the blanket test silently covered almost nothing"


def test_the_final_row_is_the_only_one_a_new_bar_may_create() -> None:
    """Sanity check on the test itself: appending a bar must add exactly one row."""
    base = rising(100)

    assert per_symbol_features(series([*base, 200.0])).height == len(base) + 1


# ---------------------------------------------------------------------------
# Specific structural traps
# ---------------------------------------------------------------------------
def test_a_bar_cannot_break_its_own_high() -> None:
    """`breakout_20` compares against the prior window, shifted by one.

    Without the shift every strong bar breaks a range it is itself the top of,
    and 'breakout' degenerates into 'went up today'.
    """
    closes = [*[100.0] * 40, 130.0]
    frame = per_symbol_features(series(closes))

    prior_high = frame["prior_high_20"].to_list()[-1]
    assert prior_high is not None
    assert prior_high < 130.0, "the breakout bar's own high leaked into its threshold"
    assert frame["breakout_20"].to_list()[-1] is True


def test_rolling_windows_end_at_the_current_row() -> None:
    """A centred window would be the classic silent leak."""
    flat_then_spike = [*[100.0] * 60, 100.0, 160.0]
    frame = per_symbol_features(series(flat_then_spike))

    atr = frame["atr"].to_list()
    # The bar *before* the spike must not know about it.
    assert atr[-2] is not None
    assert atr[-2] < atr[-1]


def test_the_percentile_rank_never_sees_later_values() -> None:
    """`atr_pct_percentile` ranks today within the trailing window only."""
    calm_then_wild = [
        *[100.0 + i * 0.05 for i in range(200)],
        *[100.0 + i * 5.0 for i in range(20)],
    ]
    frame = per_symbol_features(series(calm_then_wild))

    ranks = frame["atr_pct_percentile"].to_list()
    calm_rank = ranks[150]
    wild_rank = ranks[-1]
    assert calm_rank is not None
    assert wild_rank is not None
    assert wild_rank > calm_rank, "a later volatility regime should not flatten an earlier rank"


def test_the_consecutive_run_counter_resets_and_never_anticipates() -> None:
    closes = [*rising(80), *[80.0] * 10]
    frame = per_symbol_features(series(closes))

    runs = frame["bars_above_ema50"].to_list()
    assert runs[79] > 0, "a long uptrend should accumulate a run"
    assert runs[-1] == 0, "the run must reset once price falls below the average"


def test_returns_look_backwards_only() -> None:
    closes = [*[100.0] * 30, 110.0]
    frame = per_symbol_features(series(closes))

    returns = frame["ret_1h_pct"].to_list()
    assert returns[-1] == pytest.approx(10.0)
    assert returns[-2] == pytest.approx(0.0), "a flat bar showed a return from the next one"


def test_relative_volume_uses_the_trailing_average() -> None:
    volumes = [*[1_000_000.0] * 40, 5_000_000.0]
    frame = per_symbol_features(series([100.0] * 41, volumes=volumes))

    rel = frame["rel_volume"].to_list()
    assert rel[-1] > 3.0
    assert rel[-2] == pytest.approx(1.0, abs=0.05)


# ---------------------------------------------------------------------------
# Exclusions are declared, not silently approximated
# ---------------------------------------------------------------------------
def test_uncomputable_features_are_declared_with_a_reason() -> None:
    """Part C requires NOT_CAUSALLY_AVAILABLE rather than a quiet approximation."""
    names = {name for name, _ in NOT_CAUSALLY_AVAILABLE}

    assert "spread_bps" in names, "historical quotes do not exist and must be declared absent"
    assert any("failed_breakout" in name for name in names)
    for _, reason in NOT_CAUSALLY_AVAILABLE:
        assert len(reason) > 20, "an exclusion without a reason is just a deletion"


def test_no_excluded_feature_leaked_into_the_analysed_set() -> None:
    excluded = {name for name, _ in NOT_CAUSALLY_AVAILABLE}

    assert not (set(CONTINUOUS_FEATURES) & excluded)
    assert not (set(BOOLEAN_FEATURES) & excluded)


def test_no_outcome_derived_column_is_offered_as_a_feature() -> None:
    """Outcome fields live in `signal_outcomes` and must never appear here."""
    forbidden = ("raw_return", "mfe", "mae", "positive", "outcome", "label", "future")

    for name in (*CONTINUOUS_FEATURES, *BOOLEAN_FEATURES):
        assert not any(token in name.lower() for token in forbidden), name
