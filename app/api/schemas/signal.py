"""Signal wire schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    Classification,
    Horizon,
    HorizonBucket,
    PriceSeriesAdjustment,
    ReasonKind,
    Timeframe,
)
from app.signals.models import ComponentKind


class ReasonResponse(BaseModel):
    """One piece of evidence for or against a signal."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    kind: ReasonKind
    code: str
    message: str
    feature: str | None = None
    value: float | None = None


class ComponentScoreResponse(BaseModel):
    """A component's contribution."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    name: str
    kind: ComponentKind
    score: float = Field(description="-100..100; QUALITY components score -100..0.")
    weight: float = Field(description="Effective weight after renormalising for availability.")
    configured_weight: float
    contribution: float = Field(description="score * weight")
    available: bool
    reasons: list[ReasonResponse]


class NetEdgeResponse(BaseModel):
    """Expected move against modelled cost."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    expected_move_bps: Decimal = Field(
        description=(
            "Crude estimate of the favourable move, in bps. A placeholder based on "
            "ATR and the score; not a calibrated forecast."
        )
    )
    cost_bps: Decimal = Field(description="Modelled round-trip cost in bps at a reference size.")
    net_edge_bps: Decimal = Field(description="expected_move_bps - cost_bps.")
    is_actionable: bool = Field(description="True when net_edge_bps > 0.")
    cost_coverage_ratio: float | None = Field(
        default=None, description="expected_move_bps / cost_bps; null when cost is zero."
    )


class SignalResponse(BaseModel):
    """A complete, self-explaining signal.

    ``score`` is an ordinal heuristic in ``[-100, 100]``, not a return forecast or
    a probability. ``confidence`` measures internal agreement between components,
    not historical accuracy -- nothing here has been validated against realised
    outcomes yet.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    timestamp: datetime = Field(description="Bar the signal was computed on.")
    generated_at: datetime
    timeframe: Timeframe
    horizon: Horizon
    horizon_bucket: HorizonBucket
    price_adjustment: PriceSeriesAdjustment = Field(
        description=(
            "Price series the features were computed from. Required to reproduce "
            "the signal: the same bar yields different features on raw prices."
        )
    )

    score: float
    classification: Classification
    confidence: float
    is_actionable: bool = Field(
        description="Directional AND expected to survive transaction costs."
    )

    reasons: list[ReasonResponse] = Field(description="Evidence supporting the signal.")
    risks: list[ReasonResponse] = Field(description="Evidence against it.")
    components: list[ComponentScoreResponse]

    reference_price: Decimal
    spread_bps: Decimal
    net_edge: NetEdgeResponse

    feature_snapshot: dict[str, float | None]
    bars_used: int
    engine_version: str
