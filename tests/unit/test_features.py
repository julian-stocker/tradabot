"""Feature registry and engine behaviour."""

from __future__ import annotations

import math

import pytest

from app.core.errors import InsufficientDataError
from app.domain.enums import Timeframe
from app.features.calendars import bars_per_year
from app.features.engine import FeatureEngine
from app.features.frame import candles_to_frame
from app.features.registry import FeatureSet, FeatureSpec, build_default_feature_set


@pytest.fixture(scope="module")
def feature_set():
    return build_default_feature_set(Timeframe.D1)


class TestWarmupDeclarations:
    def test_declared_warmup_matches_actual(self, daily_candles, feature_set):
        """Every ``warmup_bars`` must equal the bar at which the value appears.

        Declared warm-ups are used to size database queries. If a declaration is
        too low the engine returns nulls a signal may treat as neutral; if it is
        too high, queries fetch history for nothing. Both are silent, so the
        declaration is verified rather than trusted.
        """
        frame = candles_to_frame(daily_candles)
        computed = frame.with_columns(feature_set.expressions())

        mismatches = {}
        for spec in feature_set:
            values = computed.get_column(spec.name).to_list()
            actual = next((i + 1 for i, v in enumerate(values) if v is not None), None)
            if actual != spec.warmup_bars:
                mismatches[spec.name] = {"declared": spec.warmup_bars, "actual": actual}

        assert not mismatches, f"warm-up declarations are wrong: {mismatches}"

    def test_set_warmup_is_the_maximum(self, feature_set):
        assert feature_set.warmup_bars == max(s.warmup_bars for s in feature_set)

    def test_every_feature_is_documented(self, feature_set):
        """Descriptions are served through the API, so an empty one is a bug."""
        undocumented = [s.name for s in feature_set if not s.description.strip()]
        assert not undocumented

    def test_expression_alias_matches_name(self, daily_candles, feature_set):
        """A spec whose expression is aliased differently would silently vanish."""
        computed = candles_to_frame(daily_candles).with_columns(feature_set.expressions())
        for spec in feature_set:
            assert spec.name in computed.columns


class TestFeatureSet:
    def test_duplicate_names_rejected(self):
        spec = FeatureSpec(
            name="dup",
            description="x",
            warmup_bars=1,
            build=lambda: __import__("polars").col("close"),
        )
        with pytest.raises(ValueError, match="already registered"):
            FeatureSet([spec, spec])

    def test_subset_selects_requested_features(self, feature_set):
        subset = feature_set.subset(["rsi_14", "sma_20"])
        assert subset.names() == ("rsi_14", "sma_20")
        assert subset.warmup_bars == 20

    def test_subset_rejects_unknown_names(self, feature_set):
        with pytest.raises(KeyError, match="unknown feature"):
            feature_set.subset(["rsi_14", "does_not_exist"])


class TestFeatureEngine:
    def test_compute_preserves_row_count_and_order(self, daily_candles, feature_set):
        frame = candles_to_frame(daily_candles)
        computed = FeatureEngine(feature_set).compute(frame)
        assert computed.height == frame.height
        assert computed.get_column("timestamp").to_list() == frame.get_column("timestamp").to_list()

    def test_compute_on_empty_frame_returns_empty(self, feature_set):
        assert FeatureEngine(feature_set).compute(candles_to_frame([])).height == 0

    def test_compute_on_short_frame_returns_nulls_not_an_error(self, daily_candles, feature_set):
        """Charting 10 bars must work even though nothing has warmed up."""
        computed = FeatureEngine(feature_set).compute(candles_to_frame(daily_candles[:10]))
        assert computed.height == 10
        assert computed.get_column("sma_20").null_count() == 10

    def test_snapshot_requires_warmup(self, daily_candles, feature_set):
        """Unlike compute, snapshot refuses rather than returning a half-warmed row."""
        engine = FeatureEngine(feature_set)
        short = candles_to_frame(daily_candles[: engine.warmup_bars - 1])
        with pytest.raises(InsufficientDataError, match="insufficient data"):
            engine.snapshot(short)

    def test_snapshot_returns_every_registered_feature(self, daily_candles, feature_set):
        snapshot = FeatureEngine(feature_set).snapshot(candles_to_frame(daily_candles))
        assert set(snapshot.values) == set(feature_set.names())

    def test_snapshot_values_are_all_warmed_up(self, daily_candles, feature_set):
        snapshot = FeatureEngine(feature_set).snapshot(candles_to_frame(daily_candles))
        missing = [name for name, value in snapshot.values.items() if value is None]
        assert not missing, f"features still null past warm-up: {missing}"

    def test_snapshot_close_matches_last_candle(self, daily_candles, feature_set):
        snapshot = FeatureEngine(feature_set).snapshot(candles_to_frame(daily_candles))
        assert math.isclose(snapshot.close, float(daily_candles[-1].close))
        assert snapshot.timestamp == daily_candles[-1].timestamp

    def test_get_unknown_feature_raises(self, daily_candles, feature_set):
        snapshot = FeatureEngine(feature_set).snapshot(candles_to_frame(daily_candles))
        with pytest.raises(KeyError, match="was not computed"):
            snapshot.get("not_a_feature")

    def test_nan_and_inf_are_normalised_to_none(self, feature_set):
        """A NaN escaping into a weighted score turns the whole score into NaN."""
        from app.features.engine import _as_optional_float

        assert _as_optional_float(float("nan")) is None
        assert _as_optional_float(float("inf")) is None
        assert _as_optional_float(None) is None
        assert _as_optional_float(1.5) == 1.5


class TestAnnualisation:
    def test_bars_per_year_scales_with_timeframe(self):
        assert bars_per_year(Timeframe.D1) == 252
        assert bars_per_year(Timeframe.H1) == 1638
        assert bars_per_year(Timeframe.W1) == 52

    def test_intraday_annualisation_differs_from_daily(self, daily_candles):
        """Using 252 for 5-minute bars understates volatility ~9x; guard the wiring."""
        daily = FeatureEngine.for_timeframe(Timeframe.D1)
        intraday = FeatureEngine.for_timeframe(Timeframe.M5)

        frame = candles_to_frame(daily_candles)
        daily_vol = daily.snapshot(frame).require("volatility_20")
        intraday_vol = intraday.snapshot(frame).require("volatility_20")

        ratio = intraday_vol / daily_vol
        expected = math.sqrt(bars_per_year(Timeframe.M5) / bars_per_year(Timeframe.D1))
        assert math.isclose(ratio, expected, rel_tol=1e-6)
