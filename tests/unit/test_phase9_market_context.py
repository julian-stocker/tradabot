"""Phase 9A analysis: the comparison must be fair before its answer means anything.

These tests do not check that real context *works* -- that is what the phase
measures, and a test asserting an outcome would be assuming the result. They
check the machinery that makes the measurement trustworthy: that proxy and real
features are compared on identical rows, that the real reference is genuinely
external to the universe, and that the regime split is causal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.market_data.benchmarks import (
    BENCHMARK_SYMBOLS,
    SECTOR_BENCHMARKS,
    is_benchmark,
    sector_benchmark,
)
from app.research.featureset import attach_real_context, per_symbol_features
from app.research.phase9 import (
    PROXY_VERSUS_REAL,
    analyse_real_regime,
    compare_proxy_and_real,
    measure_adjustment_impact,
    verdict_summary,
)

START = datetime(2021, 1, 4, 14, 0, tzinfo=UTC)


def outcome_frame(n: int, *, seed: int = 0) -> pl.DataFrame:
    """Rows shaped like a loaded phase-6 dataset, with both context families."""
    rng = __import__("random").Random(seed)
    timestamps = [START + timedelta(hours=i) for i in range(n)]
    proxy = [rng.gauss(0, 1) for _ in range(n)]
    return pl.DataFrame(
        {
            "instrument_id": [1] * n,
            "evaluation_id": list(range(n)),
            "symbol": ["AAPL"] * n,
            "direction": ["LONG"] * n,
            "timestamp": timestamps,
            "evaluated_ts": timestamps,
            "raw_return": [rng.gauss(0, 2) for _ in range(n)],
            "mfe": [abs(rng.gauss(1, 1)) for _ in range(n)],
            "mae": [-abs(rng.gauss(1, 1)) for _ in range(n)],
            "proxy_ret_1d_pct": proxy,
            # Correlated with the proxy but not identical, as a real index is.
            "index_ret_1d_pct": [p * 0.9 + rng.gauss(0, 0.3) for p in proxy],
            "rel_strength_market_pct": [rng.gauss(0, 1) for _ in range(n)],
            "relative_strength_market_1d": [rng.gauss(0, 1) for _ in range(n)],
            "sector_ret_1d_pct": [rng.gauss(0, 1) for _ in range(n)],
            "sector_etf_ret_1d_pct": [rng.gauss(0, 1) for _ in range(n)],
            "rel_strength_sector_pct": [rng.gauss(0, 1) for _ in range(n)],
            "relative_strength_sector_1d": [rng.gauss(0, 1) for _ in range(n)],
        }
    )


# ---------------------------------------------------------------------------
# The reference must be outside the universe
# ---------------------------------------------------------------------------
def test_every_benchmark_is_recognised_as_one() -> None:
    """``is_benchmark`` is the filter that keeps SPY out of its own average."""
    for symbol in BENCHMARK_SYMBOLS:
        assert is_benchmark(symbol)
        assert is_benchmark(symbol.lower())


def test_watchlist_names_are_not_benchmarks() -> None:
    for symbol in ("AAPL", "NVDA", "JPM", "XOM"):
        assert not is_benchmark(symbol)


def test_every_sector_tag_maps_to_a_distinct_fund() -> None:
    """One fund per sector, and no fund doing double duty.

    Reusing XLK for both ``technology`` and ``semiconductors`` would make
    sector relative strength nearly identical across two thirds of the universe.
    """
    funds = [b.symbol for b in SECTOR_BENCHMARKS]
    assert len(funds) == len(set(funds))
    assert sector_benchmark("semiconductors").symbol != sector_benchmark("technology").symbol


def test_an_unknown_sector_tag_raises_rather_than_returning_nothing() -> None:
    with pytest.raises(KeyError, match="no sector benchmark"):
        sector_benchmark("biotech-moonshots")


# ---------------------------------------------------------------------------
# Part B fairness
# ---------------------------------------------------------------------------
def test_pairs_are_compared_on_identical_rows() -> None:
    """A proxy measured on rows the real reference never saw is a different study."""
    frame = outcome_frame(600)
    # Punch a hole in the real series only, as a halted ETF would.
    holed = frame.with_columns(
        pl.when(pl.col("evaluation_id") < 200)
        .then(None)
        .otherwise(pl.col("index_ret_1d_pct"))
        .alias("index_ret_1d_pct")
    )

    comparisons = compare_proxy_and_real(holed, horizon="1d", stream="test")
    pair = next(c for c in comparisons if c.real_feature == "index_ret_1d_pct")

    assert pair.proxy is not None
    assert pair.real is not None
    proxy_n = sum(b.episodes for b in pair.proxy.buckets)
    real_n = sum(b.episodes for b in pair.real.buckets)
    assert proxy_n == real_n


def test_every_declared_pair_is_attempted() -> None:
    comparisons = compare_proxy_and_real(outcome_frame(600), horizon="1d", stream="test")
    assert {(c.proxy_feature, c.real_feature) for c in comparisons} == set(PROXY_VERSUS_REAL)


def test_correlation_is_reported_for_each_pair() -> None:
    """The number that decides whether a verdict difference is meaningful."""
    comparisons = compare_proxy_and_real(outcome_frame(600), horizon="1d", stream="test")
    pair = next(c for c in comparisons if c.real_feature == "index_ret_1d_pct")
    assert pair.correlation is not None
    assert pair.correlation > 0.8


def test_a_missing_pair_is_skipped_not_faked() -> None:
    frame = outcome_frame(400).drop("sector_etf_ret_1d_pct")
    comparisons = compare_proxy_and_real(frame, horizon="1d", stream="test")
    assert all(c.real_feature != "sector_etf_ret_1d_pct" for c in comparisons)


# ---------------------------------------------------------------------------
# Part C
# ---------------------------------------------------------------------------
def test_adjustment_impact_reports_the_shrinking_tail() -> None:
    raw = pl.DataFrame({"ret_1d_pct": [-95.0, 1.0, 2.0, -3.0]})
    adjusted = pl.DataFrame({"ret_1d_pct": [-2.0, 1.0, 2.0, -3.0]})

    impact = measure_adjustment_impact(raw, adjusted, feature="ret_1d_pct", stream="test")

    assert impact is not None
    assert impact.raw_min == pytest.approx(-95.0)
    assert impact.adjusted_min == pytest.approx(-3.0)
    assert impact.rows_changed == 1
    assert impact.extreme_shrinkage == pytest.approx(3.0 / 95.0)


def test_adjustment_impact_on_an_untouched_feature_shows_no_change() -> None:
    same = pl.DataFrame({"rsi14": [40.0, 55.0, 60.0]})
    impact = measure_adjustment_impact(same, same, feature="rsi14", stream="test")

    assert impact is not None
    assert impact.rows_changed == 0
    assert impact.extreme_shrinkage == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Part D
# ---------------------------------------------------------------------------
def test_regime_states_partition_the_rows() -> None:
    """The 2x2 must be exhaustive and non-overlapping: every row lands once.

    One symbol per row, so each row survives the episode collapse as its own
    episode and the counts are directly comparable to ``n``. Pooling them under
    a single symbol would collapse each bucket to one episode and make the
    partition property untestable.
    """
    n = 800
    rng = __import__("random").Random(7)
    frame = pl.DataFrame(
        {
            "instrument_id": [1] * n,
            "evaluation_id": list(range(n)),
            "symbol": [f"S{i}" for i in range(n)],
            "direction": ["LONG"] * n,
            "timestamp": [START + timedelta(hours=i) for i in range(n)],
            "index_above_ema50": [rng.random() > 0.5 for _ in range(n)],
            "index_ret_1d_pct": [rng.gauss(0, 1) for _ in range(n)],
            "sector_etf_ret_1d_pct": [rng.gauss(0, 1) for _ in range(n)],
            "raw_return": [rng.gauss(0, 2) for _ in range(n)],
        }
    )

    buckets = analyse_real_regime(frame)
    assert len(buckets) == 4
    assert sum(b.observations for b in buckets) == n


def test_regime_returns_nothing_when_real_context_is_absent() -> None:
    """Better an empty result than a 2x2 built from whatever columns existed."""
    frame = pl.DataFrame({"raw_return": [1.0, 2.0], "instrument_id": [1, 1]})
    assert analyse_real_regime(frame) == []


# ---------------------------------------------------------------------------
# Causality of the joined context
# ---------------------------------------------------------------------------
def test_real_context_at_a_row_does_not_use_a_later_benchmark_bar() -> None:
    """Appending a violent future SPY bar must not move earlier context."""
    n = 300
    stamps = [START + timedelta(hours=i) for i in range(n)]
    closes = [100.0 + i * 0.2 for i in range(n)]

    def series(symbol: str, prices: list[float]) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "instrument_id": [1 if symbol != "SPY" else 99] * len(prices),
                "symbol": [symbol] * len(prices),
                "timestamp": stamps[: len(prices)],
                "open": [p * 0.999 for p in prices],
                "high": [p * 1.003 for p in prices],
                "low": [p * 0.997 for p in prices],
                "close": prices,
                "volume": [1_000_000.0] * len(prices),
            }
        )

    window = 250
    stock = per_symbol_features(series("AAPL", closes)).head(window)

    calm = per_symbol_features(series("SPY", closes))
    shocked = per_symbol_features(series("SPY", [*closes[: n - 1], closes[-1] * 3.0]))

    baseline = attach_real_context(stock, calm.head(window))
    disturbed = attach_real_context(stock, shocked.head(window))

    for column in ("index_ret_1d_pct", "relative_strength_market_1d"):
        assert baseline[column].to_list() == pytest.approx(disturbed[column].to_list(), nan_ok=True)


def test_verdict_summary_counts_each_verdict() -> None:
    class Stub:
        def __init__(self, verdict: str) -> None:
            self.verdict = verdict

    results = [Stub("NO_INFORMATION"), Stub("NO_INFORMATION"), Stub("REGIME_DEPENDENT")]
    assert verdict_summary(results) == {  # type: ignore[arg-type]
        "NO_INFORMATION": 2,
        "REGIME_DEPENDENT": 1,
    }
