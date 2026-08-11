"""Probabilistic forecast interfaces.

No model is implemented in phase 1, and none should be until backtesting can
falsify one (see docs/ml.md).

These types exist to fix the *shape* of a forecast now: probabilistic, horizon-
explicit, and cost-aware. A forecast that returns a single price -- "NVDA will be
at 132" -- is unfalsifiable in any useful sense and cannot be position-sized.
Everything here is a distribution or a probability.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Horizon, Timeframe


class ProbabilisticForecast(BaseModel):
    """A calibrated probabilistic view over one horizon.

    Every probability here must be **calibrated**: when the model says 0.6, the
    event should happen about 60% of the time. An uncalibrated probability is
    worse than no probability, because position sizing consumes it as if it were
    real. Calibration is measured with reliability curves and Brier scores in
    phase 8 -- not assumed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timestamp: datetime = Field(description="Bar the forecast was made from.")
    timeframe: Timeframe
    horizon: Horizon

    prob_positive_return: float = Field(ge=0, le=1, description="P(return > 0 over the horizon).")
    prob_exceeds_threshold: float = Field(
        ge=0, le=1, description="P(return > threshold_bps over the horizon)."
    )
    threshold_bps: Decimal = Field(
        description=(
            "Threshold for prob_exceeds_threshold. Should default to the round-trip "
            "cost: P(profitable) matters, P(up) does not."
        )
    )

    expected_return_bps: Decimal = Field(description="Mean of the predicted return distribution.")
    expected_range_low_bps: Decimal = Field(description="Lower bound of the prediction interval.")
    expected_range_high_bps: Decimal = Field(description="Upper bound of the prediction interval.")
    interval_confidence: float = Field(
        default=0.8, gt=0, lt=1, description="Coverage of the interval, e.g. 0.8 for 10th-90th pct."
    )

    model_name: str
    model_version: str
    features_used: tuple[str, ...] = Field(
        description="Exact feature set the model consumed, for reproducibility."
    )

    @property
    def is_directional(self) -> bool:
        """Whether the model has a view distinguishable from a coin flip."""
        return abs(self.prob_positive_return - 0.5) > 0.05  # noqa: PLR2004


@runtime_checkable
class Forecaster(Protocol):
    """Produces a :class:`ProbabilisticForecast`.

    ``predict`` takes a feature snapshot for a single bar and nothing else, which
    keeps a model structurally unable to consume future data -- the same
    constraint the rule-based signal engine operates under.
    """

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    def predict(
        self,
        *,
        symbol: str,
        features: dict[str, float | None],
        timestamp: datetime,
        timeframe: Timeframe,
        horizon: Horizon,
    ) -> ProbabilisticForecast:
        """Forecast from one bar's features."""
        ...
