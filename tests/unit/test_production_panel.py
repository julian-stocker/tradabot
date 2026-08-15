"""The production panel must see today, and must never see tomorrow.

Two failures matter here and they pull in opposite directions: requiring future
bars makes the newest session unevaluable (the Phase 12.8 blocker), and reading
future bars makes every candidate worthless. These tests pin both edges.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.research.phase12 import bars_above, causal_features
from app.strategy import panel
from app.strategy.panel import PRODUCTION_MIN_HISTORY, production_eligible

T0 = datetime(2024, 1, 2, tzinfo=UTC)

CAUSAL_COLUMNS = ("ret_20d", "ema20", "atr14", "atr_pct", "trend_dist_atr", "bars_above_ema20")


def series(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(closes),
            "timestamp": [T0 + timedelta(days=i) for i in range(len(closes))],
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * len(closes),
        }
    )


# ---------------------------------------------------------------------------
# A: production carries no forward information
# ---------------------------------------------------------------------------
class TestNoForwardTargets:
    def test_the_production_panel_never_calls_forward_target_construction(self) -> None:
        """**The gate.** The one function that reads bars after t."""
        source = inspect.getsource(panel).split('"""', 2)[-1]
        assert "forward_targets" not in source

    def test_no_forward_column_is_referenced(self) -> None:
        source = inspect.getsource(panel).split('"""', 2)[-1]
        for forbidden in ("fwd_", "mfe_", "mae_", "entry_price", "median_fwd"):
            assert forbidden not in source

    def test_production_eligibility_requires_no_future_bar(self) -> None:
        """Everything retained must be knowable at the session close."""
        source = inspect.getsource(production_eligible)
        for forbidden in ("fwd_", "entry_price", "shift(-"):
            assert forbidden not in source

    def test_feature_formulas_are_imported_not_duplicated(self) -> None:
        """One canonical causal implementation; research layers targets on it."""
        source = inspect.getsource(panel)
        assert "from app.research.phase12 import" in source
        assert "def causal_features" not in source
        assert "def cross_sectional" not in source


# ---------------------------------------------------------------------------
# B: prefix invariance — adding future data cannot change a past decision
# ---------------------------------------------------------------------------
class TestPrefixInvariance:
    def test_causal_features_are_identical_with_and_without_later_history(self) -> None:
        """**The gate.** A production candidate for session t must be invariant
        to every bar that arrives after t."""
        closes = [100.0 + (i % 7) - 3 + i * 0.5 for i in range(400)]
        full = bars_above(causal_features(series(closes)))
        prefix = bars_above(causal_features(series(closes[:300])))
        for column in CAUSAL_COLUMNS:
            a = full[column][:300].to_list()
            b = prefix[column].to_list()
            for x, y in zip(a, b, strict=True):
                if x is None or y is None:
                    assert x is None
                    assert y is None
                else:
                    assert x == pytest.approx(y)

    def test_truncating_the_future_does_not_change_eligibility_of_past_rows(self) -> None:
        closes = [100.0 + i * 0.3 for i in range(400)]
        full = bars_above(causal_features(series(closes))).with_columns(
            pl.arange(0, pl.len()).alias("bar_index"),
            pl.lit(1.0).alias("rel_mom_market_20d"),
            pl.lit(1.0).alias("sector_etf_ret_20d"),
            pl.lit("technology").alias("sector"),
        )
        prefix = full.head(320)
        assert (
            production_eligible(full).head(320 - PRODUCTION_MIN_HISTORY).height
            == production_eligible(prefix).height
        )

    def test_the_newest_row_survives_production_eligibility(self) -> None:
        """The Phase 12.8 blocker, inverted: research would drop this row."""
        closes = [100.0 + i * 0.2 for i in range(300)]
        frame = bars_above(causal_features(series(closes))).with_columns(
            pl.arange(0, pl.len()).alias("bar_index"),
            pl.lit(1.0).alias("rel_mom_market_20d"),
            pl.lit(1.0).alias("sector_etf_ret_20d"),
            pl.lit("technology").alias("sector"),
        )
        eligible = production_eligible(frame)
        assert eligible["timestamp"].max() == frame["timestamp"].max()

    def test_a_missing_sector_benchmark_correctly_drops_the_row(self) -> None:
        """MATCH_B gates on the sector ETF; evaluating without it would be a
        different rule. This is why the newest session can lag the newest bar."""
        closes = [100.0 + i * 0.2 for i in range(300)]
        frame = bars_above(causal_features(series(closes))).with_columns(
            pl.arange(0, pl.len()).alias("bar_index"),
            pl.lit(1.0).alias("rel_mom_market_20d"),
            pl.lit(None, dtype=pl.Float64).alias("sector_etf_ret_20d"),
            pl.lit("technology").alias("sector"),
        )
        assert production_eligible(frame).height == 0
