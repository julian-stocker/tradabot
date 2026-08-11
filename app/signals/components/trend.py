"""Trend component: is the moving-average structure aligned, and how stretched?

Two distinct ideas, deliberately weighted differently:

* **Alignment** (EMA20 vs EMA50) -- the trend's direction.
* **Extension** (price vs SMA20) -- how far price has run from its own mean.

Extension is capped and given a smaller weight because it cuts both ways: price
above its average confirms an uptrend, but price *far* above it is a stretched
entry. That is recorded as a risk rather than as more bullishness.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import blend, squash

NAME: Final = "trend"
KIND: Final = ComponentKind.DIRECTIONAL

EMA_SPREAD_SCALE: Final = 2.0  # 2% gap between EMA20 and EMA50 is a strong trend
DIST_SMA_SCALE: Final = 5.0  # 5% above the 20-bar SMA is a strong extension
STRETCHED_DIST_PCT: Final = 10.0

WEIGHT_ALIGNMENT: Final = 0.65
WEIGHT_EXTENSION: Final = 0.35


class TrendComponent:
    """Scores moving-average structure."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        inputs = context.values("ema_spread_20_50", "dist_sma_20", "ema_20")
        if inputs is None:
            return unavailable(
                NAME,
                KIND,
                "ema_spread_20_50, dist_sma_20 or ema_20 not warmed up",
                self._configured_weight,
            )
        ema_spread, dist_sma, ema_20 = inputs
        close = context.snapshot.close

        alignment_score = squash(ema_spread, EMA_SPREAD_SCALE)
        extension_score = squash(dist_sma, DIST_SMA_SCALE)

        total = blend(
            (alignment_score, WEIGHT_ALIGNMENT),
            (extension_score, WEIGHT_EXTENSION),
        )

        reasons: list[Reason] = []
        if ema_spread >= 0:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="ema20_above_ema50",
                    message=f"EMA20 is {ema_spread:.2f}% above EMA50 (uptrend structure).",
                    feature="ema_spread_20_50",
                    value=ema_spread,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="ema20_below_ema50",
                    message=f"EMA20 is {abs(ema_spread):.2f}% below EMA50 (downtrend structure).",
                    feature="ema_spread_20_50",
                    value=ema_spread,
                )
            )

        above_ema = close >= ema_20
        reasons.append(
            Reason(
                kind=ReasonKind.SUPPORT,
                code="price_above_ema20" if above_ema else "price_below_ema20",
                message=(
                    f"Price {close:.2f} is {'above' if above_ema else 'below'} "
                    f"EMA20 ({ema_20:.2f})."
                ),
                feature="ema_20",
                value=ema_20,
            )
        )

        if abs(dist_sma) >= STRETCHED_DIST_PCT:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="stretched_from_sma20",
                    message=(
                        f"Price is {dist_sma:+.1f}% from its 20-bar SMA; "
                        f"entry is stretched and prone to mean reversion."
                    ),
                    feature="dist_sma_20",
                    value=dist_sma,
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
