"""Production consumers must read adjusted bars, and keep reading them.

``FeatureService`` already defaulted to ``SPLIT_ADJUSTED``, which is why the
scanner, backtester, paper replay and API were safe through phase 9A. Two
consumers did not want features -- volatility-v1 and the #market-trends movers --
so they went straight to ``CandleRepository`` and straight past the adjustment.

These tests hold the seam. The first two are the ones that matter: if someone
re-imports ``CandleRepository`` into either module, they fail.
"""

from __future__ import annotations

import inspect

from app.domain.enums import PriceSeriesAdjustment
from app.features.service import DEFAULT_ADJUSTMENT as FEATURE_DEFAULT
from app.market_data import volatility_service
from app.market_data.adjusted import DEFAULT_ADJUSTMENT, AdjustedCandleReader, AdjustedSeries
from app.notifications import trends_service


def test_volatility_reads_through_the_adjustment_layer() -> None:
    """**The gate.** volatility-v1 must not see a split as a true range."""
    source = inspect.getsource(volatility_service)
    assert "AdjustedCandleReader" in source
    assert "CandleRepository" not in source


def test_market_trends_reads_through_the_adjustment_layer() -> None:
    """**The gate.** A split must not become a -75% "mover" in Discord."""
    source = inspect.getsource(trends_service)
    assert "AdjustedCandleReader" in source
    assert "CandleRepository" not in source


def test_the_default_matches_the_feature_service() -> None:
    """One canonical semantic. Two defaults that could disagree is not one."""
    assert DEFAULT_ADJUSTMENT is FEATURE_DEFAULT
    assert DEFAULT_ADJUSTMENT is PriceSeriesAdjustment.SPLIT_ADJUSTED


def test_an_empty_series_is_falsy_and_has_no_bars() -> None:
    """Callers treat "no history" as a per-symbol skip, not an error."""
    series = AdjustedSeries([], PriceSeriesAdjustment.SPLIT_ADJUSTED, 0)
    assert not series
    assert len(series) == 0


def test_the_reader_corroborates_before_adjusting() -> None:
    """**The regression.** Wiring production into the adjustment layer without
    a corroboration step applied HON's phantom 1-for-2 to live volatility --
    doubling every bar before 2026-06-29 and inventing exactly the discontinuity
    the layer exists to remove. Caught by a live log line, not by a test.
    """
    source = inspect.getsource(AdjustedCandleReader)
    assert "contradicted_actions" in source


def test_the_reader_accepts_an_as_of_for_replay() -> None:
    """A replay must not adjust a 2021 window with a 2024 split.

    Checked on the signature rather than by running a replay: the point is that
    the parameter exists and is keyword-only, so a caller cannot pass it
    positionally by accident and cannot silently omit it without meaning to.
    """
    signature = inspect.signature(AdjustedCandleReader.latest)
    as_of = signature.parameters["as_of"]
    assert as_of.kind is inspect.Parameter.KEYWORD_ONLY
    assert as_of.default is None
