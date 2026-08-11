"""Scoring component contract."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.domain.enums import Horizon, ReasonKind, Timeframe
from app.features.engine import FeatureSnapshot
from app.signals.models import ComponentKind, ComponentScore, Reason


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything a component may look at.

    Deliberately narrow. A component gets a feature snapshot for *one bar*, and
    has no access to the candle history, the database, or any future bar. It is
    structurally incapable of look-ahead bias -- there is nothing to look ahead
    into.
    """

    symbol: str
    snapshot: FeatureSnapshot
    timeframe: Timeframe
    horizon: Horizon
    spread_bps: Decimal

    def value(self, feature: str) -> float | None:
        """Feature value, or ``None`` if it has not warmed up."""
        return self.snapshot.get(feature)

    def values(self, *features: str) -> tuple[float, ...] | None:
        """All requested values, or ``None`` if *any* is unavailable.

        The all-or-nothing behaviour is intentional: a component that computes a
        blend from three inputs and silently substitutes 0.0 for a missing one is
        reporting a confident opinion it does not have.
        """
        collected: list[float] = []
        for name in features:
            value = self.snapshot.get(name)
            if value is None:
                return None
            collected.append(value)
        return tuple(collected)


@runtime_checkable
class ScoringComponent(Protocol):
    """One facet of the scoring model.

    Implementations are plain objects with a ``name``, a ``kind`` and a ``score``
    method -- no base class to inherit (coding rule 3).
    """

    @property
    def name(self) -> str:
        """Must match a key in :class:`~app.core.config.SignalWeights`."""
        ...

    @property
    def kind(self) -> ComponentKind: ...

    def score(self, context: ScoringContext) -> ComponentScore:
        """Evaluate this component.

        Must return a score in ``[-100, 100]`` (``[-100, 0]`` for
        :attr:`~app.signals.models.ComponentKind.QUALITY` components), and set
        ``available=False`` rather than guessing when inputs are missing.
        """
        ...


def unavailable(
    name: str, kind: ComponentKind, missing: str, configured_weight: float = 0.0
) -> ComponentScore:
    """Build the "I cannot evaluate this" result.

    The engine drops unavailable components and renormalises the remaining
    weights, so a missing feature reduces confidence instead of quietly voting
    neutral.
    """
    return ComponentScore(
        name=name,
        kind=kind,
        score=0.0,
        weight=0.0,
        configured_weight=configured_weight,
        available=False,
        reasons=(
            Reason(
                kind=ReasonKind.RISK,
                code=f"{name}_unavailable",
                message=f"{name.capitalize()} could not be evaluated: {missing}.",
            ),
        ),
    )
