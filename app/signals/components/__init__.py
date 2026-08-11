"""Scoring components.

Each module contributes one facet of the baseline model. Names must match the
keys of :class:`~app.core.config.SignalWeights`.
"""

from app.signals.components.base import ScoringComponent, ScoringContext
from app.signals.components.momentum import MomentumComponent
from app.signals.components.regime import RegimeComponent
from app.signals.components.spread import SpreadComponent
from app.signals.components.trend import TrendComponent
from app.signals.components.volatility import VolatilityComponent
from app.signals.components.volume import VolumeComponent

__all__ = [
    "MomentumComponent",
    "RegimeComponent",
    "ScoringComponent",
    "ScoringContext",
    "SpreadComponent",
    "TrendComponent",
    "VolatilityComponent",
    "VolumeComponent",
]
