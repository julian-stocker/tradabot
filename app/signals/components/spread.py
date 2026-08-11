"""Spread/cost-quality component (QUALITY): can this move be captured profitably?

Scores in ``[-100, 0]``, comparing the round-trip cost against the size of the
move the instrument typically makes over the signal's horizon.

The comparison is the point. A 20 bps spread is negligible on a name that moves
3% a day and prohibitive on one that moves 0.2%. Judging spread in isolation --
"under 10 bps is good" -- is meaningless without the move it has to be paid out
of.

This is the component that most directly encodes the project's core financial
constraint: cost is not a footnote applied after the decision, it is part of the
decision. It is also mirrored, separately and more precisely, in the
:class:`~app.costs.models.NetEdge` attached to every signal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from app.domain.enums import ReasonKind
from app.signals.components.base import ScoringContext, unavailable
from app.signals.models import ComponentKind, ComponentScore, Reason
from app.signals.scoring import clamp, penalty_score

NAME: Final = "spread"
KIND: Final = ComponentKind.QUALITY

# Cost as a fraction of the expected horizon move. Below `CHEAP` the friction is
# immaterial; at `PROHIBITIVE` it consumes the entire expected move.
CHEAP_COST_RATIO: Final = 0.10
PROHIBITIVE_COST_RATIO: Final = 1.00


class SpreadComponent:
    """Penalises setups whose expected move is small relative to its cost."""

    def __init__(self, configured_weight: float = 0.0) -> None:
        self._configured_weight = configured_weight

    @property
    def name(self) -> str:
        return NAME

    @property
    def kind(self) -> ComponentKind:
        return KIND

    def score(self, context: ScoringContext) -> ComponentScore:
        atr_pct = context.value("atr_pct_14")
        if atr_pct is None:
            return unavailable(NAME, KIND, "atr_pct_14 not warmed up", self._configured_weight)

        spread_bps = float(context.spread_bps)
        horizon_bars = context.horizon.bars_for_timeframe(context.timeframe)

        # Typical horizon move, in bps, scaled as a random walk (sqrt of time).
        typical_move_bps = atr_pct * 100.0 * (horizon_bars**0.5)
        if typical_move_bps <= 0:
            return unavailable(NAME, KIND, "expected move is zero", self._configured_weight)

        # Round trip crosses the spread twice; broker fees are excluded here
        # because they depend on position size, which a signal does not know.
        cost_ratio = (2.0 * spread_bps) / typical_move_bps
        penalty = _penalty(cost_ratio)

        reasons: list[Reason] = []
        if cost_ratio >= PROHIBITIVE_COST_RATIO:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="spread_prohibitive",
                    message=(
                        f"Round-trip spread ({2 * spread_bps:.1f} bps) meets or exceeds the "
                        f"typical {context.horizon.value} move ({typical_move_bps:.0f} bps); "
                        f"there is no room for an edge."
                    ),
                    value=spread_bps,
                )
            )
        elif cost_ratio >= CHEAP_COST_RATIO:
            reasons.append(
                Reason(
                    kind=ReasonKind.RISK,
                    code="spread_material",
                    message=(
                        f"Round-trip spread consumes {cost_ratio * 100:.0f}% of the typical "
                        f"{context.horizon.value} move."
                    ),
                    value=spread_bps,
                )
            )
        else:
            reasons.append(
                Reason(
                    kind=ReasonKind.SUPPORT,
                    code="spread_tight",
                    message=(
                        f"Spread {spread_bps:.1f} bps is small relative to the typical "
                        f"{context.horizon.value} move ({typical_move_bps:.0f} bps)."
                    ),
                    value=spread_bps,
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


def estimate_expected_move_bps(
    *,
    atr_pct: float,
    score: float,
    horizon_bars: int,
    capture_ratio: float,
) -> Decimal:
    """Crude expected favourable move for a signal, in basis points.

    **This is a placeholder, and by some distance the weakest number tradabot
    produces.** It says: "a typical *range* over this horizon is ATR% scaled by
    the square root of time; a full-conviction signal claims ``capture_ratio`` of
    that range as directional drift"::

        expected_move_bps = (|score| / 100) * capture_ratio
                            * atr_pct * 100 * sqrt(horizon_bars)

    Three assumptions do all the work, and none is validated:

    1. **Square-root-of-time scaling** assumes independent, identically
       distributed returns. Real returns show autocorrelation and volatility
       clustering, so this is wrong in a regime-dependent direction.
    2. **Range is not drift.** ATR measures how far price wanders, not how far it
       trends. ``capture_ratio`` (from configuration, default 0.25) exists solely
       to stop the estimate from treating the entire wandering range as expected
       profit.
    3. **``|score| / 100`` as a conviction fraction** is invention. Nothing has
       yet established that a score of 60 precedes any move at all.

    It exists so the net-edge calculation has a defined, configurable input rather
    than a hidden guess, and so cost-aware filtering is exercised end to end.
    Phase 7 replaces it with a model calibrated on realised forward returns. Until
    then, treat the resulting net edge as illustrative plumbing, not a forecast.
    """
    if horizon_bars < 1:
        msg = f"horizon_bars must be >= 1, got {horizon_bars}"
        raise ValueError(msg)
    if not 0 < capture_ratio <= 1:
        msg = f"capture_ratio must be in (0, 1], got {capture_ratio}"
        raise ValueError(msg)
    conviction = min(abs(score), 100.0) / 100.0
    move_bps = conviction * capture_ratio * atr_pct * 100.0 * (horizon_bars**0.5)
    return Decimal(str(round(move_bps, 4)))


def _penalty(cost_ratio: float) -> float:
    """Linear 0..100 penalty between the cheap and prohibitive cost ratios."""
    if cost_ratio <= CHEAP_COST_RATIO:
        return 0.0
    if cost_ratio >= PROHIBITIVE_COST_RATIO:
        return 100.0
    span = PROHIBITIVE_COST_RATIO - CHEAP_COST_RATIO
    return clamp((cost_ratio - CHEAP_COST_RATIO) / span * 100.0, 0.0, 100.0)
