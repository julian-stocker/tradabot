"""Turning a named horizon into an actual future instant.

The whole file exists because "5 days later" is ambiguous and the two readings
differ by a lot. Adding ``timedelta(days=5)`` to a Friday afternoon lands on the
following Wednesday only if no holiday intervenes, and lands *inside a weekend*
whenever the observation is late in the week -- where there is no price at all.
Every day-denominated horizon here is resolved through the exchange calendar, so
``D5`` means five sessions the venue actually opened.

Intraday horizons are wall-clock, but still session-aware at the boundary: a
15-minute horizon evaluated eight minutes before the close does not resolve to a
price seven minutes into a market that is shut. Those roll to the next session's
open, and the resolved timestamp is recorded so the stretch is visible in the
data rather than hidden inside the label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from app.core.time import ensure_utc
from app.domain.enums import Horizon, Timeframe
from app.market_data.calendars import TradingCalendar

LABEL_POLICY_VERSION: Final = "labels-v1"
"""Bumped whenever resolution, MFE/MAE or barrier rules change.

Rows carrying different policy versions are not comparable and must never be
pooled into one statistic -- see :mod:`app.research.labels`.
"""

SUPPORTED_HORIZONS: Final[tuple[Horizon, ...]] = (
    Horizon.M15,
    Horizon.H1,
    Horizon.H4,
    Horizon.D1,
    Horizon.D3,
    Horizon.D5,
    Horizon.D20,
)
"""The horizons phase 5 labels, shortest first.

Ordering matters: a labelling run stops widening once a horizon has no future
data, and shorter horizons must therefore be attempted first.
"""

TRADING_DAY_HORIZONS: Final[frozenset[Horizon]] = frozenset(
    {Horizon.D1, Horizon.D3, Horizon.D5, Horizon.D20}
)
"""Horizons counted in sessions, not in 24-hour blocks."""

_TRADING_DAYS: Final[dict[Horizon, int]] = {
    Horizon.D1: 1,
    Horizon.D3: 3,
    Horizon.D5: 5,
    Horizon.D20: 20,
}

LABEL_TIMEFRAMES: Final[dict[Horizon, tuple[Timeframe, ...]]] = {
    Horizon.M15: (Timeframe.M5, Timeframe.M15),
    Horizon.H1: (Timeframe.M15, Timeframe.M5, Timeframe.H1),
    Horizon.H4: (Timeframe.H1, Timeframe.M15),
    Horizon.D1: (Timeframe.H1, Timeframe.D1),
    Horizon.D3: (Timeframe.D1,),
    Horizon.D5: (Timeframe.D1,),
    Horizon.D20: (Timeframe.D1,),
}
"""Which candle series measures each horizon, in order of preference.

Finer bars give a truer MFE/MAE -- a daily candle's high says the move happened
sometime that day, which is a much weaker statement than a 5-minute high. But
finer series are also the shortest ones in this database, so each horizon lists
fallbacks and the labeller takes the first that actually covers the window.
The choice is recorded on every label: a excursion measured on daily bars and
one measured on 5-minute bars are different measurements and must not be pooled
silently.
"""


@dataclass(frozen=True, slots=True)
class ResolvedHorizon:
    """Where a horizon lands, and how it got there."""

    horizon: Horizon
    target: datetime
    """The instant the horizon elapses."""
    rolled_to_next_session: bool
    """True if the naive target fell outside a session and was pushed forward."""

    @property
    def is_trading_day_based(self) -> bool:
        return self.horizon in TRADING_DAY_HORIZONS


def resolve(
    horizon: Horizon,
    *,
    reference: datetime,
    calendar: TradingCalendar,
) -> ResolvedHorizon | None:
    """The instant at which ``horizon`` elapses, measured from ``reference``.

    Returns ``None`` when the calendar cannot reach that far -- which is a real
    answer for a 20-session horizon near the end of the loaded calendar, not an
    error.
    """
    moment = ensure_utc(reference)
    if horizon in TRADING_DAY_HORIZONS:
        return _resolve_sessions(horizon, moment=moment, calendar=calendar)
    return _resolve_intraday(horizon, moment=moment, calendar=calendar)


def _resolve_sessions(
    horizon: Horizon, *, moment: datetime, calendar: TradingCalendar
) -> ResolvedHorizon | None:
    """N sessions ahead, measured to that session's close.

    The close rather than the same time-of-day: "the 3-day return" on a daily
    series is close-to-close, and anchoring to the observation's own clock time
    would make two signals on the same day resolve to different prices for no
    reason the data supports.
    """
    days = _TRADING_DAYS[horizon]
    session = calendar.add_trading_days(moment, days)
    if session is None:
        return None
    close = calendar.session_close(session)
    if close is None:
        return None
    return ResolvedHorizon(horizon=horizon, target=close, rolled_to_next_session=False)


def _resolve_intraday(
    horizon: Horizon, *, moment: datetime, calendar: TradingCalendar
) -> ResolvedHorizon | None:
    """Wall-clock ahead, rolled forward if it lands outside a session.

    Rolling to the *open* rather than clamping to the previous close matters: a
    4-hour horizon from 18:00 that clamped backwards would resolve to a price
    before the observation, producing a return whose sign is meaningless.
    """
    naive_target = moment + horizon.duration
    if calendar.is_open_at(naive_target):
        return ResolvedHorizon(horizon=horizon, target=naive_target, rolled_to_next_session=False)

    session = calendar.next_session(naive_target)
    if session is None:
        return None
    open_at = calendar.session_open(session)
    if open_at is None:
        return None
    # The next session's open can precede the naive target when that target sits
    # inside the session it belongs to but the calendar disagrees on the edge;
    # never resolve backwards.
    target = max(open_at, naive_target) if open_at < naive_target else open_at
    return ResolvedHorizon(horizon=horizon, target=target, rolled_to_next_session=True)


def horizon_window(
    horizon: Horizon,
    *,
    reference: datetime,
    calendar: TradingCalendar,
) -> tuple[datetime, datetime] | None:
    """The half-open interval a horizon's excursions are measured over.

    ``(reference, target]``: the reference bar itself is excluded because its
    range is already known at signal time and is not future information. Folding
    it in would let an MFE be "achieved" before the trade could exist.
    """
    resolved = resolve(horizon, reference=reference, calendar=calendar)
    if resolved is None:
        return None
    return ensure_utc(reference), resolved.target


def longest_horizon_span(horizon: Horizon) -> timedelta:
    """A generous upper bound on the wall-clock span of a horizon.

    Used only to size candle fetches. Deliberately over-estimates: a 20-session
    horizon can span a month once holidays are counted, and fetching too few
    bars would silently truncate an excursion window.
    """
    if horizon in TRADING_DAY_HORIZONS:
        return timedelta(days=_TRADING_DAYS[horizon] * 2 + 14)
    return horizon.duration * 2 + timedelta(days=4)


def epoch() -> datetime:
    """A fixed floor for open-ended queries."""
    return datetime(1970, 1, 1, tzinfo=UTC)
