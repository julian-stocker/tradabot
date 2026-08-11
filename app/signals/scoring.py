"""Score-shaping helpers.

Every component ultimately emits a number in ``[-100, 100]``. These helpers are
the only sanctioned way to get there, so the mapping from "a 4% five-day move" to
"a score of 62" is written down once instead of being re-improvised per component.

All of the scale constants passed to these functions are **baseline heuristics**.
They were chosen so that typical values land in a readable part of the range, not
because any study says they should.
"""

from __future__ import annotations

import math

SCORE_MIN = -100.0
SCORE_MAX = 100.0


def clamp(value: float, low: float = SCORE_MIN, high: float = SCORE_MAX) -> float:
    """Constrain ``value`` to ``[low, high]``.

    The trailing ``+ 0.0`` normalises negative zero, which otherwise renders as
    ``-0.0`` in API responses and reads as a (tiny) bearish tilt.
    """
    return max(low, min(high, value)) + 0.0


def squash(value: float, scale: float) -> float:
    """Map an unbounded quantity onto ``[-100, 100]`` via ``tanh``.

    ``scale`` is the value that maps to roughly ±76 (``tanh(1) = 0.7616``).
    So ``squash(0.04, 0.04)`` is about 76: "a 4% move is a strong reading".

    tanh is used rather than a linear clip because it degrades gracefully at the
    extremes: a 40% move scores more than a 4% move, but not ten times more.
    Outliers should not dominate a weighted average.
    """
    if scale <= 0:
        msg = f"scale must be positive, got {scale}"
        raise ValueError(msg)
    return SCORE_MAX * math.tanh(value / scale)


def linear_score(value: float, neutral: float, full: float) -> float:
    """Linearly map ``value`` from ``neutral`` -> 0 and ``full`` -> ±100.

    Used where a bounded input already has a natural neutral point, e.g. RSI
    (neutral 50, saturating near 20/80). ``full`` may be below ``neutral`` to
    invert the direction.
    """
    if full == neutral:
        msg = "neutral and full must differ"
        raise ValueError(msg)
    return clamp((value - neutral) / (full - neutral) * SCORE_MAX)


def penalty_score(magnitude: float) -> float:
    """Turn a 0..100 penalty magnitude into a QUALITY score in ``[-100, 0]``.

    The single place the "quality components never score positive" invariant is
    enforced. The trailing ``+ 0.0`` matters: negating a clamped zero yields
    ``-0.0``, which survives JSON serialisation and reads as a bearish tilt from a
    component that has no direction to give.
    """
    return -clamp(magnitude, 0.0, SCORE_MAX) + 0.0


def blend(*weighted: tuple[float, float]) -> float:
    """Weighted average of ``(score, weight)`` pairs, ignoring zero weights.

    Returns 0.0 when no positive weight is supplied, which callers should treat
    as "no opinion" rather than "neutral opinion".
    """
    total_weight = sum(w for _, w in weighted if w > 0)
    if total_weight <= 0:
        return 0.0
    return clamp(sum(s * w for s, w in weighted if w > 0) / total_weight)
