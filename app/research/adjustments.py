"""Split adjustment for the research candle frames.

Why this exists separately from ``app.corporate_actions.adjust``
---------------------------------------------------------------
That module is the authority on the rule and stays the authority: it works in
``Decimal`` over ``CorporateAction`` objects, which is right for a signal the
user will act on and far too slow for the 559,080-row hourly frame this package
loads in one go. This module applies **the same rule** in polars over float64.

The two are pinned to each other by test, not by comment:
``tests/unit/test_research_adjustments.py`` generates random split schedules and
asserts these factors equal ``cumulative_split_factors`` exactly. If the rule
ever changes in one place, that test fails rather than the two silently
diverging.

The defect this closes
----------------------
Bars are stored RAW on purpose -- the provider is asked for unadjusted prices
because tradabot adjusts on read. But the research loader read ``candles``
directly and never adjusted, so eleven split bars entered the feature set as
genuine returns:

    AAPL  2020-08-31   499.35 -> 125.34   (-75%)
    AMZN  2022-06-06  2446.41 -> 124.78   (-95%)
    GOOGL 2022-07-18  2235.49 -> 113.26   (-95%)
    NVDA  2024-06-10  1208.42 -> 120.50   (-90%)
    GE    2021-08-02    12.96 -> 102.71   (+693%, a 1-for-8 reverse split)

``ret_1d_pct`` feeds ``market_proxy``, so each of those also moved the
equal-weight market proxy by roughly 1/52nd of its size on that bar -- in the
one feature family phase 6 reported above its 5pp floor.

Is back-adjustment causal?
--------------------------
Rescaling past prices when a *future* split occurs looks like leakage, and the
question deserves a real answer rather than a reassurance.

It is not leakage **here**, because every feature built on these prices is
scale-invariant: returns, percentage distances from moving averages, ATR as a
percentage of price, volume relative to its own rolling mean. Multiplying a
whole trailing window by a constant leaves all of them unchanged. The factor
only stops being constant *across* a split boundary, and there it is removing a
discontinuity that was never a price move.

The one thing that would break this is a feature using an absolute price level
-- "is it above 100 dollars". There is none, and
``test_a_future_split_does_not_move_any_earlier_feature`` proves the property
directly rather than asserting it: adjusting with a split dated after the window
leaves every feature value in that window identical.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import timedelta
from typing import Final

import polars as pl

from app.core.logging import get_logger

logger = get_logger(__name__)

_PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

MAX_CORROBORATION_GAP: Final = timedelta(days=10)
"""How far apart the bars bracketing a split may be and still arbitrate it.

Ten days spans a long holiday weekend plus a halt, and nothing legitimate in a
daily series exceeds it. Beyond this the ratio between two bars measures price
drift rather than the share-count change, which is precisely how NVDA's real
10-for-1 came to be rejected across a 557-day hole.
"""

CORROBORATION_BAND: Final = math.log(1.35)
"""How far the observed price jump may sit from the declared ratio, in log space.

Generous on purpose. TSLA's 2020 5-for-1 shows an observed ratio of 4.44 because
the stock genuinely moved 12% that session, and a tight band would reject a real
split. The band only has to exclude *non-events*, and
:func:`corroborated_splits` additionally requires the declared ratio to explain
the jump better than "no split at all" -- which is the test that actually does
the work.
"""

SPLIT_QUERY: Final = """
    SELECT instrument_id, effective_at,
           CAST(to_shares AS REAL) / CAST(from_shares AS REAL) AS ratio
    FROM corporate_actions
    WHERE action_type = 'SPLIT'
      AND from_shares IS NOT NULL AND to_shares IS NOT NULL
      AND CAST(from_shares AS REAL) > 0
    ORDER BY instrument_id, effective_at
"""
"""Splits only.

Dividends are excluded deliberately, matching ``cumulative_split_factors``: a
cash dividend lowers the price by roughly its amount, but correcting for that
means choosing a reinvestment price and timing, and a wrong choice silently
biases every return. ``docs/data-adjustments.md`` records why TOTAL_RETURN is
unimplemented rather than approximated.
"""


def load_splits(connection: sqlite3.Connection) -> pl.DataFrame:
    """Every stored split, as ``instrument_id, effective_at, ratio``.

    Returns an empty, correctly-typed frame when the table holds no splits, so
    callers need no special case for a fresh database.
    """
    splits = pl.read_database(SPLIT_QUERY, connection)
    if splits.is_empty():
        return pl.DataFrame(
            schema={"instrument_id": pl.Int64, "effective_at": pl.Datetime, "ratio": pl.Float64}
        )
    return splits.with_columns(
        pl.col("effective_at").str.to_datetime(strict=False),
        pl.col("ratio").cast(pl.Float64),
    ).sort(["instrument_id", "effective_at"])


def corroborated_splits(candles: pl.DataFrame, splits: pl.DataFrame) -> pl.DataFrame:
    """Keep only splits the price series itself confirms.

    A stored action the prices do not show is not harmless. Adjusting for it
    multiplies every earlier bar by a factor nothing offsets, **creating** a
    discontinuity where none existed -- the exact inverse of the defect this
    module fixes. The real case from this database:

    * ``HON 2026-06-29`` is reported as a 1-for-2 reverse split. The observed
      ratio across that date is 1.02: nothing happened to the price. Applying it
      would invent a +100% jump.

    The test is scale-free: the declared ratio must explain the observed jump
    better than "no split" does, and land within :data:`CORROBORATION_BAND`. A
    percentage threshold would have to be loose enough for TSLA's 5-for-1 on a
    12%-move day, which is loose enough to admit HON.

    "I cannot check" is not "it is wrong"
    ------------------------------------
    Phase 9A rejected any split whose bracketing bars disagreed, and phase 9B
    found that rule inverting itself. NVDA's daily series was missing
    2023-12-29 through 2025-07-08, so the bars either side of its genuine
    10-for-1 straddled a 557-day hole and showed 3.10 -- and the split was
    dropped, leaving every pre-2024 NVDA daily price ten times too high. The
    provider was right and the check was wrong.

    So a gap wider than :data:`MAX_CORROBORATION_GAP` now yields
    ``INDETERMINATE``, and an indeterminate split is **applied**. The provider is
    the authority on whether a corporate action happened; corroboration exists to
    catch a provider *error*, and missing local data is no evidence of one. The
    decision is logged either way, and ``market-data verify-adjustments`` reports
    the same situation as ``CONTRADICTED`` so a human sees it.
    """
    if splits.is_empty() or candles.is_empty():
        return splits

    frame = candles.sort("timestamp")
    timestamps = frame["timestamp"].to_list()
    closes = frame["close"].cast(pl.Float64).to_list()

    kept: list[bool] = []
    for row in splits.iter_rows(named=True):
        effective = row["effective_at"]
        ratio = float(row["ratio"])

        after = next((i for i, ts in enumerate(timestamps) if ts >= effective), None)
        if after is None or after == 0 or closes[after] in (None, 0):
            # No bars either side at all. Nothing to contradict, and nothing the
            # adjustment would visibly change -- applied, and logged.
            logger.info(
                "split indeterminate: no bars bracket it; applying provider action",
                effective_at=str(effective),
                ratio=ratio,
            )
            kept.append(True)
            continue

        span = timestamps[after] - timestamps[after - 1]
        if span > MAX_CORROBORATION_GAP:
            logger.info(
                "split indeterminate: bracketing bars straddle a data gap; "
                "applying provider action",
                effective_at=str(effective),
                ratio=ratio,
                gap_days=span.days,
            )
            kept.append(True)
            continue

        observed = closes[after - 1] / closes[after]
        residual = abs(math.log(observed / ratio))
        identity = abs(math.log(observed))
        ok = residual < CORROBORATION_BAND and residual < identity
        if not ok:
            logger.warning(
                "split contradicted by the price series; skipping",
                effective_at=str(effective),
                declared_ratio=ratio,
                observed_ratio=round(observed, 4),
            )
        kept.append(ok)

    return splits.filter(pl.Series(kept))


def adjust_for_splits(candles: pl.DataFrame, splits: pl.DataFrame) -> pl.DataFrame:
    """Back-adjust one instrument's bars for its splits.

    Args:
        candles: one instrument's raw bars, ascending by ``timestamp``, with
            float-castable ``open, high, low, close, volume``.
        splits: rows for **this instrument only**, columns ``effective_at`` and
            ``ratio`` (``to_shares / from_shares``; 4.0 for a 4-for-1, 0.125 for
            a 1-for-8 reverse split).

    Returns the frame with prices divided and volume multiplied by the
    cumulative factor of every split effective strictly after each bar. Bars at
    or after the last split are untouched, so the series still ends on the real
    traded price a reader can check against a broker screen.
    """
    if splits.is_empty() or candles.is_empty():
        return candles

    price_factor = pl.lit(1.0, dtype=pl.Float64)
    volume_factor = pl.lit(1.0, dtype=pl.Float64)

    # Strictly-after, matching `cumulative_split_factors`. A bar stamped exactly
    # at the effective instant is already trading post-split and must not be
    # adjusted -- that boundary is the whole difference between a correct series
    # and one that is off by one bar at every split.
    for row in splits.sort("effective_at").iter_rows(named=True):
        before = pl.col("timestamp") < pl.lit(row["effective_at"])
        ratio = float(row["ratio"])
        price_factor = pl.when(before).then(price_factor / ratio).otherwise(price_factor)
        volume_factor = pl.when(before).then(volume_factor * ratio).otherwise(volume_factor)

    return candles.with_columns(
        *[(pl.col(name).cast(pl.Float64) * price_factor).alias(name) for name in _PRICE_COLUMNS],
        (pl.col("volume").cast(pl.Float64) * volume_factor).alias("volume"),
    )


def adjust_all(
    candles: pl.DataFrame, splits: pl.DataFrame, *, corroborate: bool = True
) -> pl.DataFrame:
    """Back-adjust a multi-instrument frame, one instrument at a time.

    Instruments with no split pass through untouched rather than being rebuilt,
    which keeps the common case free.

    ``corroborate`` runs :func:`corroborated_splits` per instrument first, using
    the very bars about to be adjusted. Leave it on: the check is what stops a
    mis-reported action from inventing a jump, and it is series-specific, so it
    must run here rather than once at load time.
    """
    if splits.is_empty():
        return candles

    affected = set(splits["instrument_id"].to_list())
    parts: list[pl.DataFrame] = []
    for key, group in candles.group_by("instrument_id", maintain_order=True):
        instrument_id = key[0]
        if instrument_id not in affected:
            parts.append(group)
            continue
        mine = splits.filter(pl.col("instrument_id") == instrument_id)
        if corroborate:
            mine = corroborated_splits(group, mine)
        parts.append(adjust_for_splits(group, mine))
    return pl.concat(parts, how="vertical_relaxed")
