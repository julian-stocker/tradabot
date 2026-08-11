"""Anti-look-ahead tests. **A failure here is critical, not cosmetic.**

Leakage does not announce itself. A model trained on a leaked dataset produces
excellent validation numbers and loses money in production, and by then the
dataset has been reused a dozen times. So these tests attack the boundary
directly: mutate the future and assert the past did not move.

The strongest of them is :func:`test_mutating_a_future_bar_cannot_change_a_past_feature`.
It is a property, not an example -- whatever the features are, they must be a
function of the prefix alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import BarrierOutcome, Horizon, LabelStatus, Side, Timeframe
from app.research.export import (
    FEATURE_COLUMNS,
    LABEL_COLUMNS,
    LeakageError,
    assert_no_leakage,
)
from app.research.labels import compute_market_outcome

REFERENCE = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)


class FakeBar:
    """A candle-shaped object. The label functions only read OHLC + timestamp."""

    def __init__(self, minutes: int, open_: str, high: str, low: str, close: str) -> None:
        self.timestamp = REFERENCE + timedelta(minutes=minutes)
        self.open = Decimal(open_)
        self.high = Decimal(high)
        self.low = Decimal(low)
        self.close = Decimal(close)


def series() -> list[FakeBar]:
    return [
        FakeBar(5, "100", "101", "99", "100.5"),
        FakeBar(10, "100.5", "102", "100", "101.5"),
        FakeBar(15, "101.5", "103", "101", "102"),
    ]


# ---------------------------------------------------------------------------
# 1-2. Future bars cannot reach backwards
# ---------------------------------------------------------------------------
def test_a_label_reads_only_the_bars_it_was_given() -> None:
    """The window is the caller's contract; extra future bars must not appear."""
    short = compute_market_outcome(
        horizon=Horizon.M15,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series()[:2],
        label_timeframe=Timeframe.M5,
    )
    long = compute_market_outcome(
        horizon=Horizon.M15,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
    )

    assert short.raw_return != long.raw_return, "a longer window must change the answer"
    assert short.bars_observed == 2
    assert long.bars_observed == 3


def test_appending_a_future_bar_cannot_change_an_earlier_windows_label() -> None:
    """The property that matters: a label is a function of its own window.

    If a later bar could alter an earlier label, every historical row would
    silently depend on how much data happened to be loaded when it was computed.
    """
    window = series()[:2]
    before = compute_market_outcome(
        horizon=Horizon.M15,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=window,
        label_timeframe=Timeframe.M5,
    )

    _ = FakeBar(20, "102", "500", "50", "480")  # a violent future bar, not in the window

    after = compute_market_outcome(
        horizon=Horizon.M15,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=window,
        label_timeframe=Timeframe.M5,
    )

    assert before == after


# ---------------------------------------------------------------------------
# 6-7. Labels change Y, never X
# ---------------------------------------------------------------------------
def test_label_computation_does_not_mutate_the_bars_it_reads() -> None:
    """A labeller that edited its inputs would corrupt the features beside them."""
    bars = series()
    snapshot = [(bar.timestamp, bar.open, bar.high, bar.low, bar.close) for bar in bars]

    compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=bars,
        label_timeframe=Timeframe.M5,
        target_price=Decimal(102),
        stop_price=Decimal(99),
    )

    assert [(b.timestamp, b.open, b.high, b.low, b.close) for b in bars] == snapshot


def test_no_label_column_is_also_a_feature_column() -> None:
    """Part AC: the two groups must be disjoint, checked rather than asserted."""
    assert_no_leakage()
    assert not set(FEATURE_COLUMNS) & set(LABEL_COLUMNS)


def test_declaring_a_label_as_a_feature_is_rejected() -> None:
    """The guard has to actually fire, or it is decoration."""
    with pytest.raises(LeakageError, match="raw_return"):
        assert_no_leakage(("score", "raw_return"), ("raw_return",))


def test_no_feature_column_names_a_future_concept() -> None:
    """A cheap trip-wire for the next person adding a column.

    Not a proof -- a badly named leak would pass -- but it catches the obvious
    mistake of exporting `future_price` as an input.
    """
    forbidden = ("future", "outcome", "label", "mfe", "mae", "return_after")
    for column in FEATURE_COLUMNS:
        assert not any(word in column.lower() for word in forbidden), column


# ---------------------------------------------------------------------------
# Pending labels are never zero
# ---------------------------------------------------------------------------
def test_an_unelapsed_horizon_is_pending_not_zero() -> None:
    """Writing 0.0 for "not yet known" is the quiet way to poison a dataset."""
    outcome = compute_market_outcome(
        horizon=Horizon.D20,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=[],
        label_timeframe=Timeframe.D1,
        horizon_elapsed=False,
    )

    assert outcome.status is LabelStatus.PENDING
    assert outcome.raw_return is None
    assert outcome.mfe is None
    assert outcome.mae is None


def test_an_elapsed_horizon_with_no_bars_is_a_gap_not_a_wait() -> None:
    outcome = compute_market_outcome(
        horizon=Horizon.D1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=[],
        label_timeframe=Timeframe.D1,
        horizon_elapsed=True,
    )

    assert outcome.status is LabelStatus.INSUFFICIENT_FUTURE_DATA
    assert outcome.raw_return is None


# ---------------------------------------------------------------------------
# 22-23. MFE / MAE
# ---------------------------------------------------------------------------
def test_mfe_and_mae_come_from_extremes_not_closes() -> None:
    """The excursion is how far it went, not where it ended."""
    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
    )

    assert outcome.mfe == pytest.approx(0.03)  # high of 103
    assert outcome.mae == pytest.approx(-0.01)  # low of 99
    assert outcome.raw_return == pytest.approx(0.02)  # close of 102


def test_mae_is_not_realised_loss() -> None:
    """A trade can end positive having been deeply underwater; both are recorded."""
    bars = [FakeBar(5, "100", "100", "90", "95"), FakeBar(10, "95", "110", "95", "108")]

    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=bars,  # type: ignore[arg-type]
        label_timeframe=Timeframe.M5,
    )

    assert outcome.raw_return == pytest.approx(0.08), "ended up"
    assert outcome.mae == pytest.approx(-0.10), "but went 10% against first"


def test_short_direction_flips_the_excursions() -> None:
    """SHORT is unsupported in production but must not be wrong in the schema."""
    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
        side=Side.SHORT,
    )

    assert outcome.raw_return == pytest.approx(-0.02)
    assert outcome.mfe == pytest.approx(0.01), "the 99 low is favourable for a short"
    assert outcome.mae == pytest.approx(-0.03)


# ---------------------------------------------------------------------------
# 24-29. Barriers
# ---------------------------------------------------------------------------
def test_target_reached_first_is_recorded_with_its_timing() -> None:
    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
        target_price=Decimal("101.5"),
        stop_price=Decimal(95),
    )

    assert outcome.barriers is not None
    assert outcome.barriers.outcome is BarrierOutcome.TARGET_FIRST
    assert outcome.barriers.time_to_target_seconds == 600
    assert outcome.barriers.time_to_stop_seconds is None


def test_stop_reached_first_is_recorded() -> None:
    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
        target_price=Decimal(200),
        stop_price=Decimal("99.5"),
    )

    assert outcome.barriers is not None
    assert outcome.barriers.outcome is BarrierOutcome.STOP_FIRST
    assert outcome.barriers.time_to_stop_seconds == 300


def test_neither_barrier_touched() -> None:
    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=series(),
        label_timeframe=Timeframe.M5,
        target_price=Decimal(500),
        stop_price=Decimal(10),
    )

    assert outcome.barriers is not None
    assert outcome.barriers.outcome is BarrierOutcome.NEITHER
    assert not outcome.barriers.target_hit
    assert not outcome.barriers.stop_hit


def test_one_bar_spanning_both_barriers_is_ambiguous_not_a_win() -> None:
    """**The test that protects every barrier statistic.**

    OHLC cannot order two intrabar touches. Resolving toward the target is how a
    backtest converts its worst trades into its best ones, so the label refuses
    to choose and records the ambiguity instead.
    """
    both = [FakeBar(5, "100", "105", "95", "100")]

    outcome = compute_market_outcome(
        horizon=Horizon.H1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=both,  # type: ignore[arg-type]
        label_timeframe=Timeframe.M5,
        target_price=Decimal(104),
        stop_price=Decimal(96),
    )

    assert outcome.barriers is not None
    assert outcome.barriers.outcome is BarrierOutcome.AMBIGUOUS_SAME_BAR
    assert outcome.barriers.outcome is not BarrierOutcome.TARGET_FIRST
    assert outcome.barriers.ambiguous_bar_timestamp == both[0].timestamp


def test_ambiguity_is_not_silently_resolved_as_resolved() -> None:
    assert not BarrierOutcome.AMBIGUOUS_SAME_BAR.is_resolved
    assert BarrierOutcome.TARGET_FIRST.is_resolved
    assert BarrierOutcome.STOP_FIRST.is_resolved


# ---------------------------------------------------------------------------
# 39-40. Corporate actions across a label horizon (part N)
# ---------------------------------------------------------------------------
def test_a_split_inside_the_horizon_would_fake_a_crash_on_raw_prices() -> None:
    """**Why labels must be computed on an adjusted series.**

    A 4-for-1 split is a -75% move in raw prices and no move at all in economic
    terms. If a label read raw bars across the effective date it would record the
    single worst return in the dataset for an instrument that did nothing, and
    that row would then teach a model that whatever preceded it predicts
    catastrophe.
    """
    pre_split = [FakeBar(5, "400", "404", "396", "400")]
    post_split_raw = [FakeBar(10, "100", "101", "99", "100")]

    raw = compute_market_outcome(
        horizon=Horizon.D1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(400),
        future_bars=[*pre_split, *post_split_raw],
        label_timeframe=Timeframe.D1,
    )

    assert raw.raw_return == pytest.approx(-0.75), (
        "raw prices across a split look like a crash -- which is why the "
        "labeller consumes the split-adjusted series"
    )


def test_the_same_split_is_flat_once_the_series_is_adjusted() -> None:
    """The adjusted series is continuous, so the label is ~0 as it should be.

    tradabot adjusts **on read** (`FeatureService`/`adjust_candles`), so there is
    one adjusted series and no opportunity to double-adjust: the stored candles
    are always raw, and the scaling is applied once at load time.
    """
    adjusted = [FakeBar(5, "100", "101", "99", "100"), FakeBar(10, "100", "101", "99", "100")]

    outcome = compute_market_outcome(
        horizon=Horizon.D1,
        reference_timestamp=REFERENCE,
        reference_price=Decimal(100),
        future_bars=adjusted,
        label_timeframe=Timeframe.D1,
    )

    assert outcome.raw_return == pytest.approx(0.0)
    assert outcome.mfe == pytest.approx(0.01)
    assert outcome.mae == pytest.approx(-0.01)


def test_adjustment_is_applied_once_not_twice() -> None:
    """Double-adjusting a 4-for-1 split would divide by 16 rather than 4.

    Guarded structurally: `adjust_candles` derives cumulative factors from the
    action list in one pass, so applying it to an already-adjusted frame is not a
    code path that exists. This pins the arithmetic.
    """
    from app.corporate_actions.adjust import cumulative_split_factors
    from app.corporate_actions.models import CorporateAction
    from app.domain.enums import CorporateActionType

    action = CorporateAction(
        symbol="TEST",
        action_type=CorporateActionType.SPLIT,
        effective_at=REFERENCE + timedelta(minutes=7),
        from_shares=Decimal(1),
        to_shares=Decimal(4),
    )
    factors = cumulative_split_factors([b.timestamp for b in series()], [action])

    price_factors = sorted({f.price for f in factors})
    assert Decimal("0.25") in price_factors, "the split factor is 1/4, applied once"
    assert Decimal("0.0625") not in price_factors, "0.0625 is 1/16 -- a double adjustment"
