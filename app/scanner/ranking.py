"""Ranking current candidates.

Deterministic and transparent. **No machine learning**: the weights below are
legible guesses, and the point of persisting the contributions is that a future
phase can measure whether any of them earn their place.

Every ranked candidate carries its own breakdown, so "why did A rank above B?"
is answered by reading the row rather than by re-running the scanner and hoping
the market has not moved.

What ranking is not
-------------------
A rank is an ordering of things that already cleared a threshold. It is **not** a
probability, not a confidence, and not a claim that the top candidate is more
likely to work than the second. Sorting a list does not create information.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

RANKING_VERSION: Final = "rank-v1"

# Contribution weights, summing to 1.0. Chosen for legibility, not fitted to
# anything. `score` dominates because it is the only component that already
# aggregates the technical picture; the rest are tie-breakers that encode "and it
# should also be tradable".
WEIGHT_SCORE: Final = 0.40
WEIGHT_AGREEMENT: Final = 0.20
WEIGHT_CONFIDENCE: Final = 0.15
WEIGHT_NET_EDGE: Final = 0.15
WEIGHT_LIQUIDITY: Final = 0.10

# Normalisation bounds. A net edge of 100bps or more scores full marks; a spread
# of 50bps or more scores zero. Both are engineering choices that bound an
# unbounded quantity so it can be mixed with the others.
NET_EDGE_CAP_BPS: Final = 100.0
SPREAD_FLOOR_BPS: Final = 5.0
SPREAD_CAP_BPS: Final = 50.0
RELATIVE_VOLUME_CAP: Final = 3.0


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One candidate with its rank and the arithmetic behind it."""

    symbol: str
    evaluation_id: int | None
    tracked_signal_id: int | None

    score: float
    confidence: float
    agreement: float
    net_edge_bps: float | None
    spread_bps: float | None
    relative_volume: float | None

    rank_score: float
    contributions: dict[str, float] = field(default_factory=dict)
    """Each component's weighted contribution. Persisted and displayed so the
    ordering can be audited rather than trusted."""

    direction: int = 0
    lifecycle: str = ""
    horizon: str = ""

    def explain(self) -> str:
        """One line naming the largest contributors."""
        top = sorted(self.contributions.items(), key=lambda kv: kv[1], reverse=True)[:3]
        parts = ", ".join(f"{name} {value:+.3f}" for name, value in top)
        return f"{self.symbol} rank={self.rank_score:.3f} ({parts})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "evaluation_id": self.evaluation_id,
            "tracked_signal_id": self.tracked_signal_id,
            "score": self.score,
            "confidence": self.confidence,
            "agreement": self.agreement,
            "net_edge_bps": self.net_edge_bps,
            "spread_bps": self.spread_bps,
            "relative_volume": self.relative_volume,
            "rank_score": self.rank_score,
            "contributions": self.contributions,
            "direction": self.direction,
            "lifecycle": self.lifecycle,
            "horizon": self.horizon,
            "ranking_version": RANKING_VERSION,
        }


def rank_score(
    *,
    score: float,
    confidence: float,
    agreement: float,
    net_edge_bps: float | None,
    spread_bps: float | None,
    relative_volume: float | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute a rank in [0, 1] and the contribution of each component.

    Missing inputs contribute **zero**, never a neutral midpoint. A candidate
    with no measured spread should not outrank one with a measured good spread
    on the strength of an assumption.
    """
    normalised_score = _clamp(score / 100.0)
    normalised_agreement = _clamp(agreement)
    normalised_confidence = _clamp(confidence)
    normalised_edge = _clamp(net_edge_bps / NET_EDGE_CAP_BPS) if net_edge_bps is not None else 0.0
    normalised_liquidity = _liquidity(spread_bps, relative_volume)

    contributions = {
        "score": WEIGHT_SCORE * normalised_score,
        "agreement": WEIGHT_AGREEMENT * normalised_agreement,
        "confidence": WEIGHT_CONFIDENCE * normalised_confidence,
        "net_edge": WEIGHT_NET_EDGE * normalised_edge,
        "liquidity": WEIGHT_LIQUIDITY * normalised_liquidity,
    }
    return sum(contributions.values()), contributions


def _liquidity(spread_bps: float | None, relative_volume: float | None) -> float:
    """Tradability, from spread and participation.

    Spread dominates: a wide spread is a cost paid on every trade, while
    elevated volume is merely encouraging. Unknown spread scores zero rather
    than average -- an unmeasured cost is not a low cost.
    """
    if spread_bps is None:
        return 0.0

    span = SPREAD_CAP_BPS - SPREAD_FLOOR_BPS
    tightness = _clamp((SPREAD_CAP_BPS - spread_bps) / span) if span > 0 else 0.0

    if relative_volume is None:
        return tightness
    participation = _clamp(relative_volume / RELATIVE_VOLUME_CAP)
    return 0.75 * tightness + 0.25 * participation


def rank_candidates(
    candidates: list[RankedCandidate], *, limit: int | None = None
) -> list[RankedCandidate]:
    """Sort by rank, highest first.

    Ties break on symbol so the order is **totally** deterministic. Two runs over
    identical data must produce an identical list, or "the ranking changed"
    stops being evidence of anything.
    """
    ordered = sorted(candidates, key=lambda c: (-c.rank_score, c.symbol))
    return ordered[:limit] if limit is not None else ordered


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))
