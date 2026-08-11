"""Rule-based signal engine.

Explicitly **not** machine learning, and not described as intelligence of any
kind. Every number it produces can be traced by hand from the feature snapshot
through a fixed arithmetic expression to the final score.

Aggregation model
-----------------
Directional and quality components combine differently, on purpose::

    directional = sum(w_i * s_i) / sum(w_i)          for DIRECTIONAL components
    dampening   = sum(w_j * |s_j|) / sum(w_j)        for QUALITY components (s_j <= 0)
    score       = directional * (1 - quality_share * dampening / 100)

where ``quality_share`` is the fraction of configured weight assigned to quality
components.

Why not simply sum everything? Because quality components have no direction. A
wide spread summed into a directional total makes an illiquid stock look bearish,
which is nonsense: the spread is equally punishing for a short. Treating quality
as a multiplicative dampener means poor conditions shrink a signal toward
neutral -- which is the actual claim -- and can never invert it.

Unavailable components are dropped and the remaining weights renormalised, so a
feature that has not warmed up lowers confidence instead of silently voting zero.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.core.config import CostSettings, SignalSettings
from app.core.errors import InsufficientDataError
from app.core.logging import get_logger
from app.core.time import utc_now
from app.costs.calculator import net_expected_edge, round_trip_cost_bps
from app.costs.models import NetEdge
from app.domain.enums import Horizon, Timeframe
from app.features.engine import FeatureSnapshot
from app.signals.classify import classify, estimate_confidence
from app.signals.components import (
    MomentumComponent,
    RegimeComponent,
    ScoringComponent,
    ScoringContext,
    SpreadComponent,
    TrendComponent,
    VolatilityComponent,
    VolumeComponent,
)
from app.signals.components.spread import estimate_expected_move_bps
from app.signals.models import ComponentScore, SignalResult
from app.signals.scoring import clamp

logger = get_logger(__name__)

ENGINE_VERSION: Final = "baseline-heuristic-v1"
"""Bump on any change to scoring logic or constants.

Stored on every signal so a result can always be reproduced with the code that
produced it. Comparing signals across versions is meaningless without it.
"""

# Notional used to express round-trip cost in bps. Fixed per-order fees make cost
# size-dependent, so a reference size is required to quote a rate at all.
REFERENCE_NOTIONAL: Final = Decimal("5000")


class SignalEngine:
    """Combines scoring components into an explainable :class:`SignalResult`.

    Args:
        signal_settings: weights and classification thresholds.
        cost_settings: broker cost assumptions for the net-edge calculation.
        components: override the component list (tests, experiments).
        clock: injected time source, so ``generated_at`` is deterministic in tests.
    """

    def __init__(
        self,
        signal_settings: SignalSettings,
        cost_settings: CostSettings,
        components: list[ScoringComponent] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = signal_settings
        self._costs = cost_settings
        self._clock = clock
        self._components = components or self._default_components(signal_settings)
        self._validate_components()

    @staticmethod
    def _default_components(settings: SignalSettings) -> list[ScoringComponent]:
        weights = settings.weights
        return [
            MomentumComponent(weights.momentum),
            VolumeComponent(weights.volume),
            TrendComponent(weights.trend),
            VolatilityComponent(weights.volatility),
            RegimeComponent(weights.regime),
            SpreadComponent(weights.spread),
        ]

    def _validate_components(self) -> None:
        """Fail fast on duplicate names or names with no configured weight."""
        names_list = [c.name for c in self._components]
        duplicates = {n for n in names_list if names_list.count(n) > 1}
        if duplicates:
            msg = f"duplicate scoring component name(s): {sorted(duplicates)}"
            raise ValueError(msg)

        configured = set(self._settings.weights.as_mapping())
        unknown = set(names_list) - configured
        if unknown:
            msg = (
                f"scoring components {sorted(unknown)} have no configured weight; "
                f"known weights: {sorted(configured)}"
            )
            raise ValueError(msg)

    @property
    def version(self) -> str:
        return ENGINE_VERSION

    def evaluate(
        self,
        *,
        symbol: str,
        snapshot: FeatureSnapshot,
        timeframe: Timeframe,
        horizon: Horizon,
        spread_bps: Decimal,
        reference_price: Decimal,
    ) -> SignalResult:
        """Score one instrument at one point in time.

        Args:
            symbol: instrument ticker.
            snapshot: feature values at the signal bar. Only this bar's data is
                used -- there is no access to later bars anywhere in the call.
            timeframe: candle interval the features were computed on.
            horizon: forecast horizon the signal applies to.
            spread_bps: observed or assumed spread, for cost modelling.
            reference_price: close of the signal bar, for cost notional maths.

        Raises:
            InsufficientDataError: no directional component could be evaluated.
        """
        context = ScoringContext(
            symbol=symbol,
            snapshot=snapshot,
            timeframe=timeframe,
            horizon=horizon,
            spread_bps=spread_bps,
        )

        raw_scores = [component.score(context) for component in self._components]
        scored = self._renormalise(raw_scores)
        total = self._aggregate(scored)

        classification = classify(total, self._settings)
        confidence = estimate_confidence(scored, total)

        net_edge = self._net_edge(
            snapshot=snapshot,
            score=total,
            timeframe=timeframe,
            horizon=horizon,
            spread_bps=spread_bps,
            reference_price=reference_price,
        )

        logger.debug(
            "signal evaluated",
            symbol=symbol,
            score=round(total, 2),
            classification=classification.value,
            horizon=horizon.value,
            net_edge_bps=float(net_edge.net_edge_bps),
        )

        return SignalResult(
            symbol=symbol,
            timestamp=snapshot.timestamp,
            generated_at=self._now(),
            timeframe=timeframe,
            horizon=horizon,
            score=round(total, 4),
            classification=classification,
            confidence=confidence,
            components=tuple(scored),
            feature_snapshot=dict(snapshot.values),
            reference_price=reference_price,
            spread_bps=spread_bps,
            net_edge=net_edge,
            bars_used=snapshot.bars_used,
            engine_version=ENGINE_VERSION,
        )

    # -- aggregation -------------------------------------------------------

    def _renormalise(self, scores: list[ComponentScore]) -> tuple[ComponentScore, ...]:
        """Redistribute the weight of unavailable components across the rest.

        Renormalisation happens **within each kind**. Directional weight must not
        leak into quality weight or vice versa -- they are combined by different
        arithmetic, so mixing them would change the meaning of both.
        """
        result: list[ComponentScore] = []
        for kind_scores in _partition_by_kind(scores):
            available = [s for s in kind_scores if s.available]
            total_weight = sum(s.configured_weight for s in available)
            for score in kind_scores:
                if not score.available or total_weight <= 0:
                    result.append(score.model_copy(update={"weight": 0.0}))
                else:
                    normalised = score.configured_weight / total_weight
                    result.append(score.model_copy(update={"weight": normalised}))
        # Preserve the original component order for stable output.
        by_name = {s.name: s for s in result}
        return tuple(by_name[s.name] for s in scores)

    def _aggregate(self, scores: tuple[ComponentScore, ...]) -> float:
        """Combine renormalised component scores into the headline score."""
        directional = [s for s in scores if s.is_directional and s.available]
        if not directional:
            raise InsufficientDataError(
                required=self._settings.min_bars,
                available=0,
                context="no directional scoring component could be evaluated",
            )

        directional_score = sum(s.score * s.weight for s in directional)

        quality = [s for s in scores if s.is_quality and s.available]
        if not quality:
            return clamp(directional_score)

        # Quality scores are <= 0; take the weighted magnitude as a 0..100 penalty.
        dampening = sum(abs(s.score) * s.weight for s in quality)

        configured_total = sum(s.configured_weight for s in scores)
        quality_share = (
            sum(s.configured_weight for s in quality) / configured_total
            if configured_total > 0
            else 0.0
        )

        factor = 1.0 - quality_share * (dampening / 100.0)
        # Poor conditions shrink a signal toward neutral; they never invert it.
        factor = max(0.0, factor)
        return clamp(directional_score * factor)

    # -- net edge ----------------------------------------------------------

    def _net_edge(
        self,
        *,
        snapshot: FeatureSnapshot,
        score: float,
        timeframe: Timeframe,
        horizon: Horizon,
        spread_bps: Decimal,
        reference_price: Decimal,
    ) -> NetEdge:
        """Expected move minus modelled round-trip cost.

        See :func:`~app.signals.components.spread.estimate_expected_move_bps` for
        why the expected-move input is the least trustworthy number here.
        """
        atr_pct = snapshot.get("atr_pct_14")
        horizon_bars = horizon.bars_for_timeframe(timeframe)

        expected_move_bps = (
            estimate_expected_move_bps(
                atr_pct=atr_pct,
                score=score,
                horizon_bars=horizon_bars,
                capture_ratio=self._settings.expected_move_capture_ratio,
            )
            if atr_pct is not None
            else Decimal(0)
        )

        quantity = REFERENCE_NOTIONAL / reference_price
        cost_bps = round_trip_cost_bps(
            price=reference_price,
            quantity=quantity,
            spread_bps=spread_bps,
            settings=self._costs,
        )
        return net_expected_edge(expected_move_bps=expected_move_bps, cost_bps=cost_bps)

    def _now(self) -> datetime:
        return self._clock()


def _partition_by_kind(scores: list[ComponentScore]) -> list[list[ComponentScore]]:
    """Group scores by :class:`~app.signals.models.ComponentKind`."""
    buckets: dict[str, list[ComponentScore]] = {}
    for score in scores:
        buckets.setdefault(score.kind.value, []).append(score)
    return list(buckets.values())
