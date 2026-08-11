"""Feature registry.

A feature is a *named, documented, warm-up-aware* column derived from OHLCV data.
Registering one is deliberately cheap -- a :class:`FeatureSpec` and one line --
because the roadmap depends on adding many of them.

Design notes
------------
Plain functions and frozen dataclasses, no class hierarchy (coding rule 3).
A feature is data plus one callable; subclassing ``BaseFeature`` would add a vtable
and no capability.

``warmup_bars`` is the number of bars required before the feature yields a
non-null value. It is *declared* here and *verified* in
``tests/unit/test_features.py``, so a mis-declared warm-up fails CI rather than
silently producing nulls a signal then treats as neutral.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

import polars as pl

from app.domain.enums import Timeframe
from app.features import indicators as ind
from app.features.calendars import bars_per_year


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Definition of a single derived column.

    Attributes:
        name: stable identifier, also the output column name and the API key.
            Treat it as a public contract -- renaming breaks stored signals.
        description: human-readable meaning, surfaced through the API so a
            consumer never has to read the source to know what a number is.
        warmup_bars: bars required before the value is non-null.
        build: returns the Polars expression, aliased to ``name``.
        tags: free-form grouping, e.g. ``{"trend"}``, ``{"volume"}``.
    """

    name: str
    description: str
    warmup_bars: int
    build: Callable[[], pl.Expr]
    tags: frozenset[str] = field(default_factory=frozenset)

    def expression(self) -> pl.Expr:
        """The expression, guaranteed to be aliased to :attr:`name`."""
        return self.build().alias(self.name)


class FeatureSet:
    """An ordered, uniquely-named collection of :class:`FeatureSpec`."""

    def __init__(self, specs: Iterable[FeatureSpec]) -> None:
        self._specs: list[FeatureSpec] = []
        self._by_name: dict[str, FeatureSpec] = {}
        for spec in specs:
            self.add(spec)

    def add(self, spec: FeatureSpec) -> None:
        """Register a spec.

        Raises:
            ValueError: on a duplicate name. Two features sharing a name would
                mean one silently overwrites the other's column.
        """
        if spec.name in self._by_name:
            msg = f"feature {spec.name!r} is already registered"
            raise ValueError(msg)
        self._specs.append(spec)
        self._by_name[spec.name] = spec

    def get(self, name: str) -> FeatureSpec | None:
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def subset(self, names: Iterable[str]) -> FeatureSet:
        """A new set containing only ``names``.

        Raises:
            KeyError: if a requested name is not registered.
        """
        wanted = list(names)
        missing = [n for n in wanted if n not in self._by_name]
        if missing:
            msg = f"unknown feature(s): {', '.join(sorted(missing))}"
            raise KeyError(msg)
        return FeatureSet(self._by_name[n] for n in wanted)

    @property
    def warmup_bars(self) -> int:
        """Bars needed before *every* feature in the set is non-null."""
        return max((spec.warmup_bars for spec in self._specs), default=0)

    def expressions(self) -> list[pl.Expr]:
        return [spec.expression() for spec in self._specs]

    def __iter__(self) -> Iterator[FeatureSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name


def build_default_feature_set(timeframe: Timeframe = Timeframe.D1) -> FeatureSet:
    """The baseline features computed for every instrument.

    ``timeframe`` only affects volatility annualisation; every other feature is
    unit-free in bar counts.

    Window lengths (14/20/50/60) are conventional chart periods, not optimised
    values. They are inputs to the phase 4 backtest, not conclusions from one.
    """
    periods = bars_per_year(timeframe)

    return FeatureSet(
        [
            # --- Returns --------------------------------------------------
            FeatureSpec(
                name="return_1",
                description="Simple return over the last bar, as a fraction.",
                warmup_bars=2,
                build=lambda: ind.simple_return(1),
                tags=frozenset({"momentum", "return"}),
            ),
            FeatureSpec(
                name="return_5",
                description="Simple return over the last 5 bars, as a fraction.",
                warmup_bars=6,
                build=lambda: ind.simple_return(5),
                tags=frozenset({"momentum", "return"}),
            ),
            FeatureSpec(
                name="return_20",
                description="Simple return over the last 20 bars, as a fraction.",
                warmup_bars=21,
                build=lambda: ind.simple_return(20),
                tags=frozenset({"momentum", "return"}),
            ),
            FeatureSpec(
                name="log_return_1",
                description="Log return over the last bar.",
                warmup_bars=2,
                build=lambda: ind.log_return(1),
                tags=frozenset({"return"}),
            ),
            # --- Moving averages -----------------------------------------
            FeatureSpec(
                name="sma_20",
                description="20-bar simple moving average of close.",
                warmup_bars=20,
                build=lambda: ind.sma(20),
                tags=frozenset({"trend"}),
            ),
            FeatureSpec(
                name="sma_50",
                description="50-bar simple moving average of close.",
                warmup_bars=50,
                build=lambda: ind.sma(50),
                tags=frozenset({"trend"}),
            ),
            FeatureSpec(
                name="ema_20",
                description="20-bar exponential moving average of close.",
                warmup_bars=20,
                build=lambda: ind.ema(20),
                tags=frozenset({"trend"}),
            ),
            FeatureSpec(
                name="ema_50",
                description="50-bar exponential moving average of close.",
                warmup_bars=50,
                build=lambda: ind.ema(50),
                tags=frozenset({"trend"}),
            ),
            FeatureSpec(
                name="dist_sma_20",
                description="Percentage distance of close from its 20-bar SMA.",
                warmup_bars=20,
                build=lambda: ind.distance_from_ma(20),
                tags=frozenset({"trend", "mean_reversion"}),
            ),
            FeatureSpec(
                name="ema_spread_20_50",
                description=(
                    "Percentage gap between the 20-bar and 50-bar EMA. "
                    "Positive means the fast EMA is above the slow one."
                ),
                warmup_bars=50,
                build=lambda: ind.ma_spread_percent(20, 50),
                tags=frozenset({"trend"}),
            ),
            # --- Oscillators ----------------------------------------------
            FeatureSpec(
                name="rsi_14",
                description="Wilder's 14-bar Relative Strength Index, 0-100.",
                warmup_bars=15,
                build=lambda: ind.rsi(14),
                tags=frozenset({"momentum", "oscillator"}),
            ),
            # --- Volatility -----------------------------------------------
            # ATR needs 14 bars, not 15: the first True Range falls back to
            # high - low when there is no previous close, which is the standard
            # convention, so no bar is consumed by the lag.
            FeatureSpec(
                name="atr_14",
                description="14-bar Average True Range in absolute price units.",
                warmup_bars=14,
                build=lambda: ind.atr(14),
                tags=frozenset({"volatility"}),
            ),
            FeatureSpec(
                name="atr_pct_14",
                description="14-bar ATR as a percentage of close.",
                warmup_bars=14,
                build=lambda: ind.atr_percent(14),
                tags=frozenset({"volatility"}),
            ),
            FeatureSpec(
                name="volatility_20",
                description=(
                    "Annualised standard deviation of 1-bar log returns over 20 bars, "
                    "as a fraction (0.25 == 25%)."
                ),
                warmup_bars=21,
                build=lambda: ind.rolling_volatility(20, periods_per_year=periods),
                tags=frozenset({"volatility"}),
            ),
            FeatureSpec(
                name="vol_ratio_10_60",
                description=(
                    "10-bar volatility divided by 60-bar volatility. "
                    "Above 1.0 means volatility is expanding."
                ),
                warmup_bars=61,
                build=lambda: ind.volatility_ratio(10, 60),
                tags=frozenset({"volatility", "regime"}),
            ),
            # --- Volume ----------------------------------------------------
            FeatureSpec(
                name="rel_volume_20",
                description=(
                    "Bar volume divided by the mean volume of the 20 preceding bars. "
                    "2.0 means twice the recent typical volume."
                ),
                warmup_bars=21,
                build=lambda: ind.relative_volume(20),
                tags=frozenset({"volume"}),
            ),
            FeatureSpec(
                name="volume_sma_20",
                description="20-bar simple moving average of volume.",
                warmup_bars=20,
                build=lambda: ind.volume_sma(20),
                tags=frozenset({"volume"}),
            ),
        ]
    )
