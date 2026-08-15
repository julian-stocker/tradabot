"""The causal feature panel production infers from. **No forward targets.**

The blocker this removes
------------------------
``app.research.phase12.build_dataset`` ends with an eligibility filter requiring
a *complete forward window* — ``fwd_5d`` must be non-null, and ``entry_price`` is
the next session's open. Both are correct for research, which cannot measure an
outcome it does not have. Both are fatal in production, which is asked about
today.

Phase 12.8 measured the consequence: stored bars ran through 2026-08-14 while the
newest evaluable session was 2026-08-07 — a **structural one-week lag**, so the
candidate service could never see the session it was being asked about.

What is shared, and what is not
-------------------------------
Feature formulas are **not** duplicated. ``causal_features``, ``bars_above``,
``attach_context`` and ``cross_sectional`` are imported unchanged from the
research module, because they are already causal by construction — every rolling
window ends at the current bar. Research then layers its target builder on top;
production never calls it, and a test asserts this module cannot reference a
forward column.

What production drops
---------------------
Only two things, and both are future information:

* the forward-window requirement (``fwd_5d`` non-null);
* ``entry_price``, which is *tomorrow's* open.

The candidate's reference price is the session **close**, which is known at t.
The real entry price is discovered when the next session opens, which is exactly
how the paper engine already behaves.
"""

from __future__ import annotations

import sqlite3
from typing import Final

import polars as pl

from app.research.phase12 import (
    BREAKOUT_VOLUME_RATIO,
    CONTINUATION_MAX_PULLBACK_ATR,
    MIN_HISTORY_BARS,
    attach_context,
    bars_above,
    causal_features,
    cross_sectional,
    load_daily,
    round_trip_cost_pct,
)

PRODUCTION_MIN_HISTORY: Final = MIN_HISTORY_BARS
"""Unchanged from research. A symbol that cannot be ranked against its own year
of history is not eligible, in either context."""


def production_eligible(frame: pl.DataFrame) -> pl.DataFrame:
    """Rows that may enter today's cross-section.

    Deliberately the research eligibility rule **minus every clause that needs a
    bar after t**. Everything retained is knowable at the session close:
    sufficient history, a usable ATR, a positive close, market and sector
    context, and a sector label.

    A row is *not* required to have an entry price or a forward window, because
    neither exists yet and requiring them is what caused the one-week lag.
    """
    return frame.filter(
        pl.col("bar_index").ge(PRODUCTION_MIN_HISTORY)
        & pl.col("atr14").is_not_null()
        & pl.col("atr14").gt(0)
        & pl.col("close").gt(0)
        & pl.col("rel_mom_market_20d").is_not_null()
        & pl.col("sector_etf_ret_20d").is_not_null()
        & pl.col("sector").is_not_null()
    )


def build_production_panel(
    connection: sqlite3.Connection, *, cost_pct: float | None = None
) -> pl.DataFrame:
    """The causal panel, up to and including the newest complete session.

    Identical to the research construction except that the research target
    builder is never applied and the forward-window filter is never used. Split adjustment
    still runs first: a split left in the series is a -75% bar that every
    rolling window would read as a price move.

    Args:
        connection: read-only handle to the stored candles.
        cost_pct: round-trip cost as a percentage of notional, for
            ``movement_to_cost``. Defaults to the canonical paper cost model.
    """
    candles, sectors = load_daily(connection)

    parts = []
    for _, group in candles.group_by("instrument_id", maintain_order=True):
        ordered = group.sort("timestamp")
        featured = bars_above(causal_features(ordered))
        parts.append(featured.with_columns(pl.arange(0, pl.len()).alias("bar_index")))
    frame = pl.concat(parts, how="vertical_relaxed")

    frame = attach_context(frame, sectors)
    frame = cross_sectional(frame)

    cost = cost_pct if cost_pct is not None else _default_cost_pct()
    frame = frame.with_columns(
        (
            (pl.col("ret_1d") > 0)
            & (pl.col("ret_5d") > 0)
            & (pl.col("pullback_atr") < CONTINUATION_MAX_PULLBACK_ATR)
        ).alias("continuation"),
        (pl.col("breakout_20d") & (pl.col("volume_ratio") > BREAKOUT_VOLUME_RATIO)).alias(
            "breakout_volume"
        ),
        pl.col("timestamp").dt.year().alias("year"),
        (pl.col("atr_pct") / cost).alias("movement_to_cost"),
    )
    return production_eligible(frame)


def _default_cost_pct() -> float:
    """The canonical paper cost model, never a bps substitute."""
    from app.simulation.defaults import build_default_profiles  # noqa: PLC0415

    profile = next(p for p in build_default_profiles() if p.name == "5000eur-balanced")
    return round_trip_cost_pct(profile.costs.to_cost_settings())


def newest_evaluable_session(frame: pl.DataFrame) -> pl.Series | None:
    """The most recent session the panel can actually answer about."""
    if frame.height == 0:
        return None
    return frame["timestamp"].max()  # type: ignore[return-value]
