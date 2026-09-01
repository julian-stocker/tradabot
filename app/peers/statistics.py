"""Cross-sectional statistics, with the conventions written down.

Two different things get called "percentile" and they are computed differently
here, on purpose:

**Percentile rank** -- where one company sits among its peers. Uses the midrank
convention: values strictly below, plus half of the ties, over the peer count.
Ties are the reason to spell this out. Three peers sharing a margin of 22.0%
should all report the same rank, and that rank should sit in the middle of the
band they jointly occupy rather than at either edge. Counting ties as "below"
would let a company at the median report the 100th percentile against a group
where every peer matched it.

**Distribution quantiles** -- the peer median and quartiles. Linear
interpolation between the two nearest order statistics (the convention R calls
type 7 and NumPy calls ``linear``). Chosen because it is the most widely
implemented, so a reader checking the number in a spreadsheet gets the same
answer.

No winsorization, and none is needed
------------------------------------
Outlier handling would be a judgement call this layer is not entitled to make,
and a silent one would be worse. It is avoided by construction instead: the
median and the interquartile range are already robust to extreme values, so a
single peer with a 400x multiple moves the reported distribution barely at all.
The mean and standard deviation, which are not robust, are deliberately not
reported.

What *is* excluded is the undefined rather than the extreme: a non-finite value,
and a non-positive value for a metric where that makes it meaningless (a
price-to-earnings on negative earnings is not a low multiple). Each exclusion is
counted and surfaced rather than dropped quietly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

MIN_PEERS: int = 8
"""Peers required before any percentile is reported. **Pre-declared, and not
tuned against output.**

Eight is the smallest group where the reported statistics are all supported by
the sample. The midrank percentile then has a resolution of 1/8 -- 12.5 points
-- which is coarser than it looks but honest, and it is the finest resolution at
which the three-band wording in :mod:`app.peers.service` still separates. Both
quartiles are interpolated between real observations with at least two values
outside them on each side, so neither is an extrapolation from the tail.

Below eight the arithmetic still produces a number, and that is the problem: a
"75th percentile" among four peers means "third of four", which reads as
precision the sample cannot support. The refusal is the honest output.

The floor applies twice -- to the peer group, and again to each metric, since a
group of fourteen may hold only five companies with a usable free-cash-flow
multiple."""


def quantile(values: Sequence[float], q: float) -> float:
    """The ``q`` quantile by linear interpolation (R type 7 / NumPy ``linear``).

    Args:
        values: at least one value. Need not be sorted.
        q: quantile in ``[0, 1]``.
    """
    ordered = sorted(values)
    if not ordered:
        msg = "quantile of an empty sample"
        raise ValueError(msg)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def percentile_rank(value: float, peers: Sequence[float]) -> float:
    """Where ``value`` sits among ``peers``, as a percentage, midrank convention.

    Returns the share of peers the value exceeds, counting each tie as half.
    Deterministic for any input ordering: the arithmetic depends only on the
    counts, never on the sequence.
    """
    if not peers:
        msg = "percentile rank against an empty peer set"
        raise ValueError(msg)
    below = sum(1 for p in peers if p < value)
    tied = sum(1 for p in peers if p == value)
    return (below + 0.5 * tied) / len(peers) * 100.0


def usable(value: float | None, *, positive_only: bool) -> bool:
    """Whether a value may enter a distribution at all.

    Excludes the undefined, never the merely extreme -- ``None``, NaN and
    infinities always, and non-positive values for metrics where they carry no
    meaning. A company with negative earnings has no price-to-earnings ratio;
    admitting it as a very low one would rank it as the cheapest in its group.
    """
    if value is None or not math.isfinite(value):
        return False
    return not (positive_only and value <= 0)
