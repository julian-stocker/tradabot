"""When a change in expected movement is worth saying out loud.

The volatility engine produces an estimate for all 52 symbols on every cycle.
Almost none of that is news. This module decides which of it is, and it is
separate from :mod:`app.notifications.trends` because the two answer different
questions: trends asks "did something happen?", this asks "did the *state*
change?".

State, not cooldown
-------------------
The existing trend cooldown is time-based -- fire, then stay quiet for four
hours. That is the right rule for "NVDA moved 4%", which is an event. It is the
wrong rule for a volatility regime, which is a condition that persists for days:
a four-hour timer would re-announce the same elevated state six times a day.

So volatility uses **transitions**. A symbol entering HIGH is news; a symbol
still being HIGH ninety minutes later is not. And because the state itself is the
deduplication, an escalation from HIGH to EXTREME publishes immediately rather
than waiting out a timer that exists for a different purpose -- which is the
specific failure the brief warns against.

Flood control
-------------
Volatility is common-factor driven, so the universe moves together: phase 8.1
measured 34 of 52 symbols elevated during the 2020 election and 0 today. Thirty-
four messages would be an outage of attention, so a cycle emits **one** post with
the few most unusual names and a count of the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from app.market_data.volatility import (
    MODEL_VERSION,
    ExpectedMovement,
    VolatilityRegime,
)

VOLATILITY_SCOPE: Final = "volatility"
"""``notification_state`` scope holding the last announced regime per symbol.

Reuses the existing table exactly as signal and health state do -- the row is
``(scope='volatility', key='vol:NVDA', phase='HIGH_VOL')``. No migration: this is
the same shape of fact those scopes already store.
"""

DISCLAIMER: Final = "Movement magnitude only — not a direction forecast."

TOP_N: Final = 4
"""Symbols named individually before the rest become a count.

Four fits a phone screen with its detail lines intact. The alternative -- listing
everything -- is a spreadsheet, and nobody reads a spreadsheet on a phone.
"""

FORBIDDEN_WORDS: Final[tuple[str, ...]] = (
    "buy",
    "sell",
    "entry",
    "exit",
    "target",
    "bullish",
    "bearish",
    "probability",
    "recommend",
)
"""Vocabulary a magnitude message must never contain.

Broader than the trends channel's list because this one quotes *numbers about
future movement*, which is exactly the context in which a stray "target" would
read as a price forecast.
"""

_RANK: Final[dict[VolatilityRegime, int]] = {
    VolatilityRegime.LOW: 0,
    VolatilityRegime.NORMAL: 1,
    VolatilityRegime.HIGH: 2,
    VolatilityRegime.EXTREME: 3,
}


class VolatilityTransition:
    """What changed, and whether it is worth a message."""

    ELEVATED = "ELEVATED"
    ESCALATED = "ESCALATED"
    NORMALISED = "NORMALISED"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class VolatilityEvent:
    """One symbol's regime change, ready to render."""

    movement: ExpectedMovement
    transition: str
    previous: VolatilityRegime | None

    @property
    def symbol(self) -> str:
        return self.movement.symbol

    @property
    def headline(self) -> str:
        """Magnitude vocabulary only. No direction word can appear here."""
        if self.transition == VolatilityTransition.NORMALISED:
            return "volatility normalised"
        if self.movement.regime is VolatilityRegime.EXTREME:
            return "EXTREME expected movement"
        return "HIGH expected movement"

    @property
    def detail(self) -> str:
        movement = self.movement
        if self.transition == VolatilityTransition.NORMALISED:
            return f"now {movement.regime.value} ({movement.percentile * 100:.0f}th pct)"
        return (
            f"{movement.percentile * 100:.0f}th pct · typical session "
            f"~{movement.typical_range_pct:.1f}% · stress ~{movement.stress_range_pct:.1f}%"
        )


def classify_transition(current: VolatilityRegime, previous: VolatilityRegime | None) -> str:
    """Decide whether this regime change deserves a message.

    Publishes on entering an elevated state and on escalating within it. Stays
    silent while a state persists, and on de-escalating from EXTREME to HIGH --
    still elevated, still already announced, and a message saying "slightly less
    extreme" is noise.
    """
    was_elevated = previous is not None and previous.is_elevated

    if current.is_elevated and not was_elevated:
        return VolatilityTransition.ELEVATED
    if current.is_elevated and was_elevated:
        assert previous is not None
        if _RANK[current] > _RANK[previous]:
            return VolatilityTransition.ESCALATED
        return VolatilityTransition.UNCHANGED
    if was_elevated and not current.is_elevated:
        return VolatilityTransition.NORMALISED
    return VolatilityTransition.UNCHANGED


def detect_events(
    estimates: list[ExpectedMovement],
    previous: dict[str, VolatilityRegime | None],
    *,
    now: datetime,
) -> list[VolatilityEvent]:
    """Regime changes worth announcing, most unusual first.

    **A stale estimate produces nothing at all** -- neither an elevation nor a
    normalisation. Its inputs are too old to support either claim, and inventing
    a "volatility normalised" message from a stalled feed would be reporting on
    the feed while appearing to report on the market.
    """
    events: list[VolatilityEvent] = []
    for movement in estimates:
        if movement.is_stale(now=now):
            continue
        prior = previous.get(movement.symbol)
        transition = classify_transition(movement.regime, prior)
        if transition == VolatilityTransition.UNCHANGED:
            continue
        events.append(VolatilityEvent(movement=movement, transition=transition, previous=prior))

    return sorted(events, key=_notability, reverse=True)


def _notability(event: VolatilityEvent) -> tuple[int, float]:
    """Escalations rank above first elevations, and both above normalisations.

    Within a tier, by percentile -- how unusual the reading is, never how
    attractive. There is no attractiveness quantity here to sort by.
    """
    tier = {
        VolatilityTransition.ESCALATED: 2,
        VolatilityTransition.ELEVATED: 1,
        VolatilityTransition.NORMALISED: 0,
    }[event.transition]
    return tier, event.movement.percentile


def next_state(estimates: list[ExpectedMovement], *, now: datetime) -> dict[str, str]:
    """The regime to persist per symbol after this cycle.

    Stale estimates are **omitted**, leaving the stored regime untouched: a
    stalled feed must not silently rewrite the state and cause a spurious
    transition when it recovers.
    """
    return {
        movement.symbol: movement.regime.value
        for movement in estimates
        if not movement.is_stale(now=now)
    }


def assert_no_recommendation_language(text: str) -> None:
    """Guard the boundary between magnitude and advice."""
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        if word in lowered:
            msg = f"volatility message contains forbidden language: {word!r}"
            raise ValueError(msg)


def build_section(
    events: list[VolatilityEvent], *, elevated_total: int, limit: int = TOP_N
) -> dict[str, Any]:
    """The volatility part of a market-activity payload.

    One section per cycle, never one message per symbol. ``elevated_total`` is
    the full count of currently elevated names so the post can say how much it is
    not showing, rather than implying the listed few are all of it.
    """
    shown = events[:limit]
    remaining = max(0, len(events) - len(shown))

    section: dict[str, Any] = {
        "title": "📊 EXPECTED MOVEMENT",
        "events": [
            {
                "symbol": event.symbol,
                "transition": event.transition,
                "headline": event.headline,
                "detail": event.detail,
                "regime": event.movement.regime.value,
                "percentile": round(event.movement.percentile * 100),
            }
            for event in shown
        ],
        "more": remaining,
        "elevated_total": elevated_total,
        "model": MODEL_VERSION,
        "disclaimer": DISCLAIMER,
    }

    lines = [f"**{e.symbol}** — {e.headline}\n   {e.detail}" for e in shown]
    if remaining:
        lines.append(f"+ {remaining} more symbol(s) changed volatility state")
    if elevated_total > len(shown):
        lines.append(f"({elevated_total} symbols currently elevated)")
    section["lines"] = lines

    assert_no_recommendation_language(" ".join(lines))
    return section
