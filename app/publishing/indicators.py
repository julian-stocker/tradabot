"""What a displayed number currently means, decided from evidence that exists.

``/check`` prints figures a reader without a finance background cannot place. Is
a 33.2% operating margin good? Is a 37x price-to-earnings ratio a problem? The
figures were correct and mute. This module attaches a category to the ones that
can carry one, and -- more often than not -- declines.

The rule that shapes everything here
------------------------------------
**A sign is not a semantic.** A positive number is not favourable and a negative
one is not unfavourable. Net debt of -$5B is net cash. A share count falling 2%
a year is a buyback. A 37x multiple is not a verdict. Every assessment below
comes from a *stated context* -- where a figure sits in the company's own
history, what an owning service already declared about it -- and never from the
number's magnitude or sign.

What the owning layers already decided, and what they refused to
---------------------------------------------------------------
Most of the work was done before this phase and is reused rather than repeated:

* The Advisor already labels capital structure ``BUYBACK_REDUCING_SHARE_COUNT``
  or ``MATERIAL_DILUTION``, and a balance sheet ``NET_CASH``, ``ACCEPTABLE`` or
  ``LEVERAGED``. :mod:`app.publishing.presentation` already maps those to
  colours. Nothing here re-decides them.
* The Advisor already places a multiple in its own history as
  ``VERY_HIGH_VS_HISTORY`` and so on, which presentation already calls orange --
  unusual, no direction. Expensive is not bad and cheap is not good.

Two refusals are load-bearing, and both belong to the layer that made them:

* :class:`app.history.schemas.Direction` is ``EXPANDING``/``COMPRESSING``, never
  ``IMPROVING``/``DECLINING``, because "whether it is bad depends on why". So a
  direction alone never produces green or red here. Position within the
  company's own recorded range does, and that is a different claim.
* :attr:`app.peers.schemas.MetricComparison.higher_is_not_better` is always
  true, and its docstring says it exists "so any future consumer that wants to
  sort by 'good' has to confront that the data does not support it". This module
  is that consumer. It confronts it: **a peer percentile never yields green or
  red on its own.** An extreme peer position is orange -- notable, worth a look,
  no direction -- because a SIC group is an industry, not a set of companies
  doing the same thing at the same scale.

Revenue gets no direction at all
--------------------------------
Not caution: measurement. The renderer already excludes revenue's own-history
percentile because it has a median of 98 across the universe and sits at or
above the 95th for 60% of companies -- an absolute quantity that trends upward
is nearly always at its own record. There is no context in which a revenue
figure here could be called favourable without inventing one, so revenue is
described and left alone.

Nothing aggregates
------------------
There is no score, no rating and no overall verdict. Each indicator is about one
metric, and two of them never combine into a third.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from app.publishing.presentation import Semantic, semantic

VERY_LOW_PCT: Final = 10.0
LOW_PCT: Final = 25.0
HIGH_PCT: Final = 75.0
VERY_HIGH_PCT: Final = 90.0
"""Percentile bands, on the **0-100** scale.

The same cuts the Advisor uses to place a multiple in its own history, so the
project has one band vocabulary rather than two that nearly agree -- but stated
in different units, because the project has two percentile scales. The Advisor
works in 0-1 (``_P10 = 0.10``); :mod:`app.history` and :mod:`app.peers` both
return 0-100, which is what arrives here. Copying ``0.10`` into this file would
have put every band below the first percentile and marked every margin in the
universe favourable. A test asserts the two remain numerically equivalent."""

MARGIN_METRICS: Final[frozenset[str]] = frozenset(
    {"operating_margin", "gross_margin", "fcf_margin"}
)
"""Metrics whose own-history position has a defensible direction.

A margin is profit retained per unit of revenue, so sitting near the top of the
range this company has actually recorded is a presently favourable condition,
and near the bottom an unfavourable one. The claim is self-referential and
therefore immune to the objection that sinks cross-company comparison: a
structurally low-margin business is compared only against itself.

Revenue is absent for the reason in the module docstring. Share count is absent
because the Advisor already labels it and that label is authoritative."""


class Evidence(StrEnum):
    """Which layer's output an indicator was derived from. Always recorded."""

    ADVISOR_STATE = "ADVISOR_STATE"
    """A label the Advisor already emitted; presentation already coloured it."""
    OWN_HISTORY = "OWN_HISTORY"
    """Position within this company's own recorded range."""
    PEER_POSITION = "PEER_POSITION"
    """Position within the declared industry group. Never directional alone."""
    MIXED_CONTEXT = "MIXED_CONTEXT"
    """Own history and peers point different ways. Reported, never averaged."""
    COVERAGE = "COVERAGE"
    """Partial, stale or otherwise reduced data."""
    REFUSAL = "REFUSAL"
    """Not applicable or deliberately declined by an owning service."""


@dataclass(frozen=True, slots=True)
class MetricIndicator:
    """One metric's current category, and the sentence that justifies it.

    ``reason`` is not decoration. An indicator that can only say "green" asks the
    reader to trust it; one that says "98th pct of its own history" tells them
    what was actually observed, and they can disagree with it.
    """

    metric: str
    status: Semantic
    reason: str
    evidence: Evidence

    @property
    def badge(self) -> str:
        return BADGES[self.status]

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "status": str(self.status),
            "reason": self.reason,
            "evidence": str(self.evidence),
            "badge": self.badge,
        }


BADGES: Final[dict[Semantic, str]] = {
    Semantic.GOOD: "🟢",
    Semantic.BAD: "🔴",
    Semantic.UNUSUAL: "🟠",
    Semantic.UNCERTAIN: "🟡",
    Semantic.NEUTRAL: "🔵",
    Semantic.UNAVAILABLE: "⚪",
}
"""The six categories as emoji, matching the six embed colours exactly.

Emoji rather than colour, because Discord cannot colour a substring of embed
text. The ANSI-code-block trick can, and produces a block that renders as grey
noise on mobile and defeats selection -- a worse card in exchange for a nicer
screenshot."""


def _band(percentile: float | None) -> str | None:
    """Which band a percentile falls in, or ``None`` if there is not one."""
    if percentile is None:
        return None
    if percentile <= VERY_LOW_PCT:
        return "VERY_LOW"
    if percentile <= LOW_PCT:
        return "LOW"
    if percentile < HIGH_PCT:
        return "NORMAL"
    if percentile < VERY_HIGH_PCT:
        return "HIGH"
    return "VERY_HIGH"


def ordinal(percentile: float) -> str:
    """``90.0`` -> ``90th``. Rounded to whole points: the sample supporting a
    percentile is never fine enough to justify a decimal place."""
    value = round(percentile)
    teens = 10 <= value % 100 <= 20  # noqa: PLR2004 - 11th, 12th, 13th
    suffix = "th" if teens else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def margin_indicator(
    metric: str,
    *,
    own_percentile: float | None,
    peer_percentile: float | None,
) -> MetricIndicator | None:
    """Where a margin stands, from its own history and its peer group.

    Own history decides the direction; peers can only disagree with it. When
    they do, the result is orange and says both -- Apple's operating margin sits
    at the 98th percentile of its own record and the 83rd among peers, which
    agree, while its gross margin is high for Apple and below a peer median
    dragged up by companies selling software. Neither number is wrong and
    neither wins. Averaging them would produce a third number describing
    nothing.
    """
    if metric not in MARGIN_METRICS:
        return None
    own = _band(own_percentile)
    peer = _band(peer_percentile)
    if own is None and peer is None:
        return None

    if own is None:
        return peer_only(metric, peer_percentile)

    assert own_percentile is not None
    favourable = own in ("HIGH", "VERY_HIGH")
    unfavourable = own in ("LOW", "VERY_LOW")
    own_text = f"{ordinal(own_percentile)} pct of own history"

    if not favourable and not unfavourable:
        return MetricIndicator(
            metric=metric,
            status=Semantic.NEUTRAL,
            reason=f"mid-range: {own_text}",
            evidence=Evidence.OWN_HISTORY,
        )
    return _with_peer(
        metric,
        directed=Semantic.GOOD if favourable else Semantic.BAD,
        favourable=favourable,
        own_text=own_text,
        peer=peer,
        peer_percentile=peer_percentile,
    )


def _with_peer(
    metric: str,
    *,
    directed: Semantic,
    favourable: bool,
    own_text: str,
    peer: str | None,
    peer_percentile: float | None,
) -> MetricIndicator:
    """Own history has decided a direction; peers may only disagree with it."""
    if peer is None or peer_percentile is None:
        return MetricIndicator(
            metric=metric, status=directed, reason=own_text, evidence=Evidence.OWN_HISTORY
        )
    peer_text = f"{ordinal(peer_percentile)} pct among peers"
    contradicts = (favourable and peer in ("LOW", "VERY_LOW")) or (
        not favourable and peer in ("HIGH", "VERY_HIGH")
    )
    if contradicts:
        return MetricIndicator(
            metric=metric,
            status=Semantic.UNUSUAL,
            reason=f"{own_text}; {peer_text}",
            evidence=Evidence.MIXED_CONTEXT,
        )
    return MetricIndicator(
        metric=metric,
        status=directed,
        reason=f"{own_text} · {peer_text}",
        evidence=Evidence.OWN_HISTORY,
    )


def from_state(metric: str, internal: str | None, reason: str) -> MetricIndicator | None:
    """An indicator for a state an owning service already named.

    The colour is whatever :mod:`app.publishing.presentation` already says it
    is. Nothing is re-decided here, so a change to how a buyback or a leveraged
    balance sheet is categorised happens in one place and arrives here for free.
    """
    if not internal:
        return None
    return MetricIndicator(
        metric=metric,
        status=semantic(internal),
        reason=reason,
        evidence=Evidence.ADVISOR_STATE,
    )


def peer_only(metric: str, percentile: float | None) -> MetricIndicator | None:
    """An extreme peer position, with no direction attached.

    Orange and never green or red. A high percentile means the company's figure
    exceeds most of a group defined by four-digit SIC code -- an industry, not a
    set of companies doing the same thing at the same scale.
    """
    band = _band(percentile)
    if percentile is None or band not in ("VERY_HIGH", "VERY_LOW"):
        return None
    return MetricIndicator(
        metric=metric,
        status=Semantic.UNUSUAL,
        reason=f"{ordinal(percentile)} pct among peers",
        evidence=Evidence.PEER_POSITION,
    )
