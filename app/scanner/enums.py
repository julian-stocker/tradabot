"""Scanner vocabulary.

Kept separate from :mod:`app.domain.enums` because these describe the *scanner's*
view of the world -- how a setup is progressing, what a session permits, whether
the data was good enough -- rather than the market itself.
"""

from __future__ import annotations

from enum import StrEnum


class SignalLifecycle(StrEnum):
    """Where a tracked setup is in its life.

    A setup that persists across scans keeps one identity and moves through these
    states. Creating a fresh signal every fifteen minutes for the same continuing
    setup would make "how long has this been true?" unanswerable, and that
    question is the whole reason the lifecycle exists.
    """

    DISCOVERED = "DISCOVERED"
    """Seen and evaluated, below the qualification threshold. The common case,
    and still recorded -- a rejected candidate is training data."""

    QUALIFIED = "QUALIFIED"
    STRONG = "STRONG"

    WEAKENED = "WEAKENED"
    """Was qualified, has fallen back but not out. Distinguished from
    INVALIDATED so a setup that dips and recovers is not double-counted as two
    separate discoveries."""

    INVALIDATED = "INVALIDATED"
    """Fell below the threshold, or its structural premise broke. Terminal."""

    EXPIRED = "EXPIRED"
    """Stopped being evaluated -- delisted, removed from the watchlist, or simply
    not seen for longer than the configured horizon. Terminal, and deliberately
    distinct from INVALIDATED: "we stopped looking" is not "it stopped being
    true", and conflating them would poison the future labels."""

    @property
    def is_terminal(self) -> bool:
        return self in {SignalLifecycle.INVALIDATED, SignalLifecycle.EXPIRED}

    @property
    def is_active(self) -> bool:
        """Whether the setup is still being tracked."""
        return not self.is_terminal


class TrendState(StrEnum):
    """One timeframe's structural read.

    Deliberately coarse. A finer scale would imply a precision the underlying
    heuristics do not have.
    """

    STRONG_UP = "STRONG_UP"
    UP = "UP"
    SIDEWAYS = "SIDEWAYS"
    DOWN = "DOWN"
    STRONG_DOWN = "STRONG_DOWN"
    UNKNOWN = "UNKNOWN"
    """Not enough warmed-up data to say. Never treated as SIDEWAYS: "no opinion"
    and "no trend" are different claims, and averaging them together is how a
    missing feature quietly becomes a neutral vote."""

    @property
    def direction(self) -> int:
        """-1, 0 or +1. UNKNOWN is 0 but is never *counted* as agreement."""
        return {
            TrendState.STRONG_UP: 1,
            TrendState.UP: 1,
            TrendState.SIDEWAYS: 0,
            TrendState.DOWN: -1,
            TrendState.STRONG_DOWN: -1,
            TrendState.UNKNOWN: 0,
        }[self]

    @property
    def is_known(self) -> bool:
        return self is not TrendState.UNKNOWN


class StructureState(StrEnum):
    """What price is doing relative to its recent range.

    Every one of these has a precise definition in
    :mod:`app.scanner.structure`. No subjective chart-pattern names: if it
    cannot be computed from OHLCV by a stated rule, it is not here.
    """

    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    CONSOLIDATION = "CONSOLIDATION"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class SessionPhase(StrEnum):
    """Where an instant falls relative to the venue's session."""

    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"

    @property
    def is_tradable(self) -> bool:
        """Whether tradabot will qualify a *new* signal in this phase.

        Regular session only, for now. The free IEX feed's extended-hours data is
        thin enough that spreads and volume read very differently from the
        regular session, and a scanner that qualified setups on it would be
        measuring the feed rather than the market. Evaluations are still recorded
        outside the session -- see docs/scanner.md.
        """
        return self is SessionPhase.REGULAR


class DataQuality(StrEnum):
    """Whether the inputs were good enough to act on."""

    OK = "OK"
    STALE = "STALE"
    """Newest bar older than the configured tolerance."""
    INSUFFICIENT = "INSUFFICIENT"
    """Not enough history to warm up the features."""
    MISSING = "MISSING"
    """No data at all for a required timeframe."""

    @property
    def is_actionable(self) -> bool:
        return self is DataQuality.OK
