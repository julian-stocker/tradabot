"""Phase 12's conclusions are only worth as much as its causality.

A single leaked future bar would make every advantage in this phase real-looking
and worthless. These tests pin the construction — where features may read from,
where targets may read from, and that the two never share a bar — plus the
pre-registered constants, so moving a threshold after seeing a result means
editing a test that says not to.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import polars as pl
import pytest

from app.core.config import CostSettings
from app.research import phase12
from app.research.phase12 import (
    ADVANTAGE_HIT_RATE_PP,
    ADVANTAGE_NET_RETURN_PP,
    FDR_Q,
    MIN_HISTORY_BARS,
    MIN_POOLED_SAMPLE,
    MOVEMENT_COST_BUCKETS,
    PULLBACK_BUCKETS,
    SELECTIVITY_BUCKETS,
    STABILITY_FOLD_FRACTION,
    Advantage,
    HypothesisRegistry,
    Verdict,
    bars_above,
    causal_features,
    classify,
    cross_sectional,
    forward_targets,
    measure,
    round_trip_cost_pct,
    stability,
    universe_median,
)

T0 = datetime(2024, 1, 2, tzinfo=UTC)


def series(closes: list[float], symbol: str = "AAA") -> pl.DataFrame:
    """A clean OHLC frame where open==close==mid, so arithmetic is checkable."""
    return pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "timestamp": [T0 + timedelta(days=i) for i in range(len(closes))],
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        }
    )


# ---------------------------------------------------------------------------
# Causality: the one thing that cannot be wrong
# ---------------------------------------------------------------------------
class TestNoLeakage:
    def test_entry_is_the_next_session_open_never_the_signal_close(self) -> None:
        """**The gate.** A fill at the signal bar's close is the most flattering
        bug a research panel can have."""
        frame = forward_targets(series([100.0, 110.0, 120.0, 130.0]), [1])
        assert frame["entry_price"][0] == 110.0
        assert frame["entry_price"][1] == 120.0

    def test_forward_return_runs_from_the_entry_open_to_the_horizon_close(self) -> None:
        frame = forward_targets(series([100.0, 200.0, 300.0, 400.0]), [1, 2])
        assert frame["fwd_1d"][0] == pytest.approx(0.0)  # open 200 -> close 200
        assert frame["fwd_2d"][0] == pytest.approx(50.0)  # open 200 -> close 300

    def test_excursions_only_see_bars_inside_the_forward_window(self) -> None:
        """MFE must not include the signal bar, nor anything past the horizon."""
        closes = [100.0, 100.0, 500.0, 100.0, 9999.0]
        frame = forward_targets(series(closes), [2])
        # Window is bars 1..2: highs 101 and 505. The 9999 bar is outside it.
        assert frame["mfe_2d"][0] == pytest.approx((505.0 / 100.0 - 1) * 100)

    def test_a_feature_never_reads_a_later_bar(self) -> None:
        """Truncating the future must not change any feature already computed.

        The strongest available check: build features on the full series, build
        them again on a prefix, and require every row of the prefix to match. A
        forward-looking window would differ.
        """
        closes = [100.0 + i for i in range(80)]
        full = bars_above(causal_features(series(closes)))
        prefix = bars_above(causal_features(series(closes[:60])))
        for column in ("ret_20d", "ema20", "atr14", "trend_dist_atr", "bars_above_ema20"):
            a = full[column][:60].to_list()
            b = prefix[column].to_list()
            for x, y in zip(a, b, strict=True):
                if x is None or y is None:
                    assert x is None
                    assert y is None
                else:
                    assert x == pytest.approx(y)

    def test_targets_are_null_where_the_window_is_incomplete(self) -> None:
        """A truncated window must be dropped, not silently short-measured."""
        frame = forward_targets(series([100.0, 101.0, 102.0]), [3])
        assert frame["fwd_3d"][-1] is None


# ---------------------------------------------------------------------------
# Cross-sectional construction
# ---------------------------------------------------------------------------
class TestCrossSection:
    def build(self) -> pl.DataFrame:
        rows = []
        for day in range(3):
            for index, symbol in enumerate(["A", "B", "C", "D"]):
                rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": T0 + timedelta(days=day),
                        "sector": "tech" if index < 2 else "energy",
                        "ret_20d": float(index),
                        "ret_5d": float(index),
                        "rel_mom_market_20d": float(index),
                        "rel_mom_sector_20d": float(index),
                        "fwd_1d": float(index) - 1.5,
                        "mfe_1d": 1.0,
                        "mae_1d": -1.0,
                        "year": 2024,
                    }
                )
        return cross_sectional(pl.DataFrame(rows))

    def test_ranks_are_computed_within_a_timestamp_not_across_history(self) -> None:
        frame = self.build()
        day = frame.filter(pl.col("timestamp") == T0).sort("symbol")
        assert day["xs_rank_ret_20d"].to_list() == [
            0.0,
            pytest.approx(1 / 3),
            pytest.approx(2 / 3),
            1.0,
        ]

    def test_every_session_ranks_independently(self) -> None:
        """A rank pooled across days would encode the calendar, not the cross-section."""
        frame = self.build()
        for timestamp in frame["timestamp"].unique():
            day = frame.filter(pl.col("timestamp") == timestamp)
            assert day["xs_rank_ret_20d"].min() == 0.0
            assert day["xs_rank_ret_20d"].max() == 1.0

    def test_the_universe_median_is_per_session(self) -> None:
        frame = self.build()
        median = universe_median(frame, 1)
        assert median.height == 3
        assert median["median_fwd_1d"][0] == pytest.approx(0.0)

    def test_universe_size_is_recorded_so_thin_sessions_are_visible(self) -> None:
        assert self.build()["universe_size"].to_list()[0] == 4


# ---------------------------------------------------------------------------
# The measured advantage
# ---------------------------------------------------------------------------
class TestAdvantage:
    def frame(self) -> pl.DataFrame:
        rows = []
        for day in range(40):
            for index, symbol in enumerate(["A", "B", "C", "D"]):
                rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": T0 + timedelta(days=day),
                        "rank": index / 3,
                        "fwd_1d": [-2.0, -1.0, 1.0, 2.0][index],
                        "mfe_1d": 1.0,
                        "mae_1d": -1.0,
                        "year": 2024,
                    }
                )
        return pl.DataFrame(rows)

    def test_advantage_is_measured_against_the_same_session_median(self) -> None:
        """Not against zero, and not against a fixed benchmark."""
        result = measure(self.frame(), pl.col("rank") >= 1.0, name="top", horizon=1, cost_pct=0.25)
        assert result.gross_advantage_pp == pytest.approx(2.0)
        assert result.hit_rate == 1.0

    def test_net_absolute_charges_execution_cost_and_gross_does_not(self) -> None:
        """The two answer different questions and must not be collapsed."""
        result = measure(self.frame(), pl.col("rank") >= 1.0, name="top", horizon=1, cost_pct=0.25)
        assert result.net_absolute_pp == pytest.approx(2.0 - 0.25)

    def test_a_selection_that_matches_the_pool_shows_no_advantage(self) -> None:
        result = measure(self.frame(), pl.col("rank") >= 0.0, name="all", horizon=1, cost_pct=0.25)
        assert result.gross_advantage_pp == pytest.approx(0.0, abs=1e-9)

    def test_an_empty_selection_is_an_outcome_not_a_crash(self) -> None:
        result = measure(self.frame(), pl.col("rank") > 99, name="none", horizon=1, cost_pct=0.25)
        assert result.n == 0
        assert result.p_value == 1.0


# ---------------------------------------------------------------------------
# Gates: registered values, and they must actually bind
# ---------------------------------------------------------------------------
class TestPreRegistration:
    def test_the_registered_constants_are_unchanged(self) -> None:
        """**The gate.** Editing this test is the only way to move a threshold,
        and it says in words not to."""
        assert MIN_POOLED_SAMPLE == 500
        assert ADVANTAGE_HIT_RATE_PP == 5.0
        assert ADVANTAGE_NET_RETURN_PP == 0.10
        assert STABILITY_FOLD_FRACTION == 0.75
        assert FDR_Q == 0.10
        assert MIN_HISTORY_BARS == 252
        assert [b[0] for b in SELECTIVITY_BUCKETS] == ["TOP_50", "TOP_25", "TOP_10", "TOP_5"]
        assert [b[0] for b in PULLBACK_BUCKETS] == [
            "NONE",
            "SHALLOW",
            "MEDIUM",
            "DEEP",
            "EXTREME",
        ]
        assert [b[0] for b in MOVEMENT_COST_BUCKETS] == [
            "UNECONOMIC",
            "MARGINAL",
            "ADEQUATE",
            "AMPLE",
        ]

    def test_bucket_edges_partition_without_gaps_or_overlap(self) -> None:
        for buckets in (PULLBACK_BUCKETS, MOVEMENT_COST_BUCKETS):
            for (_, _, upper), (_, lower, _) in pairwise(buckets):
                assert upper == lower
            assert buckets[-1][2] == math.inf

    def _advantage(self, **kwargs) -> Advantage:
        base = {
            "name": "x",
            "n": 1000,
            "days": 500,
            "hit_rate": 0.60,
            "control_hit_rate": 0.50,
            "gross_advantage_pp": 0.5,
            "net_absolute_pp": 0.2,
            "t_stat": 4.0,
            "p_value": 0.0001,
            "mean_mfe": 2.0,
            "mean_mae": -2.0,
        }
        base.update(kwargs)
        return Advantage(**base)  # type: ignore[arg-type]

    def test_a_small_sample_cannot_reach_a_verdict(self) -> None:
        result = self._advantage(n=100)
        assert classify(result, dict.fromkeys(range(6), 1.0)) is Verdict.NO_INFORMATION

    def test_an_effect_that_cannot_pay_for_execution_is_no_information(self) -> None:
        """Part O's rule: statistically interesting is not the same as tradable."""
        result = self._advantage(net_absolute_pp=-0.05)
        assert classify(result, dict.fromkeys(range(6), 1.0)) is Verdict.NO_INFORMATION

    def test_an_effect_present_in_only_some_folds_is_regime_dependent(self) -> None:
        folds = {2021: 1.0, 2022: -1.0, 2023: 1.0, 2024: -1.0, 2025: 1.0, 2026: 1.0}
        assert classify(self._advantage(), folds) is Verdict.REGIME_DEPENDENT

    def test_a_consistent_tradable_effect_reaches_robust(self) -> None:
        assert classify(self._advantage(), dict.fromkeys(range(6), 1.0)) is Verdict.ROBUST

    def test_either_advantage_route_is_sufficient(self) -> None:
        """Hit-rate separation OR net return, as registered."""
        by_hit = self._advantage(hit_rate=0.56, gross_advantage_pp=0.01)
        by_return = self._advantage(hit_rate=0.505, gross_advantage_pp=0.5)
        assert by_hit.passes_advantage
        assert by_return.passes_advantage
        assert not self._advantage(hit_rate=0.51, gross_advantage_pp=0.01).passes_advantage

    def test_stability_counts_sign_agreement_not_magnitude(self) -> None:
        assert stability({1: 5.0, 2: 0.01, 3: 9.0, 4: 0.5}) == 1.0
        assert stability({1: 1.0, 2: -1.0}) == 0.5


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_every_test_is_recorded_not_only_the_good_ones(self) -> None:
        """**The gate.** A correction whose denominator counts only survivors is
        arithmetic theatre."""
        registry = HypothesisRegistry()
        for index in range(20):
            registry.record("F", f"h{index}", 0.9)
        registry.record("F", "winner", 0.0001)
        assert len(registry) == 21
        assert registry.bonferroni() == pytest.approx(0.05 / 21)

    def test_benjamini_hochberg_returns_rejected_hypotheses_too(self) -> None:
        registry = HypothesisRegistry()
        registry.record("F", "strong", 0.0001)
        registry.record("F", "weak", 0.9)
        results = registry.benjamini_hochberg(0.10)
        assert len(results) == 2
        assert {name: kept for _, name, _, kept in results} == {
            "strong": True,
            "weak": False,
        }

    def test_a_lone_borderline_result_does_not_survive_many_tests(self) -> None:
        registry = HypothesisRegistry()
        registry.record("F", "borderline", 0.04)
        for index in range(99):
            registry.record("F", f"null{index}", 0.5 + index / 250)
        survivors = [e for e in registry.benjamini_hochberg(0.10) if e[3]]
        assert survivors == []


# ---------------------------------------------------------------------------
# Cost model reuse and options quarantine
# ---------------------------------------------------------------------------
def test_the_cost_model_is_the_canonical_one() -> None:
    """No simplified bps substitute: the flat fee has to be present."""
    source = inspect.getsource(phase12)
    assert "estimate_round_trip_cost" in source
    cheap = CostSettings(order_fee=0, variable_fee_rate=0, default_spread_bps=10)
    flat = CostSettings(order_fee=1, variable_fee_rate=0, default_spread_bps=10)
    assert round_trip_cost_pct(flat) > round_trip_cost_pct(cheap)


def test_options_data_is_quarantined_from_this_phase() -> None:
    """**The gate.** IV history is too short to conclude from, and a stray join
    would let it in silently."""
    source = inspect.getsource(phase12).lower()
    for forbidden in ("option_surface", "option_quote", "implied_volatility", "iv_30d"):
        assert forbidden not in source


def test_the_phase_places_no_orders_and_enables_nothing() -> None:
    source = inspect.getsource(phase12).lower()
    for forbidden in ("submit_order", "place_order", "tradingclient", "webhook"):
        assert forbidden not in source
