"""Signal result data structures.

The result type is designed around one requirement: **a signal must be able to
explain itself**. A bare score of ``+64`` is unusable -- it cannot be reviewed,
argued with, or debugged six months later. So every component records the
evidence it acted on, on both sides of the case.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.costs.models import NetEdge
from app.domain.enums import Classification, Horizon, ReasonKind, Timeframe


class ComponentKind(StrEnum):
    """How a component's score enters the total.

    ``DIRECTIONAL``
        Has an opinion about direction. Signed: positive is bullish.

    ``QUALITY``
        Has no directional opinion; judges whether the setup is *worth acting on*.
        Always scores in ``[-100, 0]`` and can only ever reduce the magnitude of
        the final score, never flip its sign.

    The distinction exists because collapsing the two is a genuine modelling
    error. "Volatility is elevated" is not a bearish opinion, but if it is summed
    into a directional total as a negative number, that is exactly what it becomes
    -- and it would make a wide spread look like a reason to short.
    """

    DIRECTIONAL = "DIRECTIONAL"
    QUALITY = "QUALITY"


class Reason(BaseModel):
    """One piece of evidence behind a signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ReasonKind
    code: str = Field(description="Stable machine-readable identifier, e.g. 'ema20_above_ema50'.")
    message: str = Field(description="Human-readable statement, e.g. 'Price 4.2% above EMA20'.")
    feature: str | None = Field(default=None, description="Feature this was derived from.")
    value: float | None = Field(default=None, description="Feature value at signal time.")


class ComponentScore(BaseModel):
    """One component's contribution to the overall signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: ComponentKind
    score: float = Field(ge=-100.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0, description="Effective weight after renormalisation.")
    configured_weight: float = Field(ge=0.0, le=1.0, description="Weight as configured.")
    available: bool = Field(description="False when required features had not warmed up.")
    reasons: tuple[Reason, ...] = ()

    @property
    def contribution(self) -> float:
        """``score * weight`` -- what this component added to the total."""
        return self.score * self.weight

    @property
    def is_directional(self) -> bool:
        return self.kind is ComponentKind.DIRECTIONAL

    @property
    def is_quality(self) -> bool:
        return self.kind is ComponentKind.QUALITY

    @property
    def supports(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.kind is ReasonKind.SUPPORT)

    @property
    def risks(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.kind is ReasonKind.RISK)


class SignalResult(BaseModel):
    """A complete, explainable signal for one instrument, horizon and moment.

    ``score`` is the headline number in ``[-100, 100]``. It is **not** a return
    forecast, a probability, or a confidence level. It is a weighted blend of
    heuristic component scores, and its only defensible interpretation today is
    ordinal: a 60 expresses a stronger version of the same view than a 30.

    ``net_edge`` is what turns a view into a candidate: it subtracts modelled
    round-trip costs from the (crudely estimated) expected move. A bullish signal
    with a negative net edge is a bullish signal that is not worth trading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timestamp: datetime = Field(description="Timestamp of the bar the signal was computed on.")
    generated_at: datetime = Field(description="When the computation ran (UTC).")
    timeframe: Timeframe
    horizon: Horizon

    score: float = Field(ge=-100.0, le=100.0)
    classification: Classification
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Heuristic reliability estimate combining feature availability and "
            "agreement between components. NOT a probability of being correct."
        ),
    )

    components: tuple[ComponentScore, ...]
    feature_snapshot: dict[str, float | None] = Field(
        description="Every feature value the signal was computed from, for auditability."
    )

    reference_price: Decimal = Field(gt=0, description="Close of the signal bar.")
    spread_bps: Decimal = Field(ge=0, description="Spread used for cost modelling.")
    net_edge: NetEdge

    bars_used: int = Field(ge=0)
    engine_version: str = Field(description="Scoring-model version, for reproducibility.")

    @property
    def reasons(self) -> tuple[Reason, ...]:
        """Supporting evidence, strongest contributors first."""
        ordered = sorted(self.components, key=lambda c: -abs(c.contribution))
        return tuple(r for c in ordered for r in c.supports)

    @property
    def risks(self) -> tuple[Reason, ...]:
        """Evidence against the signal, strongest contributors first."""
        ordered = sorted(self.components, key=lambda c: -abs(c.contribution))
        return tuple(r for c in ordered for r in c.risks)

    @property
    def is_actionable(self) -> bool:
        """Directional *and* expected to survive transaction costs.

        The conjunction is the whole point: direction alone is not an opportunity.
        """
        return self.classification is not Classification.NEUTRAL and self.net_edge.is_actionable
