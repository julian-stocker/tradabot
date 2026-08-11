"""The most important tests in the repository.

Look-ahead bias is the failure mode that makes a research platform actively
harmful: it produces backtests that look excellent and lose money live, and it is
almost invisible in code review because it usually enters through a library
default (a centred rolling window, a back-filled null, a ``reverse()``) rather
than through obviously wrong code.

So it is tested as a **property of every registered feature**, not as a
hand-written assertion per indicator:

    For every feature f and every bar i:
        f(candles[0..i])[i] == f(candles[0..n])[i]

In words: what a feature reports for bar *i* must not change when later bars
arrive. A feature that peeks into the future violates this the moment the future
exists.

The decisive property of this test is that it covers features **not yet written**.
Anything added to the registry is checked automatically, forever.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from app.domain.enums import Timeframe
from app.features.engine import FeatureEngine
from app.features.frame import candles_to_frame
from app.features.registry import build_default_feature_set

# Bars at which to truncate. Spread across the series and past the longest
# warm-up (61 bars) so every feature is exercised while warmed up.
TRUNCATION_POINTS = (65, 80, 120, 200, 349)


@pytest.fixture(scope="module")
def feature_set():
    return build_default_feature_set(Timeframe.D1)


def _values_at(frame: pl.DataFrame, engine: FeatureEngine, index: int) -> dict[str, float | None]:
    computed = engine.compute(frame)
    row = computed.row(index, named=True)
    return {name: row[name] for name in engine.feature_set.names()}


def _equal(a: float | None, b: float | None) -> bool:
    """Compare feature values, treating null and NaN as equivalent."""
    a_missing = a is None or (isinstance(a, float) and math.isnan(a))
    b_missing = b is None or (isinstance(b, float) and math.isnan(b))
    if a_missing or b_missing:
        return a_missing and b_missing
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


@pytest.mark.parametrize("truncate_at", TRUNCATION_POINTS)
def test_features_are_prefix_invariant(daily_candles, feature_set, truncate_at):
    """Truncating the series after bar i must not change any feature at bar i.

    This is the mechanical definition of "uses no future data".
    """
    assert len(daily_candles) > max(TRUNCATION_POINTS), "fixture too short for this test"

    engine = FeatureEngine(feature_set)
    full_frame = candles_to_frame(daily_candles)
    truncated_frame = candles_to_frame(daily_candles[: truncate_at + 1])

    from_full = _values_at(full_frame, engine, truncate_at)
    from_truncated = _values_at(truncated_frame, engine, truncate_at)

    mismatches = {
        name: (from_truncated[name], from_full[name])
        for name in from_full
        if not _equal(from_truncated[name], from_full[name])
    }
    assert not mismatches, (
        f"look-ahead bias detected at bar {truncate_at}. These features changed "
        f"value once later bars became available (truncated, full): {mismatches}"
    )


def test_appending_a_bar_never_changes_earlier_values(daily_candles, feature_set):
    """Streaming a bar in must leave every previously reported value untouched.

    The incremental form of the same property, and the one that matters for live
    operation: yesterday's signal must not silently rewrite itself today.
    """
    engine = FeatureEngine(feature_set)
    base_length = 120

    before = engine.compute(candles_to_frame(daily_candles[:base_length]))
    after = engine.compute(candles_to_frame(daily_candles[: base_length + 1]))

    for name in engine.feature_set.names():
        before_column = before.get_column(name).to_list()
        after_column = after.get_column(name).to_list()[:base_length]
        for index, (old, new) in enumerate(zip(before_column, after_column, strict=True)):
            assert _equal(old, new), (
                f"feature {name!r} at bar {index} changed from {old} to {new} "
                f"after a later bar was appended"
            )


def test_future_price_change_does_not_affect_past_features(daily_candles, feature_set):
    """A deliberate, drastic edit to the final bar must not affect earlier bars.

    A direct falsification attempt: if any feature reached forward, multiplying
    the last close by 10 would show up somewhere behind it.
    """
    engine = FeatureEngine(feature_set)
    window = daily_candles[:150]

    original = engine.compute(candles_to_frame(window))

    tampered_candles = [
        *window[:-1],
        window[-1].model_copy(
            update={
                "close": window[-1].close * 10,
                "high": window[-1].high * 10,
                "open": window[-1].open * 10,
                "low": window[-1].low * 10,
            }
        ),
    ]
    tampered = engine.compute(candles_to_frame(tampered_candles))

    for name in engine.feature_set.names():
        original_values = original.get_column(name).to_list()[:-1]
        tampered_values = tampered.get_column(name).to_list()[:-1]
        for index, (untouched, edited) in enumerate(
            zip(original_values, tampered_values, strict=True)
        ):
            assert _equal(untouched, edited), (
                f"feature {name!r} at bar {index} reacted to a change in the LAST bar "
                f"({untouched} -> {edited}); it is reading future data"
            )


def test_snapshot_ignores_bars_after_the_index(daily_candles, feature_set):
    """``snapshot`` on a truncated frame equals ``snapshot`` at that index of the full frame."""
    engine = FeatureEngine(feature_set)
    index = 200

    full = engine.snapshot(candles_to_frame(daily_candles), index=index)
    truncated = engine.snapshot(candles_to_frame(daily_candles[: index + 1]), index=-1)

    assert full.timestamp == truncated.timestamp
    assert math.isclose(full.close, truncated.close, rel_tol=1e-12)
    for name, value in full.values.items():
        assert _equal(value, truncated.values[name]), f"snapshot mismatch for {name!r}"
