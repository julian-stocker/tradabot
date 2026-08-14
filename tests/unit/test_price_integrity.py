"""A split must never look like market movement, and a crash must never look like a split.

Phase 9A adjusted the research frames and left two production consumers reading
raw bars; phase 9B routed those through the adjustment layer and added a scan
that asks whether every discontinuity has a reason. These tests hold both ends
of that: the classifier must not cry split at a real crash, and no price-derived
feature may spike because a company changed its share count.

The feature tests are written against a **synthetic 4-for-1** rather than against
stored data on purpose. Real bars would make the assertions depend on a database
that can be re-backfilled; a constructed series states the invariant directly --
adjust the split away and the feature is continuous, leave it and the feature
explodes.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import polars as pl
import pytest

from app.corporate_actions.models import CorporateAction
from app.domain.enums import CorporateActionType, Timeframe
from app.market_data.integrity import (
    CORROBORATION_BAND,
    MARKET_GAP_RATIO,
    MAX_CORROBORATION_GAP,
    SPLIT_LIKE_RATIO,
    DiscontinuityKind,
    classify_series,
    contradicted_actions,
)
from app.market_data.volatility import estimate, regime_for
from app.research import adjustments as research_adjustments
from app.research.adjustments import adjust_for_splits
from app.research.featureset import per_symbol_features

START = datetime(2022, 1, 3, 14, 0, tzinfo=UTC)


def split(effective: datetime, ratio: str) -> CorporateAction:
    """A forward split when ``ratio`` > 1, a reverse split when < 1."""
    numerator, denominator = (
        (Decimal(ratio), Decimal(1))
        if float(ratio) >= 1
        else (
            Decimal(1),
            Decimal(str(round(1 / float(ratio)))),
        )
    )
    return CorporateAction(
        symbol="TEST",
        action_type=CorporateActionType.SPLIT,
        effective_at=effective,
        from_shares=denominator,
        to_shares=numerator,
    )


def daily(closes: list[float]) -> tuple[list[datetime], list[float]]:
    return [START + timedelta(days=i) for i in range(len(closes))], closes


def frame(closes: list[float], *, volumes: list[float] | None = None) -> pl.DataFrame:
    """An hourly OHLCV frame whose highs and lows track the close."""
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


def apply_raw_split(values: list[float], index: int, ratio: float) -> list[float]:
    """What the provider records: everything from ``index`` trades at 1/ratio."""
    return [v / ratio if i >= index else v for i, v in enumerate(values)]


def split_frame(effective: datetime, ratio: float) -> pl.DataFrame:
    return pl.DataFrame({"instrument_id": [1], "effective_at": [effective], "ratio": [ratio]})


# ---------------------------------------------------------------------------
# 1-4: classification
# ---------------------------------------------------------------------------
def test_a_real_split_is_explained() -> None:
    stamps, closes = daily(apply_raw_split([100.0 + i for i in range(20)], 10, 4.0))
    findings = classify_series("T", Timeframe.D1, stamps, closes, [split(stamps[10], "4")])

    assert len(findings) == 1
    assert findings[0].kind is DiscontinuityKind.EXPLAINED


def test_a_reverse_split_is_explained() -> None:
    """GE's 1-for-8 raises prices; the ratio inverts and the rule must follow."""
    stamps, closes = daily(apply_raw_split([12.0 + i * 0.1 for i in range(20)], 10, 0.125))
    findings = classify_series("T", Timeframe.D1, stamps, closes, [split(stamps[10], "0.125")])

    assert len(findings) == 1
    assert findings[0].kind is DiscontinuityKind.EXPLAINED


def test_a_split_shaped_jump_with_no_action_is_unexplained() -> None:
    """**The SMH case.** A 2-for-1 with nothing in the table to explain it."""
    stamps, closes = daily(apply_raw_split([200.0 + i for i in range(20)], 10, 2.0))
    findings = classify_series("T", Timeframe.D1, stamps, closes, [])

    assert [f.kind for f in findings] == [DiscontinuityKind.UNEXPLAINED]


def test_a_declared_split_the_prices_deny_is_contradicted() -> None:
    """**The HON case.** Applying it would invent a +100% jump."""
    stamps, closes = daily([100.0 + i * 0.2 for i in range(20)])
    denied = contradicted_actions(stamps, closes, [split(stamps[10], "0.5")])

    assert len(denied) == 1
    assert denied[0][1] == pytest.approx(closes[9] / closes[10], rel=1e-6)


def test_an_ordinary_large_market_gap_is_not_called_a_split() -> None:
    """**The NFLX case.** A real -35% crash must survive as market data.

    This is the failure that matters most: a classifier eager enough to call
    this a split is a classifier that would erase it from the record.
    """
    closes = [400.0] * 10 + [259.0] * 10  # -35.25%, ratio 1.544
    stamps, closes = daily(closes)
    findings = classify_series("T", Timeframe.D1, stamps, closes, [])

    assert [f.kind for f in findings] == [DiscontinuityKind.MARKET_GAP]


def test_a_quiet_series_produces_no_findings() -> None:
    stamps, closes = daily([100.0 + i * 0.3 for i in range(40)])
    assert classify_series("T", Timeframe.D1, stamps, closes, []) == []


def test_a_split_across_a_data_gap_is_not_contradicted() -> None:
    """**The NVDA case.** "Cannot check" must not become "is wrong".

    Phase 9A rejected NVDA's genuine 10-for-1 because its daily bars straddled a
    557-day hole, leaving every earlier daily price ten times too high.
    """
    before = [START + timedelta(days=i) for i in range(5)]
    after = [START + timedelta(days=600 + i) for i in range(5)]
    stamps = before + after
    closes = [100.0] * 5 + [310.0] * 5  # ten-for-one, then a huge rally

    denied = contradicted_actions(stamps, closes, [split(before[-1] + timedelta(days=1), "10")])
    assert denied == []


def test_the_thresholds_leave_no_gap_between_categories() -> None:
    """MARKET_GAP and SPLIT_LIKE must bracket the measured record."""
    assert MARKET_GAP_RATIO < 1.543 < SPLIT_LIKE_RATIO < 1.958


def test_the_corroboration_rules_match_the_research_layer() -> None:
    """Two implementations of one rule, pinned so they cannot drift."""
    assert CORROBORATION_BAND == research_adjustments.CORROBORATION_BAND
    assert MAX_CORROBORATION_GAP == research_adjustments.MAX_CORROBORATION_GAP


# ---------------------------------------------------------------------------
# 5-8: no price-derived feature may spike on a split
# ---------------------------------------------------------------------------
def walk(n: int, *, seed: int = 11, base: float = 100.0) -> list[float]:
    """A deterministic random walk with realistic bar-to-bar variation.

    A perfectly smooth ramp would make ATR% constant, and a percentile rank over
    constant values is decided by floating-point noise -- which would make the
    volatility assertions below measure nothing.
    """
    rng = random.Random(seed)
    values, price = [], base
    for _ in range(n):
        price *= 1.0 + rng.gauss(0.0004, 0.009)
        values.append(price)
    return values


RAW = apply_raw_split(walk(400), 200, 4.0)
EFFECTIVE = START + timedelta(hours=200)


@pytest.fixture
def raw_features() -> pl.DataFrame:
    return per_symbol_features(frame(RAW))


@pytest.fixture
def adjusted_features() -> pl.DataFrame:
    return per_symbol_features(adjust_for_splits(frame(RAW), split_frame(EFFECTIVE, 4.0)))


@pytest.mark.parametrize(
    ("feature", "min_reduction"),
    [
        ("ret_1d_pct", 2.5),
        ("ret_5d_pct", 2.5),
        ("atr_pct", 2.5),
        ("realised_vol_pct", 2.5),
        ("px_vs_ema20_pct", 2.5),
        ("px_vs_ema50_pct", 2.5),
        ("px_vs_ema200_pct", 2.5),
        ("momentum_accel", 2.5),
        # ATR-normalised, so the split inflates numerator *and* denominator and
        # partially cancels. It is still badly distorted -- 10.3 against 4.9 --
        # but it cannot reach the same reduction, and pretending otherwise would
        # mean tuning the threshold until the weakest case passed.
        ("dist_ema20_atr", 2.0),
    ],
)
def test_no_feature_spikes_once_the_split_is_adjusted(
    raw_features: pl.DataFrame,
    adjusted_features: pl.DataFrame,
    feature: str,
    min_reduction: float,
) -> None:
    """Every one of these explodes on raw bars and must be calm on adjusted ones.

    Stated as a ratio rather than an absolute limit. The series underneath is a
    random walk, so its honest extremes drift with the seed; what must hold
    regardless is that removing the split removes most of the extreme. A
    threshold in percentage points would be a number tuned to one fixture.
    """
    raw_extreme = float(raw_features[feature].drop_nulls().abs().max() or 0.0)
    adjusted_extreme = float(adjusted_features[feature].drop_nulls().abs().max() or 0.0)

    assert raw_extreme > 0
    assert adjusted_extreme < raw_extreme / min_reduction, (
        f"{feature}: raw {raw_extreme:.2f} -> adjusted {adjusted_extreme:.2f}; "
        "the split still dominates"
    )


def test_rsi_stays_inside_its_normal_band(
    raw_features: pl.DataFrame, adjusted_features: pl.DataFrame
) -> None:
    """RSI is bounded 0-100 either way, so the tell is saturation, not overflow."""
    raw = raw_features["rsi14"].drop_nulls()
    adjusted = adjusted_features["rsi14"].drop_nulls()
    # The split drives raw RSI to a floor it never reaches otherwise.
    assert float(raw.min()) < 10.0
    assert float(adjusted.min()) > 15.0


def test_breakout_flags_do_not_fire_on_the_split_bar(
    raw_features: pl.DataFrame, adjusted_features: pl.DataFrame
) -> None:
    """A 75% drop is a breakdown of the 20-bar low on raw bars, and nothing at all
    once adjusted. A false breakdown is exactly what would drive a lifecycle
    transition or a #market-trends structure alert."""
    assert bool(raw_features["breakdown_20"].to_list()[200])
    assert not bool(adjusted_features["breakdown_20"].to_list()[200])


def test_returns_across_the_split_are_economically_true(
    adjusted_features: pl.DataFrame,
) -> None:
    """The bar spanning the split must show the real move, not the share count."""
    step = adjusted_features["ret_1d_pct"].to_list()[200]
    assert step is not None
    assert abs(step) < 3.0


# ---------------------------------------------------------------------------
# 9: volatility-v1 regime continuity
# ---------------------------------------------------------------------------
def test_volatility_v1_does_not_report_extreme_because_of_a_split() -> None:
    """**The production-safety test.** A split must not manufacture EXTREME_VOL.

    Built on the same engine the scheduled job runs, so a future change that
    reintroduces raw reads fails here rather than in Discord.
    """
    bars = 300
    closes = apply_raw_split(walk(bars, seed=5), bars - 30, 4.0)
    stamps = [START + timedelta(hours=i) for i in range(bars)]

    def estimate_from(values: list[float]):
        return estimate(
            symbol="TEST",
            highs=[v * 1.004 for v in values],
            lows=[v * 0.996 for v in values],
            closes=values,
            bar_timestamp=stamps[-1],
            now=stamps[-1],
        )

    adjusted = adjust_for_splits(frame(closes), split_frame(stamps[bars - 30], 4.0))
    raw_result = estimate_from(closes)
    adjusted_result = estimate_from(adjusted["close"].to_list())

    assert raw_result is not None
    assert adjusted_result is not None
    assert raw_result.regime.is_elevated, "raw bars were expected to look violent"
    assert not adjusted_result.regime.is_elevated
    assert adjusted_result.atr_pct < raw_result.atr_pct / 3


def test_regime_thresholds_are_unchanged_by_this_phase() -> None:
    """volatility-v1 calibration is frozen; phase 9B may not tune it."""
    assert regime_for(0.10).value == "LOW_VOL"
    assert regime_for(0.50).value == "NORMAL_VOL"
    assert regime_for(0.80).value == "HIGH_VOL"
    assert regime_for(0.95).value == "EXTREME_VOL"


# ---------------------------------------------------------------------------
# 10: causal replay
# ---------------------------------------------------------------------------
def test_a_split_after_the_window_does_not_change_the_window(
    raw_features: pl.DataFrame,
) -> None:
    """Adjusting for a *future* split must leave earlier features identical.

    Back-adjustment rescales past prices, so this is the property that makes it
    legitimate: every feature here is scale-invariant, and a constant factor
    over a whole trailing window cancels.
    """
    window = 150
    baseline = per_symbol_features(frame(RAW).head(window))
    with_future_split = per_symbol_features(
        adjust_for_splits(frame(RAW), split_frame(EFFECTIVE, 4.0)).head(window)
    )

    for feature in ("ret_1d_pct", "atr_pct", "rsi14", "px_vs_ema50_pct"):
        assert baseline[feature].to_list() == pytest.approx(
            with_future_split[feature].to_list(), rel=1e-9, nan_ok=True
        )
