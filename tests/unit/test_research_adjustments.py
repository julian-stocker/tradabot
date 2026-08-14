"""The polars split adjustment must agree with the Decimal one, exactly.

``app.research.adjustments`` re-implements the rule that
``app.corporate_actions.adjust`` owns, because the research frames are too large
for per-row ``Decimal`` arithmetic. Two implementations of one rule is a
liability unless something forces them to stay equal, and that is what the first
test here does over randomised split schedules.

The rest fix the properties that make back-adjustment legitimate in a causal
feature pipeline: the boundary bar, reverse splits, and -- the one that
matters -- that adjusting for a split which happens *after* a window leaves
every feature in that window untouched.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from app.corporate_actions.adjust import cumulative_split_factors
from app.corporate_actions.models import CorporateAction
from app.domain.enums import CorporateActionType
from app.research.adjustments import adjust_all, adjust_for_splits, corroborated_splits
from app.research.featureset import CONTINUOUS_FEATURES, per_symbol_features

START = datetime(2021, 1, 4, 14, 0, tzinfo=UTC)


def bars(n: int, *, instrument_id: int = 1, base: float = 100.0) -> pl.DataFrame:
    """An hourly OHLCV frame with a gently rising close."""
    closes = [base + i * 0.35 for i in range(n)]
    return pl.DataFrame(
        {
            "instrument_id": [instrument_id] * n,
            "timestamp": [START + timedelta(hours=i) for i in range(n)],
            "open": [c * 0.999 for c in closes],
            "high": [c * 1.004 for c in closes],
            "low": [c * 0.996 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
        }
    )


def split_frame(pairs: list[tuple[datetime, float]]) -> pl.DataFrame:
    """``(effective_at, ratio)`` pairs as the frame ``adjust_for_splits`` wants."""
    if not pairs:
        return pl.DataFrame(
            schema={"instrument_id": pl.Int64, "effective_at": pl.Datetime, "ratio": pl.Float64}
        )
    return pl.DataFrame(
        {
            "instrument_id": [1] * len(pairs),
            "effective_at": [p[0] for p in pairs],
            "ratio": [p[1] for p in pairs],
        }
    )


def as_actions(pairs: list[tuple[datetime, float]]) -> list[CorporateAction]:
    """The same splits as domain objects, for the reference implementation."""
    return [
        CorporateAction(
            symbol="TEST",
            action_type=CorporateActionType.SPLIT,
            effective_at=when,
            from_shares=Decimal(1),
            to_shares=Decimal(str(ratio)),
        )
        for when, ratio in pairs
    ]


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(25))
def test_agrees_with_the_decimal_implementation(seed: int) -> None:
    """**The gate.** Random split schedules, compared bar by bar.

    Whole-number ratios only, because those are what splits actually are and
    because a float factor compared against a Decimal one needs a tolerance --
    which would let a real disagreement hide inside it.
    """
    rng = random.Random(seed)
    frame = bars(120)
    timestamps = frame["timestamp"].to_list()

    pairs = sorted(
        (timestamps[rng.randrange(1, 119)], float(rng.choice([2, 3, 4, 8, 10, 20])))
        for _ in range(rng.randrange(0, 4))
    )

    adjusted = adjust_for_splits(frame, split_frame(pairs))
    expected = cumulative_split_factors(timestamps, as_actions(pairs))

    for index, factor in enumerate(expected):
        assert adjusted["close"][index] == pytest.approx(
            frame["close"][index] * float(factor.price), rel=1e-12
        )
        assert adjusted["volume"][index] == pytest.approx(
            frame["volume"][index] * float(factor.volume), rel=1e-12
        )


def test_no_splits_returns_the_frame_untouched() -> None:
    frame = bars(30)
    assert adjust_for_splits(frame, split_frame([])).equals(frame)


# ---------------------------------------------------------------------------
# Boundary and direction
# ---------------------------------------------------------------------------
def test_the_effective_bar_itself_is_not_adjusted() -> None:
    """A bar stamped at the effective instant is already trading post-split.

    Off-by-one here is the difference between a correct series and one that is
    wrong at every split, and it is invisible in a chart.
    """
    frame = bars(10)
    effective = frame["timestamp"][5]
    adjusted = adjust_for_splits(frame, split_frame([(effective, 4.0)]))

    assert adjusted["close"][5] == pytest.approx(frame["close"][5])
    assert adjusted["close"][4] == pytest.approx(frame["close"][4] / 4.0)


def test_bars_after_the_last_split_keep_their_traded_prices() -> None:
    """The series must still end on a price a broker screen would show."""
    frame = bars(10)
    adjusted = adjust_for_splits(frame, split_frame([(frame["timestamp"][3], 2.0)]))
    assert adjusted["close"][9] == pytest.approx(frame["close"][9])


def test_reverse_split_raises_earlier_prices() -> None:
    """GE's 1-for-8 is a ratio below one, and the factor must invert with it."""
    frame = bars(10)
    adjusted = adjust_for_splits(frame, split_frame([(frame["timestamp"][5], 0.125)]))

    assert adjusted["close"][0] == pytest.approx(frame["close"][0] * 8.0)
    assert adjusted["volume"][0] == pytest.approx(frame["volume"][0] * 0.125)


def test_multiple_splits_compound() -> None:
    frame = bars(20)
    pairs = [(frame["timestamp"][5], 2.0), (frame["timestamp"][12], 5.0)]
    adjusted = adjust_for_splits(frame, split_frame(pairs))

    assert adjusted["close"][0] == pytest.approx(frame["close"][0] / 10.0)
    assert adjusted["close"][8] == pytest.approx(frame["close"][8] / 5.0)
    assert adjusted["close"][15] == pytest.approx(frame["close"][15])


# ---------------------------------------------------------------------------
# The property that makes this causal
# ---------------------------------------------------------------------------
def test_a_future_split_does_not_move_any_earlier_feature() -> None:
    """Back-adjustment rescales past prices; it must not change past *features*.

    This is the answer to "isn't back-adjusting lookahead?". Every feature in
    the set is scale-invariant, so multiplying an entire trailing window by a
    constant leaves all of them identical. A feature that keyed on an absolute
    price level would fail here, which is exactly what we want it to do.
    """
    frame = bars(400)
    window = 300
    future_split = frame["timestamp"][350]

    baseline = per_symbol_features(frame.head(window))
    adjusted = per_symbol_features(
        adjust_for_splits(frame, split_frame([(future_split, 10.0)])).head(window)
    )

    for feature in CONTINUOUS_FEATURES:
        if feature not in baseline.columns:
            continue
        left = baseline[feature].to_list()
        right = adjusted[feature].to_list()
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            if a is None or b is None:
                assert a is None, f"{feature} nullity differs at {index}"
                assert b is None, f"{feature} nullity differs at {index}"
                continue
            assert b == pytest.approx(a, rel=1e-9, abs=1e-9), f"{feature} moved at row {index}"


def test_the_split_discontinuity_is_removed_from_returns() -> None:
    """The defect itself: a 4-for-1 must stop looking like a 75% loss."""
    frame = bars(60)
    effective = frame["timestamp"][30]

    # Simulate the raw record: everything from the split onwards trades at 1/4.
    raw = frame.with_columns(
        pl.when(pl.col("timestamp") >= pl.lit(effective))
        .then(pl.col(name) / 4.0)
        .otherwise(pl.col(name))
        .alias(name)
        for name in ("open", "high", "low", "close")
    )

    raw_step = raw["close"][30] / raw["close"][29] - 1.0
    assert raw_step < -0.7

    adjusted = adjust_for_splits(raw, split_frame([(effective, 4.0)]))
    fixed_step = adjusted["close"][30] / adjusted["close"][29] - 1.0
    assert abs(fixed_step) < 0.01


# ---------------------------------------------------------------------------
# Corroboration: a stored action the prices do not show must not be applied
# ---------------------------------------------------------------------------
def split_the_prices(frame: pl.DataFrame, index: int, ratio: float) -> pl.DataFrame:
    """Divide prices from ``index`` onwards, as a raw feed would record a split."""
    effective = frame["timestamp"][index]
    return frame.with_columns(
        pl.when(pl.col("timestamp") >= pl.lit(effective))
        .then(pl.col(name) / ratio)
        .otherwise(pl.col(name))
        .alias(name)
        for name in ("open", "high", "low", "close")
    )


def test_a_real_split_is_corroborated() -> None:
    frame = split_the_prices(bars(60), 30, 4.0)
    splits = split_frame([(frame["timestamp"][30], 4.0)])
    assert len(corroborated_splits(frame, splits)) == 1


def test_an_action_the_prices_do_not_show_is_rejected() -> None:
    """The HON case: a declared 1-for-2 with a flat price series.

    Applying it would multiply every earlier bar by two and invent a jump.
    """
    frame = bars(60)
    splits = split_frame([(frame["timestamp"][30], 0.5)])
    assert corroborated_splits(frame, splits).is_empty()


def test_a_split_straddling_a_data_gap_is_rejected() -> None:
    """The NVDA-on-daily case.

    The bars either side of the effective date are months apart, and the price
    moved a long way in between, so their ratio reflects drift rather than the
    split -- NVDA's real numbers are a declared 10.00 against an observed 3.10.
    Rejecting is the safe answer: the series stays raw and visibly so, instead
    of being scaled by a factor the data cannot support.
    """
    frame = split_the_prices(bars(60), 30, 10.0)
    # Excise the middle, and let the surviving tail arrive after a large rally,
    # exactly as NVDA's post-gap bars do.
    tail = frame.tail(20).with_columns(
        pl.col(name) * 3.2 for name in ("open", "high", "low", "close")
    )
    with_hole = pl.concat([frame.head(20), tail])
    splits = split_frame([(frame["timestamp"][30], 10.0)])
    assert corroborated_splits(with_hole, splits).is_empty()


def test_a_violent_session_does_not_reject_a_real_split() -> None:
    """TSLA's 5-for-1 landed on a 12% up day and must still be corroborated."""
    frame = split_the_prices(bars(60), 30, 5.0)
    moved = frame.with_columns(
        pl.when(pl.col("timestamp") >= pl.lit(frame["timestamp"][30]))
        .then(pl.col("close") * 1.12)
        .otherwise(pl.col("close"))
        .alias("close")
    )
    splits = split_frame([(frame["timestamp"][30], 5.0)])
    assert len(corroborated_splits(moved, splits)) == 1


def test_adjust_all_drops_uncorroborated_actions() -> None:
    """End to end: the flat series must come back unchanged, not doubled."""
    frame = bars(40)
    splits = pl.DataFrame(
        {
            "instrument_id": [1],
            "effective_at": [frame["timestamp"][20]],
            "ratio": [0.5],
        }
    )
    adjusted = adjust_all(frame, splits)
    assert adjusted["close"].to_list() == pytest.approx(frame["close"].to_list())


# ---------------------------------------------------------------------------
# Multi-instrument dispatch
# ---------------------------------------------------------------------------
def test_adjust_all_only_touches_instruments_that_split() -> None:
    # Instrument 1's prices really do halve at the effective bar, so the split
    # is corroborated and applied. Instrument 2 has no action at all.
    left = split_the_prices(bars(20, instrument_id=1, base=100.0), 10, 2.0)
    right = bars(20, instrument_id=2, base=50.0)
    frame = pl.concat([left, right])

    splits = pl.DataFrame(
        {
            "instrument_id": [1],
            "effective_at": [left["timestamp"][10]],
            "ratio": [2.0],
        }
    )
    adjusted = adjust_all(frame, splits)

    changed = adjusted.filter(pl.col("instrument_id") == 1)
    untouched = adjusted.filter(pl.col("instrument_id") == 2)

    assert changed["close"][0] == pytest.approx(left["close"][0] / 2.0)
    assert untouched["close"].to_list() == pytest.approx(right["close"].to_list())
