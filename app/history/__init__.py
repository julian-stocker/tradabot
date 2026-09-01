"""Deterministic company trajectory over point-in-time filings.

Describes what a company's economics have already done. Contains no forecast,
no extrapolation and no threshold chosen from a share price. Depends on the
fact store alone -- no Discord, no broker, no research ingestion, no model
provider -- so the same report serves a card, a newsletter or a future web view.
"""

from app.history.schemas import (
    Change,
    CompanyTrajectory,
    Direction,
    MetricTrajectory,
    Observation,
    SeriesBasis,
    SeriesStatus,
)
from app.history.series import annual_series, latest_run, ratio_series, ttm_series
from app.history.service import (
    MARGIN_STABLE_PP,
    MIN_OBSERVATIONS,
    SHARE_STABLE_PCT,
    WINDOWS,
    CompanyHistoryService,
    midrank_percentile,
)

__all__ = [
    "MARGIN_STABLE_PP",
    "MIN_OBSERVATIONS",
    "SHARE_STABLE_PCT",
    "WINDOWS",
    "Change",
    "CompanyHistoryService",
    "CompanyTrajectory",
    "Direction",
    "MetricTrajectory",
    "Observation",
    "SeriesBasis",
    "SeriesStatus",
    "annual_series",
    "latest_run",
    "midrank_percentile",
    "ratio_series",
    "ttm_series",
]
