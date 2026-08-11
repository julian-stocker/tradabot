"""Signal scoring, classification and aggregation."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.config import CostSettings, SignalSettings, SignalWeights
from app.core.errors import InsufficientDataError
from app.domain.enums import Classification, Horizon, HorizonBucket, ReasonKind, Timeframe
from app.features.engine import FeatureSnapshot
from app.signals.classify import classify, estimate_confidence
from app.signals.components import (
    MomentumComponent,
    RegimeComponent,
    SpreadComponent,
    TrendComponent,
    VolatilityComponent,
    VolumeComponent,
)
from app.signals.components.base import ScoringContext
from app.signals.components.spread import estimate_expected_move_bps
from app.signals.engine import SignalEngine
from app.signals.models import ComponentKind
from app.signals.scoring import blend, clamp, linear_score, squash

BAR_TIME = datetime(2024, 6, 3, tzinfo=UTC)

BULLISH_FEATURES = {
    "return_1": 0.012,
    "return_5": 0.045,
    "return_20": 0.11,
    "log_return_1": 0.0119,
    "sma_20": 95.0,
    "sma_50": 90.0,
    "ema_20": 96.0,
    "ema_50": 91.0,
    "dist_sma_20": 5.3,
    "ema_spread_20_50": 5.5,
    "rsi_14": 62.0,
    "atr_14": 2.0,
    "atr_pct_14": 2.0,
    "volatility_20": 0.22,
    "vol_ratio_10_60": 1.0,
    "rel_volume_20": 1.8,
    "volume_sma_20": 1_000_000.0,
}

BEARISH_FEATURES = {
    **BULLISH_FEATURES,
    "return_1": -0.012,
    "return_5": -0.045,
    "return_20": -0.11,
    "dist_sma_20": -5.3,
    "ema_spread_20_50": -5.5,
    "rsi_14": 38.0,
}


def snapshot(values: dict[str, float | None], close: float = 100.0) -> FeatureSnapshot:
    return FeatureSnapshot(timestamp=BAR_TIME, close=close, values=dict(values), bars_used=200)


def context(values: dict[str, float | None], spread_bps: str = "5.0") -> ScoringContext:
    return ScoringContext(
        symbol="TEST",
        snapshot=snapshot(values),
        timeframe=Timeframe.D1,
        horizon=Horizon.D5,
        spread_bps=Decimal(spread_bps),
    )


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------
class TestScoringPrimitives:
    def test_squash_is_zero_at_zero(self):
        assert squash(0.0, 0.05) == 0.0

    def test_squash_hits_tanh_one_at_scale(self):
        assert math.isclose(squash(0.05, 0.05), 100 * math.tanh(1.0))

    def test_squash_is_bounded_and_odd(self):
        for value in (-10.0, -0.3, 0.3, 10.0):
            assert -100.0 <= squash(value, 0.05) <= 100.0
            assert math.isclose(squash(value, 0.05), -squash(-value, 0.05))

    def test_squash_saturates_rather_than_scaling_linearly(self):
        """Outliers must not dominate a weighted average."""
        modest = squash(0.04, 0.04)
        extreme = squash(0.40, 0.04)
        assert extreme > modest
        assert extreme < modest * 2

    def test_linear_score_maps_neutral_and_full(self):
        assert linear_score(50.0, neutral=50.0, full=70.0) == 0.0
        assert linear_score(70.0, neutral=50.0, full=70.0) == 100.0
        assert linear_score(30.0, neutral=50.0, full=70.0) == -100.0

    def test_clamp_normalises_negative_zero(self):
        """-0.0 renders as '-0.0' in JSON and reads as a bearish tilt."""
        assert math.copysign(1.0, clamp(-0.0)) == 1.0

    def test_blend_with_no_weight_is_zero(self):
        assert blend((50.0, 0.0), (80.0, 0.0)) == 0.0

    def test_blend_is_a_weighted_average(self):
        assert math.isclose(blend((100.0, 0.75), (0.0, 0.25)), 75.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
class TestClassification:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (100.0, Classification.STRONG_BULLISH),
            (55.0, Classification.STRONG_BULLISH),
            (54.9, Classification.BULLISH),
            (20.0, Classification.BULLISH),
            (19.9, Classification.NEUTRAL),
            (0.0, Classification.NEUTRAL),
            (-19.9, Classification.NEUTRAL),
            (-20.0, Classification.BEARISH),
            (-54.9, Classification.BEARISH),
            (-55.0, Classification.STRONG_BEARISH),
            (-100.0, Classification.STRONG_BEARISH),
        ],
    )
    def test_thresholds_are_inclusive_and_symmetric(self, score, expected):
        assert classify(score, SignalSettings()) is expected

    def test_thresholds_are_configurable(self):
        strict = SignalSettings(bullish_threshold=40.0, strong_bullish_threshold=80.0)
        assert classify(30.0, strict) is Classification.NEUTRAL
        assert classify(50.0, strict) is Classification.BULLISH

    def test_misordered_thresholds_rejected(self):
        with pytest.raises(ValueError, match="must be greater than"):
            SignalSettings(bullish_threshold=60.0, strong_bullish_threshold=30.0)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
class TestMomentumComponent:
    def test_positive_returns_score_bullish(self):
        result = MomentumComponent(0.25).score(context(BULLISH_FEATURES))
        assert result.available
        assert result.score > 0

    def test_negative_returns_score_bearish(self):
        assert MomentumComponent(0.25).score(context(BEARISH_FEATURES)).score < 0

    def test_extreme_rsi_is_reported_as_a_risk(self):
        """Buying strength and buying exhaustion must not look identical."""
        result = MomentumComponent(0.25).score(context({**BULLISH_FEATURES, "rsi_14": 85.0}))
        codes = {r.code for r in result.risks}
        assert "rsi_extreme_overbought" in codes

    def test_mid_range_rsi_is_supporting_evidence(self):
        result = MomentumComponent(0.25).score(context(BULLISH_FEATURES))
        assert "rsi_neutral_range" in {r.code for r in result.supports}

    def test_missing_feature_marks_unavailable(self):
        """A missing input must not be silently treated as neutral."""
        result = MomentumComponent(0.25).score(context({**BULLISH_FEATURES, "rsi_14": None}))
        assert not result.available
        assert result.score == 0.0
        assert result.reasons[0].kind is ReasonKind.RISK


class TestVolumeComponent:
    def test_high_volume_on_a_rally_is_bullish(self):
        assert VolumeComponent(0.25).score(context(BULLISH_FEATURES)).score > 0

    def test_high_volume_on_a_selloff_is_bearish(self):
        """The error that makes crashes look like buy signals."""
        result = VolumeComponent(0.25).score(context({**BULLISH_FEATURES, "return_5": -0.05}))
        assert result.score < 0, "heavy volume on a decline must not score bullish"

    def test_volume_is_directionless_on_a_flat_move(self):
        result = VolumeComponent(0.25).score(
            context({**BULLISH_FEATURES, "return_5": 0.0, "rel_volume_20": 5.0})
        )
        assert result.score == 0.0
        assert "volume_without_direction" in {r.code for r in result.risks}

    def test_low_volume_is_flagged_as_a_risk(self):
        result = VolumeComponent(0.25).score(context({**BULLISH_FEATURES, "rel_volume_20": 0.4}))
        assert "low_relative_volume" in {r.code for r in result.risks}


class TestTrendComponent:
    def test_aligned_emas_score_bullish(self):
        assert TrendComponent(0.20).score(context(BULLISH_FEATURES)).score > 0

    def test_inverted_emas_score_bearish(self):
        assert TrendComponent(0.20).score(context(BEARISH_FEATURES)).score < 0

    def test_stretched_price_is_flagged(self):
        result = TrendComponent(0.20).score(context({**BULLISH_FEATURES, "dist_sma_20": 15.0}))
        assert "stretched_from_sma20" in {r.code for r in result.risks}


class TestQualityComponents:
    @pytest.mark.parametrize(
        "component",
        [VolatilityComponent(0.10), RegimeComponent(0.10), SpreadComponent(0.10)],
    )
    def test_quality_scores_are_never_positive(self, component):
        """A quality component must not be able to vote a direction."""
        for features in (BULLISH_FEATURES, BEARISH_FEATURES):
            result = component.score(context(features))
            assert result.kind is ComponentKind.QUALITY
            assert -100.0 <= result.score <= 0.0

    def test_calm_volatility_is_not_penalised(self):
        result = VolatilityComponent(0.10).score(
            context({**BULLISH_FEATURES, "volatility_20": 0.15, "atr_pct_14": 1.0})
        )
        assert result.score == 0.0

    def test_severe_volatility_is_heavily_penalised(self):
        result = VolatilityComponent(0.10).score(
            context({**BULLISH_FEATURES, "volatility_20": 1.2, "atr_pct_14": 10.0})
        )
        assert result.score == -100.0

    def test_expanding_volatility_penalises_regime(self):
        result = RegimeComponent(0.10).score(context({**BULLISH_FEATURES, "vol_ratio_10_60": 3.0}))
        assert result.score < 0
        assert "volatility_regime_break" in {r.code for r in result.risks}

    def test_counter_trend_spike_is_flagged(self):
        result = RegimeComponent(0.10).score(
            context({**BULLISH_FEATURES, "ema_spread_20_50": 4.0, "dist_sma_20": -8.0})
        )
        assert "trend_extension_conflict" in {r.code for r in result.risks}

    def test_tight_spread_on_a_volatile_name_is_free(self):
        result = SpreadComponent(0.10).score(context(BULLISH_FEATURES, spread_bps="1.0"))
        assert result.score == 0.0

    def test_wide_spread_on_a_quiet_name_is_prohibitive(self):
        """Spread only means anything relative to the move it must be paid from."""
        result = SpreadComponent(0.10).score(
            context({**BULLISH_FEATURES, "atr_pct_14": 0.05}, spread_bps="60.0")
        )
        assert result.score == -100.0
        assert "spread_prohibitive" in {r.code for r in result.risks}

    def test_identical_spread_judged_differently_by_volatility(self):
        quiet = SpreadComponent(0.10).score(
            context({**BULLISH_FEATURES, "atr_pct_14": 0.2}, spread_bps="25.0")
        )
        lively = SpreadComponent(0.10).score(
            context({**BULLISH_FEATURES, "atr_pct_14": 5.0}, spread_bps="25.0")
        )
        assert quiet.score < lively.score


# ---------------------------------------------------------------------------
# Engine aggregation
# ---------------------------------------------------------------------------
class TestSignalEngine:
    @pytest.fixture
    def engine(self, fixed_clock):
        return SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)

    def evaluate(self, engine, features, spread_bps="5.0"):
        return engine.evaluate(
            symbol="TEST",
            snapshot=snapshot(features),
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            spread_bps=Decimal(spread_bps),
            reference_price=Decimal(100),
        )

    def test_bullish_features_give_a_bullish_signal(self, engine):
        result = self.evaluate(engine, BULLISH_FEATURES)
        assert result.score > 0
        assert result.classification.direction == 1

    def test_bearish_features_give_a_bearish_signal(self, engine):
        result = self.evaluate(engine, BEARISH_FEATURES)
        assert result.score < 0
        assert result.classification.direction == -1

    def test_score_stays_in_range(self, engine):
        for features in (BULLISH_FEATURES, BEARISH_FEATURES):
            assert -100.0 <= self.evaluate(engine, features).score <= 100.0

    def test_quality_components_shrink_but_never_invert(self, engine):
        """The core aggregation property.

        Terrible conditions must move a bullish score toward zero, never past it.
        A wide spread is not a reason to short.
        """
        good = self.evaluate(engine, BULLISH_FEATURES, spread_bps="1.0")
        awful = self.evaluate(
            engine,
            {**BULLISH_FEATURES, "volatility_20": 1.5, "atr_pct_14": 12.0, "vol_ratio_10_60": 4.0},
            spread_bps="150.0",
        )
        assert awful.score < good.score
        assert awful.score >= 0.0, "quality penalties must not flip the direction"

    def test_engine_is_deterministic(self, engine):
        first = self.evaluate(engine, BULLISH_FEATURES)
        second = self.evaluate(engine, BULLISH_FEATURES)
        assert first.score == second.score
        assert first.generated_at == second.generated_at

    def test_weights_change_the_outcome(self, fixed_clock):
        """Weights must actually be wired through, not decorative config."""
        momentum_heavy = SignalEngine(
            SignalSettings(
                weights=SignalWeights(
                    momentum=0.7,
                    volume=0.1,
                    trend=0.1,
                    volatility=0.1,
                    regime=0.0,
                    spread=0.0,
                )
            ),
            CostSettings(),
            clock=fixed_clock,
        )
        trend_heavy = SignalEngine(
            SignalSettings(
                weights=SignalWeights(
                    momentum=0.1,
                    volume=0.1,
                    trend=0.7,
                    volatility=0.1,
                    regime=0.0,
                    spread=0.0,
                )
            ),
            CostSettings(),
            clock=fixed_clock,
        )
        features = {**BULLISH_FEATURES, "return_5": 0.001, "return_20": 0.002, "rsi_14": 50.0}
        assert (
            self.evaluate(momentum_heavy, features).score
            != self.evaluate(trend_heavy, features).score
        )

    def test_unavailable_components_are_renormalised(self, engine):
        """Missing features must lower confidence, not silently vote neutral."""
        degraded = self.evaluate(engine, {**BULLISH_FEATURES, "rel_volume_20": None})
        volume = next(c for c in degraded.components if c.name == "volume")
        assert not volume.available
        assert volume.weight == 0.0

        directional_weight = sum(
            c.weight for c in degraded.components if c.is_directional and c.available
        )
        assert math.isclose(directional_weight, 1.0), "remaining weights must renormalise to 1"

    def test_missing_features_reduce_confidence(self, engine):
        full = self.evaluate(engine, BULLISH_FEATURES)
        degraded = self.evaluate(engine, {**BULLISH_FEATURES, "rel_volume_20": None})
        assert degraded.confidence < full.confidence

    def test_no_directional_component_raises(self, engine):
        blind = dict.fromkeys(BULLISH_FEATURES)
        with pytest.raises(InsufficientDataError):
            self.evaluate(engine, blind)

    def test_result_carries_the_full_feature_snapshot(self, engine):
        result = self.evaluate(engine, BULLISH_FEATURES)
        assert result.feature_snapshot == BULLISH_FEATURES

    def test_result_records_the_engine_version(self, engine):
        assert self.evaluate(engine, BULLISH_FEATURES).engine_version == engine.version

    def test_horizon_bucket_is_derived(self, engine):
        assert self.evaluate(engine, BULLISH_FEATURES).horizon.bucket is HorizonBucket.MEDIUM_TERM

    def test_duplicate_component_names_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            SignalEngine(
                SignalSettings(),
                CostSettings(),
                components=[MomentumComponent(0.5), MomentumComponent(0.5)],
            )

    def test_unknown_component_name_rejected(self):
        class Rogue:
            name = "astrology"
            kind = ComponentKind.DIRECTIONAL

            def score(self, context):
                raise NotImplementedError

        with pytest.raises(ValueError, match="no configured weight"):
            SignalEngine(SignalSettings(), CostSettings(), components=[Rogue()])


class TestExplainability:
    def test_signal_reports_supports_and_risks(self, fixed_clock):
        engine = SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)
        result = engine.evaluate(
            symbol="TEST",
            snapshot=snapshot({**BULLISH_FEATURES, "rsi_14": 82.0}),
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            spread_bps=Decimal("5.0"),
            reference_price=Decimal(100),
        )
        assert result.reasons, "a signal must explain itself"
        assert result.risks, "an overbought RSI must appear as a risk"
        assert all(r.kind is ReasonKind.SUPPORT for r in result.reasons)
        assert all(r.kind is ReasonKind.RISK for r in result.risks)

    def test_every_reason_has_a_message_and_code(self, fixed_clock):
        engine = SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)
        result = engine.evaluate(
            symbol="TEST",
            snapshot=snapshot(BULLISH_FEATURES),
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            spread_bps=Decimal("5.0"),
            reference_price=Decimal(100),
        )
        for reason in (*result.reasons, *result.risks):
            assert reason.code
            assert reason.message


class TestNetEdgeIntegration:
    def test_high_cost_makes_a_bullish_signal_unactionable(self, fixed_clock):
        """A bullish view is not an opportunity if costs eat the move."""
        engine = SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)
        result = engine.evaluate(
            symbol="ILLIQUID",
            snapshot=snapshot({**BULLISH_FEATURES, "atr_pct_14": 0.1}),
            timeframe=Timeframe.D1,
            horizon=Horizon.D1,
            spread_bps=Decimal("400"),
            reference_price=Decimal(100),
        )
        assert result.net_edge.net_edge_bps < 0
        assert not result.is_actionable

    def test_expected_move_scales_with_conviction(self):
        weak = estimate_expected_move_bps(
            atr_pct=2.0, score=20.0, horizon_bars=5, capture_ratio=0.25
        )
        strong = estimate_expected_move_bps(
            atr_pct=2.0, score=80.0, horizon_bars=5, capture_ratio=0.25
        )
        # Linear in conviction, up to the estimator's 4-decimal quantisation.
        assert math.isclose(float(strong), float(weak) * 4, rel_tol=1e-5)

    def test_expected_move_scales_with_sqrt_time(self):
        one = estimate_expected_move_bps(
            atr_pct=2.0, score=100.0, horizon_bars=1, capture_ratio=1.0
        )
        four = estimate_expected_move_bps(
            atr_pct=2.0, score=100.0, horizon_bars=4, capture_ratio=1.0
        )
        assert math.isclose(float(four), float(one) * 2, rel_tol=1e-9)

    def test_capture_ratio_must_be_a_fraction(self):
        with pytest.raises(ValueError, match="capture_ratio"):
            estimate_expected_move_bps(atr_pct=2.0, score=50.0, horizon_bars=5, capture_ratio=1.5)


class TestConfidence:
    def test_no_components_gives_zero(self):
        assert estimate_confidence((), 0.0) == 0.0

    def test_agreeing_components_beat_disagreeing_ones(self, fixed_clock):
        """Identical scores from agreement and from cancellation are different states."""
        engine = SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)

        agreeing = engine.evaluate(
            symbol="A",
            snapshot=snapshot(BULLISH_FEATURES),
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            spread_bps=Decimal("2"),
            reference_price=Decimal(100),
        )
        conflicting = engine.evaluate(
            symbol="B",
            snapshot=snapshot({**BULLISH_FEATURES, "return_5": -0.08, "return_20": -0.15}),
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            spread_bps=Decimal("2"),
            reference_price=Decimal(100),
        )
        assert agreeing.confidence > conflicting.confidence

    def test_confidence_is_bounded(self, fixed_clock):
        engine = SignalEngine(SignalSettings(), CostSettings(), clock=fixed_clock)
        for features in (BULLISH_FEATURES, BEARISH_FEATURES):
            result = engine.evaluate(
                symbol="T",
                snapshot=snapshot(features),
                timeframe=Timeframe.D1,
                horizon=Horizon.D5,
                spread_bps=Decimal("5"),
                reference_price=Decimal(100),
            )
            assert 0.0 <= result.confidence <= 1.0


class TestHorizons:
    @pytest.mark.parametrize(
        ("horizon", "bucket"),
        [
            (Horizon.M30, HorizonBucket.SHORT_TERM),
            (Horizon.D1, HorizonBucket.SHORT_TERM),
            (Horizon.D5, HorizonBucket.MEDIUM_TERM),
            (Horizon.D20, HorizonBucket.MEDIUM_TERM),
            (Horizon.MO3, HorizonBucket.LONG_TERM),
        ],
    )
    def test_horizon_buckets(self, horizon, bucket):
        assert horizon.bucket is bucket

    def test_every_horizon_has_a_bucket_and_duration(self):
        for horizon in Horizon:
            assert horizon.bucket in HorizonBucket
            assert horizon.duration.total_seconds() > 0

    def test_bars_for_timeframe(self):
        assert Horizon.D5.bars_for_timeframe(Timeframe.D1) == 5
        assert Horizon.D1.bars_for_timeframe(Timeframe.H1) == 24
        assert Horizon.M30.bars_for_timeframe(Timeframe.M5) == 6

    def test_bars_for_timeframe_is_at_least_one(self):
        """A horizon shorter than one bar still spans a bar."""
        assert Horizon.M30.bars_for_timeframe(Timeframe.D1) == 1
