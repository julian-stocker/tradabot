"""Market-scanner interfaces.

Not implemented in phase 1 (see docs/roadmap.md phase 3). Defined here so the
multiple-comparisons problem is recorded *before* anyone builds the feature that
creates it.

The hazard: scanning 500 instruments for "score > 55" and acting on the 12 hits
is a multiple-comparisons trap. At any plausible false-positive rate, a scanner
over a large universe returns hits every single day whether or not the signal has
any predictive value. :attr:`ScanResult.instruments_scanned` and
:attr:`ScanResult.hit_rate` are mandatory fields precisely so that the base rate
is impossible to look at a result without seeing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Classification, Horizon, Timeframe
from app.signals.models import SignalResult


class ScanFilter(BaseModel):
    """Criteria for selecting instruments from a scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_score: float | None = Field(default=None, ge=-100, le=100)
    max_score: float | None = Field(default=None, ge=-100, le=100)
    classifications: tuple[Classification, ...] = ()
    min_confidence: float | None = Field(default=None, ge=0, le=1)
    require_positive_net_edge: bool = Field(
        default=True,
        description=(
            "Exclude candidates whose expected move does not survive transaction "
            "costs. Defaults to True: a cost-negative candidate is not a candidate."
        ),
    )
    exchanges: tuple[str, ...] = ()
    max_spread_bps: float | None = Field(default=None, ge=0)


class ScanRequest(BaseModel):
    """A scan to run across the universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeframe: Timeframe = Timeframe.D1
    horizon: Horizon = Horizon.D5
    symbols: tuple[str, ...] = Field(
        default=(), description="Empty means the full active universe."
    )
    filters: ScanFilter = Field(default_factory=ScanFilter)
    as_of: datetime | None = Field(
        default=None, description="Run the scan as the world looked at this instant."
    )
    limit: int = Field(default=50, ge=1, le=500)


class ScanResult(BaseModel):
    """Scan output, always accompanied by its base rate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    request: ScanRequest
    instruments_scanned: int = Field(
        ge=0, description="Universe size. Required context for interpreting the hits."
    )
    instruments_skipped: int = Field(
        default=0, ge=0, description="Skipped for insufficient history or missing data."
    )
    matches: tuple[SignalResult, ...] = ()

    @property
    def hit_rate(self) -> float:
        """Fraction of the scanned universe that matched.

        If this is routinely high, the filter is not selective and the "hits" are
        just the market. If it swings wildly day to day, the filter is tracking
        market-wide moves rather than anything instrument-specific.
        """
        if self.instruments_scanned == 0:
            return 0.0
        return len(self.matches) / self.instruments_scanned
