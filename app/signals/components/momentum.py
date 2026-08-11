"""Momentum component: has price been moving, and is that move stretched?

Blends multi-horizon returns with RSI. RSI enters with a deliberate twist: a
*moderately* elevated RSI confirms momentum, but an extreme one is flagged as a
risk rather than as extra bullishness. Buying strength and buying exhaustion look
identical to a naive momentum score.

Scale constants are baseline heuristics (see docs/architecture.md#heuristics).
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import blend, linear_score, squash

NAME: Final = "momentum"
KIND: Final = ComponentKind.DIRECTIONAL

# A 3% five-bar move or an 8% twenty-bar move is treated as a strong reading.
RETURN_5_SCALE: Final = 0.03
RETURN_20_SCALE: Final = 0.08

RSI_OVERBOUGHT: Final = 70.0
RSI_OVERSOLD: Final = 30.0
RSI_EXTREME_HIGH: Final = 80.0
RSI_EXTREME_LOW: Final = 20.0

WEIGHT_RETURN_5: Final = 0.45
WEIGHT_RETURN_20: Final = 0.30
WEIGHT_RSI: Final = 0.25


class MomentumComponent:
    """Scores recent directional price movement."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        inputs = context.values("return_5", "return_20", "rsi_14")
        if inputs is None:
            return unavailable(
                NAME, KIND, "return_5, return_20 or rsi_14 not warmed up", self._configured_weight
            )
        return_5, return_20, rsi = inputs

        score_5 = squash(return_5, RETURN_5_SCALE)
        score_20 = squash(return_20, RETURN_20_SCALE)
        score_rsi = linear_score(rsi, neutral=50.0, full=RSI_OVERBOUGHT)

        total = blend(
            (score_5, WEIGHT_RETURN_5),
            (score_20, WEIGHT_RETURN_20),
            (score_rsi, WEIGHT_RSI),
        )

        reasons: list[Reason] = []
        direction = "Positive" if return_5 >= 0 else "Negative"
        reasons.append(
            Reason(
                kind=ReasonKind.SUPPORT,
                code="return_5",
                message=f"{direction} 5-bar momentum ({return_5 * 100:+.2f}%).",
                feature="return_5",
                value=return_5,
            )
        )
        reasons.append(
            Reason(
                kind=ReasonKind.SUPPORT,
                code="return_20",
                message=f"20-bar return {return_20 * 100:+.2f}%.",
                feature="return_20",
                value=return_20,
            )
        )

        # RSI: confirmation in the mid range, exhaustion warning at the extremes.
        if rsi >= RSI_EXTREME_HIGH:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="rsi_extreme_overbought",
                    message=f"RSI {rsi:.1f} is extremely overbought; move may be exhausted.",
                    feature="rsi_14",
                    value=rsi,
                )
            )
        elif rsi >= RSI_OVERBOUGHT:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="rsi_overbought",
                    message=f"RSI {rsi:.1f} is elevated.",
                    feature="rsi_14",
                    value=rsi,
                )
            )
        elif rsi <= RSI_EXTREME_LOW:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="rsi_extreme_oversold",
                    message=f"RSI {rsi:.1f} is extremely oversold; a bounce is likely.",
                    feature="rsi_14",
                    value=rsi,
                )
            )
        elif rsi <= RSI_OVERSOLD:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="rsi_oversold",
                    message=f"RSI {rsi:.1f} is depressed.",
                    feature="rsi_14",
                    value=rsi,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="rsi_neutral_range",
                    message=f"RSI {rsi:.1f} is in a sustainable range.",
                    feature="rsi_14",
                    value=rsi,
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
