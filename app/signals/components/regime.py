"""Market-regime component (QUALITY): is the environment stable enough to trust?

Scores in ``[-100, 0]``. Two crude regime proxies:

* **Volatility expansion** (10-bar vol / 60-bar vol). A sharp expansion usually
  means the character of the market just changed, which is exactly when
  relationships fitted on the previous regime stop holding.
* **Trend/extension disagreement** -- price extended far from its SMA while the
  EMA structure points the other way, i.e. a counter-trend spike. Directional
  components read that as conviction; it is more often noise.

This is an *instrument-local* proxy for regime. A real implementation needs
market-wide inputs (index trend, breadth, a volatility index, correlation
structure), which requires multi-instrument data the phase 1 pipeline does not
assemble. Named honestly rather than overstated -- see docs/roadmap.md phase 3.
"""

from __future__ import annotations

from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import penalty_score

NAME: Final = "regime"
KIND: Final = ComponentKind.QUALITY

STABLE_VOL_RATIO: Final = 1.2
UNSTABLE_VOL_RATIO: Final = 2.5
CONFLICT_PENALTY: Final = 40.0
CONFLICT_DIST_PCT: Final = 3.0


class RegimeComponent:
    """Penalises unstable or internally contradictory market conditions."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        inputs = context.values("vol_ratio_10_60", "ema_spread_20_50", "dist_sma_20")
        if inputs is None:
            return unavailable(
                NAME,
                KIND,
                "vol_ratio_10_60, ema_spread_20_50 or dist_sma_20 not warmed up",
                self._configured_weight,
            )
        vol_ratio, ema_spread, dist_sma = inputs

        penalty = 0.0
        reasons: list[Reason] = []

        if vol_ratio >= UNSTABLE_VOL_RATIO:
            penalty += 60.0
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="volatility_regime_break",
                    message=(
                        f"Short-term volatility is {vol_ratio:.2f}x the longer-term "
                        f"baseline; the regime appears to be breaking."
                    ),
                    feature="vol_ratio_10_60",
                    value=vol_ratio,
                )
            )
        elif vol_ratio >= STABLE_VOL_RATIO:
            penalty += 25.0
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="volatility_expanding",
                    message=(
                        f"Short-term volatility is {vol_ratio:.2f}x the longer-term "
                        f"baseline (expanding)."
                    ),
                    feature="vol_ratio_10_60",
                    value=vol_ratio,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="volatility_regime_stable",
                    message=f"Volatility regime is stable ({vol_ratio:.2f}x baseline).",
                    feature="vol_ratio_10_60",
                    value=vol_ratio,
                )
            )

        # Counter-trend spike: price stretched one way, EMA structure the other.
        trend_up = ema_spread >= 0
        extended_up = dist_sma >= CONFLICT_DIST_PCT
        extended_down = dist_sma <= -CONFLICT_DIST_PCT
        if (trend_up and extended_down) or (not trend_up and extended_up):
            penalty += CONFLICT_PENALTY
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="trend_extension_conflict",
                    message=(
                        f"Price is {dist_sma:+.1f}% from its SMA20 against the prevailing "
                        f"EMA structure ({ema_spread:+.2f}%) -- likely a counter-trend move."
                    ),
                    feature="dist_sma_20",
                    value=dist_sma,
                )
            )

        return ComponentScore(
            name=NAME,
            kind=KIND,
            score=penalty_score(penalty),
            weight=self._configured_weight,
            configured_weight=self._configured_weight,
            available=True,
            reasons=tuple(reasons),
        )
