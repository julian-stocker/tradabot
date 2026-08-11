"""Feature computation engine.

Takes a validated OHLCV frame, returns the same frame plus one column per
registered feature.

Contains **no database access and no I/O** (coding rule 11). It is a pure
function of its input frame, which is what makes both unit testing and the
no-look-ahead property test straightforward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import polars as pl

from app.core.errors import InsufficientDataError
from app.core.logging import get_logger
from app.domain.enums import Timeframe
from app.features.frame import CLOSE, OHLCV_COLUMNS, TIMESTAMP
from app.features.registry import FeatureSet, build_default_feature_set

logger = get_logger(__name__)

FeatureValues = dict[str, float | None]

_NON_FEATURE_COLUMNS: Final[frozenset[str]] = frozenset(OHLCV_COLUMNS)


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Feature values at a single point in time.

    The unit of exchange between the feature engine and the signal engine. Values
    may be ``None`` when a feature has not warmed up; consumers must handle that
    explicitly rather than coercing to 0.0, which would read as "neutral" and is
    a different claim from "unknown".
    """

    timestamp: datetime
    close: float
    values: FeatureValues
    bars_used: int

    def get(self, name: str) -> float | None:
        """Value of ``name``, or ``None`` if absent or not warmed up.

        Raises:
            KeyError: if ``name`` was never computed. Distinguishing "not
                computed" from "not warmed up" is worth the noise: the first is a
                wiring bug, the second is expected.
        """
        if name not in self.values:
            msg = f"feature {name!r} was not computed; available: {sorted(self.values)}"
            raise KeyError(msg)
        return self.values[name]

    def require(self, name: str) -> float:
        """Value of ``name``, raising if it is unavailable.

        Raises:
            InsufficientDataError: the feature exists but has not warmed up.
        """
        value = self.get(name)
        if value is None:
            raise InsufficientDataError(
                required=-1, available=self.bars_used, context=f"feature {name!r} not warmed up"
            )
        return value


class FeatureEngine:
    """Computes a :class:`FeatureSet` over an OHLCV frame.

    Args:
        feature_set: the features to compute. Defaults to the daily baseline set.

    Injected rather than constructed internally (coding rule 4) so tests can pass
    a two-feature set and the future scanner can pass a cheaper one.
    """

    def __init__(self, feature_set: FeatureSet | None = None) -> None:
        self._features = feature_set or build_default_feature_set()

    @classmethod
    def for_timeframe(cls, timeframe: Timeframe) -> FeatureEngine:
        """Engine with the baseline set annualised for ``timeframe``."""
        return cls(build_default_feature_set(timeframe))

    @property
    def feature_set(self) -> FeatureSet:
        return self._features

    @property
    def warmup_bars(self) -> int:
        """Bars required before every feature is non-null."""
        return self._features.warmup_bars

    def compute(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Return ``frame`` with one column appended per feature.

        The input frame must already be validated and sorted -- use
        :func:`app.features.frame.candles_to_frame`. Row order is preserved, so
        row *i* of the output corresponds to bar *i* of the input.

        Short frames are **not** an error here: the features simply stay null.
        Refusing would prevent a caller from charting what history it does have.
        Callers that need a warmed-up value use :meth:`snapshot`, which does
        raise.
        """
        if frame.height == 0:
            return frame
        return frame.with_columns(self._features.expressions())

    def snapshot(self, frame: pl.DataFrame, *, index: int = -1) -> FeatureSnapshot:
        """Feature values at a single bar.

        Args:
            frame: raw OHLCV frame; features are computed here.
            index: bar to snapshot. ``-1`` (default) is the most recent bar.

        Only data up to and including ``index`` influences the result -- that is a
        property of the indicators themselves, not of any slicing done here.

        Raises:
            InsufficientDataError: fewer bars than the set's warm-up requirement.
            IndexError: ``index`` is out of range.
        """
        if frame.height < self.warmup_bars:
            raise InsufficientDataError(
                required=self.warmup_bars,
                available=frame.height,
                context="feature snapshot",
            )

        computed = self.compute(frame)
        row = computed.row(index, named=True)

        values: FeatureValues = {
            name: _as_optional_float(row[name]) for name in self._features.names()
        }
        return FeatureSnapshot(
            timestamp=row[TIMESTAMP],
            close=float(row[CLOSE]),
            values=values,
            bars_used=frame.height if index == -1 else index + 1,
        )

    def feature_columns(self, computed: pl.DataFrame) -> list[str]:
        """Names of the feature columns present in a computed frame."""
        return [c for c in computed.columns if c not in _NON_FEATURE_COLUMNS]


def _as_optional_float(value: object) -> float | None:
    """Coerce a Polars cell to ``float | None``.

    NaN is mapped to ``None``: it means "undefined here", the same thing a null
    means, and letting a NaN escape into a weighted score silently turns the whole
    score into NaN.
    """
    if value is None:
        return None
    number = float(value)  # type: ignore[arg-type]
    if math.isnan(number) or math.isinf(number):
        return None
    return number
