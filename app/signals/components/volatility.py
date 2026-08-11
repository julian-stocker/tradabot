"""Volatility component (QUALITY): is this setup too noisy to act on?

Scores in ``[-100, 0]``. Elevated volatility is **not** a bearish opinion -- it is
a reason to trust any directional opinion less, because the same signal produces a
wider distribution of outcomes and a wider stop.

The engine applies quality scores as a dampener on the magnitude of the
directional total, so this can shrink a signal but never invert it.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import penalty_score

NAME: Final = "volatility"
KIND: Final = ComponentKind.QUALITY

# Annualised volatility, as a fraction. Below `CALM` costs nothing; at `SEVERE`
# the penalty is maximal. Equity-like defaults: 25% is unremarkable, 80% is wild.
CALM_VOLATILITY: Final = 0.25
SEVERE_VOLATILITY: Final = 0.80

# Per-bar ATR as a percentage of price.
CALM_ATR_PCT: Final = 2.0
SEVERE_ATR_PCT: Final = 8.0

WEIGHT_VOLATILITY: Final = 0.6
WEIGHT_ATR: Final = 0.4


class VolatilityComponent:
    """Penalises setups whose noise level swamps the expected move."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        inputs = context.values("volatility_20", "atr_pct_14")
        if inputs is None:
            return unavailable(
                NAME, KIND, "volatility_20 or atr_pct_14 not warmed up", self._configured_weight
            )
        volatility, atr_pct = inputs

        vol_penalty = _penalty(volatility, CALM_VOLATILITY, SEVERE_VOLATILITY)
        atr_penalty = _penalty(atr_pct, CALM_ATR_PCT, SEVERE_ATR_PCT)
        total = penalty_score(vol_penalty * WEIGHT_VOLATILITY + atr_penalty * WEIGHT_ATR)

        reasons: list[Reason] = []
        if volatility >= SEVERE_VOLATILITY:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="volatility_severe",
                    message=(
                        f"Annualised volatility {volatility * 100:.0f}% is severe; "
                        f"position sizing and stops must widen accordingly."
                    ),
                    feature="volatility_20",
                    value=volatility,
                )
            )
        elif volatility >= CALM_VOLATILITY:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="volatility_elevated",
                    message=f"Annualised volatility {volatility * 100:.0f}% is elevated.",
                    feature="volatility_20",
                    value=volatility,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="volatility_calm",
                    message=f"Annualised volatility {volatility * 100:.0f}% is contained.",
                    feature="volatility_20",
                    value=volatility,
                )
            )

        if atr_pct >= SEVERE_ATR_PCT:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="atr_wide",
                    message=(
                        f"Average true range is {atr_pct:.1f}% of price per bar; "
                        f"noise may dominate the expected move."
                    ),
                    feature="atr_pct_14",
                    value=atr_pct,
                )
            )

        return ComponentScore(
            name=NAME,
            kind=KIND,
            score=total,
            weight=self._configured_weight,
            configured_weight=self._configured_weight,
            available=True,
            reasons=tuple(reasons),
        )


def _penalty(value: float, calm: float, severe: float) -> float:
    """Linear 0..100 penalty between the ``calm`` and ``severe`` thresholds."""
    if value <= calm:
        return 0.0
    if value >= severe:
        return 100.0
    return (value - calm) / (severe - calm) * 100.0
