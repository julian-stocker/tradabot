"""Volume component: is the price move backed by participation?

Volume is **directionless on its own**. Two million shares traded says nothing
about which way the stock is going. So this component scores the *product* of
volume intensity and recent price direction: heavy volume on an up-move is
bullish confirmation, and the identical volume on a down-move is bearish
confirmation.

Getting this wrong -- scoring raw volume as bullish -- is one of the most common
errors in naive scoring models, and it makes crashes look like buy signals.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import clamp, squash

NAME: Final = "volume"
KIND: Final = ComponentKind.DIRECTIONAL

# Relative volume of 2.0x maps to a strong intensity reading.
RELATIVE_VOLUME_SCALE: Final = 1.0
HIGH_RELATIVE_VOLUME: Final = 1.5
LOW_RELATIVE_VOLUME: Final = 0.6
# Below this, the move is treated as directionless and volume cannot confirm it.
FLAT_RETURN_THRESHOLD: Final = 0.001


class VolumeComponent:
    """Scores volume as confirmation of the prevailing price direction."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        inputs = context.values("rel_volume_20", "return_5")
        if inputs is None:
            return unavailable(
                NAME, KIND, "rel_volume_20 or return_5 not warmed up", self._configured_weight
            )
        rel_volume, return_5 = inputs

        # Intensity in [-100, 100]: 1.0x relative volume is neutral.
        intensity = squash(rel_volume - 1.0, RELATIVE_VOLUME_SCALE)

        # Direction in [-1, 1], fading to 0 for flat moves so that heavy volume on
        # a directionless bar contributes nothing instead of amplifying noise.
        direction = clamp(return_5 / (FLAT_RETURN_THRESHOLD * 10.0), -1.0, 1.0)

        total = clamp(intensity * direction)

        reasons: list[Reason] = []
        if rel_volume >= HIGH_RELATIVE_VOLUME:
            confirms = "buying" if return_5 > 0 else "selling"
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="high_relative_volume",
                    message=(f"Relative volume {rel_volume:.2f}x confirms {confirms} pressure."),
                    feature="rel_volume_20",
                    value=rel_volume,
                )
            )
        elif rel_volume <= LOW_RELATIVE_VOLUME:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="low_relative_volume",
                    message=(
                        f"Relative volume {rel_volume:.2f}x is below normal; "
                        f"the move lacks participation."
                    ),
                    feature="rel_volume_20",
                    value=rel_volume,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="normal_relative_volume",
                    message=f"Relative volume {rel_volume:.2f}x is around normal.",
                    feature="rel_volume_20",
                    value=rel_volume,
                )
            )

        if abs(return_5) < FLAT_RETURN_THRESHOLD:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="volume_without_direction",
                    message=(
                        "Price is essentially flat over 5 bars, so volume cannot "
                        "confirm a direction."
                    ),
                    feature="return_5",
                    value=return_5,
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
