"""Feature wire schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Timeframe


class FeatureDefinition(BaseModel):
    """What a feature means and how much history it needs.

    Served alongside the values so a consumer never has to read the source to
    interpret a number.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    warmup_bars: int = Field(description="Bars required before the value is non-null.")
    tags: list[str]


class FeatureRow(BaseModel):
    """Feature values at one bar.

    ``null`` means "not warmed up" or "undefined here". It never means zero, and
    must not be coerced to zero by clients -- that would turn "unknown" into
    "neutral", which is a different claim.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    close: float
    values: dict[str, float | None]


class FeatureSeriesResponse(BaseModel):
    """A feature series with its definitions."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    count: int
    definitions: list[FeatureDefinition]
    rows: list[FeatureRow]


class FeatureSnapshotResponse(BaseModel):
    """Feature values at a single, most-recent bar."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    close: float
    bars_used: int
    definitions: list[FeatureDefinition]
    values: dict[str, float | None]
