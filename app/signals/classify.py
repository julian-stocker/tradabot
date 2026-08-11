"""Score to classification, and confidence estimation."""

from __future__ import annotations

from app.core.config import SignalSettings
from app.domain.enums import Classification
from app.signals.models import ComponentScore


def classify(score: float, settings: SignalSettings) -> Classification:
    """Map a score in ``[-100, 100]`` onto a :class:`Classification`.

    Thresholds are symmetric around zero and configurable. Symmetry is a
    deliberate simplification: equities fall faster than they rise, so an
    asymmetric threshold is defensible -- but picking one without evidence would
    just be a different arbitrary choice, wearing a lab coat.
    """
    if score >= settings.strong_bullish_threshold:
        return Classification.STRONG_BULLISH
    if score >= settings.bullish_threshold:
        return Classification.BULLISH
    if score <= -settings.strong_bullish_threshold:
        return Classification.STRONG_BEARISH
    if score <= -settings.bullish_threshold:
        return Classification.BEARISH
    return Classification.NEUTRAL


def estimate_confidence(components: tuple[ComponentScore, ...], total_score: float) -> float:
    """Heuristic confidence in ``[0, 1]``.

    Two factors, multiplied:

    ``coverage``
        Share of configured weight that was actually evaluable. If half the
        components could not compute because features had not warmed up, the
        result deserves half the confidence.

    ``agreement``
        How tightly the directional components cluster around the total. Three
        components saying ``+55, +60, +58`` is a different epistemic situation
        from ``+95, -20, +80`` averaging to the same place, and the score alone
        cannot distinguish them.

    **This is not a probability.** It does not estimate how often the signal is
    right -- nothing in phase 1 has ever been measured against a realised
    outcome. It is an internal-consistency measure, and a signal can be
    confidently wrong. Calibration against realised returns is phase 8.
    """
    if not components:
        return 0.0

    configured_total = sum(c.configured_weight for c in components)
    if configured_total <= 0:
        return 0.0

    available_weight = sum(c.configured_weight for c in components if c.available)
    coverage = available_weight / configured_total

    directional = [c for c in components if c.available and c.is_directional]
    directional_weight = sum(c.configured_weight for c in directional)
    if not directional or directional_weight <= 0:
        return 0.0

    # Weighted mean absolute deviation of component scores from the total,
    # normalised by the full score range to land in [0, 1].
    deviation = (
        sum(abs(c.score - total_score) * c.configured_weight for c in directional)
        / directional_weight
    )
    agreement = max(0.0, 1.0 - deviation / 100.0)

    return round(max(0.0, min(1.0, coverage * agreement)), 4)
