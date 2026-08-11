"""Candles to Polars frames -- the single Decimal/float boundary.

Everything upstream of this module (database, providers, API) uses
:class:`~decimal.Decimal`, because rounding money is not acceptable. Everything
downstream (indicators, scoring) uses ``float64``, because a 200-period EMA over
``Decimal`` is both slow and pointless -- an indicator is a statistic, not an
amount of money.

Converting in exactly one place means there is exactly one place to audit.

This module deliberately knows nothing about SQLAlchemy (coding rule 11). It
accepts anything matching :class:`CandleLike`, which both the ORM model and the
provider DTO satisfy structurally.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

import polars as pl

from app.core.errors import ValidationError

TIMESTAMP = "timestamp"
OPEN = "open"
HIGH = "high"
LOW = "low"
CLOSE = "close"
VOLUME = "volume"

OHLCV_COLUMNS: tuple[str, ...] = (TIMESTAMP, OPEN, HIGH, LOW, CLOSE, VOLUME)

FRAME_SCHEMA: dict[str, pl.DataType] = {
    TIMESTAMP: pl.Datetime(time_unit="us", time_zone="UTC"),
    OPEN: pl.Float64(),
    HIGH: pl.Float64(),
    LOW: pl.Float64(),
    CLOSE: pl.Float64(),
    VOLUME: pl.Float64(),
}


@runtime_checkable
class CandleLike(Protocol):
    """Structural type for anything shaped like an OHLCV bar.

    Members are declared as read-only properties rather than attributes. A
    Protocol with mutable attributes is only satisfied by types whose attributes
    are also mutable, which would exclude frozen dataclasses -- and
    :class:`~app.corporate_actions.adjust.AdjustedCandle` is deliberately frozen.
    Read-only members accept both, and this module never writes to a candle
    anyway.
    """

    @property
    def timestamp(self) -> datetime: ...

    @property
    def open(self) -> Decimal: ...

    @property
    def high(self) -> Decimal: ...

    @property
    def low(self) -> Decimal: ...

    @property
    def close(self) -> Decimal: ...

    @property
    def volume(self) -> Decimal: ...


def candles_to_frame(candles: Sequence[CandleLike]) -> pl.DataFrame:
    """Build a validated, chronologically sorted OHLCV frame.

    Validation is not defensive noise -- each check prevents a specific class of
    silent wrongness:

    * **Strictly increasing timestamps.** Every indicator is a trailing-window
      calculation. On unsorted input, "trailing" stops meaning "past" and the
      whole no-look-ahead guarantee evaporates without raising anything.
    * **No duplicate timestamps.** A duplicated bar double-counts in every
      rolling window and biases volume features upward.

    Raises:
        ValidationError: on duplicate or out-of-order timestamps.
    """
    if not candles:
        return pl.DataFrame(schema=FRAME_SCHEMA)

    frame = pl.DataFrame(
        {
            TIMESTAMP: [c.timestamp for c in candles],
            OPEN: [float(c.open) for c in candles],
            HIGH: [float(c.high) for c in candles],
            LOW: [float(c.low) for c in candles],
            CLOSE: [float(c.close) for c in candles],
            VOLUME: [float(c.volume) for c in candles],
        },
        schema=FRAME_SCHEMA,
    )
    _assert_chronological(frame)
    return frame


def _assert_chronological(frame: pl.DataFrame) -> None:
    """Reject frames that are not strictly increasing in time."""
    stamps = frame.get_column(TIMESTAMP)
    if stamps.len() < 2:  # noqa: PLR2004 -- a single bar is trivially ordered
        return

    # Compare as integer microseconds: Polars Duration series do not support
    # ordering comparisons against a scalar.
    deltas = stamps.diff().drop_nulls().dt.total_microseconds()
    duplicates = int((deltas == 0).sum())
    out_of_order = int((deltas < 0).sum())
    if duplicates == 0 and out_of_order == 0:
        return

    if duplicates:
        msg = f"candle series contains {duplicates} duplicate timestamp(s)"
    else:
        msg = f"candle series is not sorted ascending by timestamp ({out_of_order} inversion(s))"
    raise ValidationError(msg)


def frame_is_empty(frame: pl.DataFrame) -> bool:
    return frame.height == 0
