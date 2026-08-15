"""How old a position is, in **market sessions**. Not in process executions.

The defect this replaces
------------------------
Simulation measures holding age as ``portfolio.bars_processed - entry_bar_index``
— a counter incremented once per ``process_bar`` call. That is a count of *how
many times a loop ran*, which is exactly right inside a replay and completely
wrong in production, where it would mean:

* a process restart resets the age to zero and the position is held forever;
* a day the machine was asleep does not age the position at all, so a
  three-session trade silently becomes a two-week one;
* running the job twice in one session ages the position twice.

Phase 11.4 already hit the third case from the other direction and forced me to
hand-manage ``advance_clock``. In production there is no loop to manage.

What replaces it
----------------
Age is derived from two timestamps and an exchange calendar: the session the
entry filled in, and the session being evaluated now. Nothing is stored except
the entry instant, so a restart cannot lose it and a missed run cannot skip it.

match-b-v1's horizon is **3 market sessions** — not 72 hours, and not 3 calendar
days. A Friday entry expires on Wednesday, and a holiday extends it by a day,
because that is what "3 sessions" means.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.strategy.match_b import HORIZON_SESSIONS

DEFAULT_EXCHANGE = "XNYS"


class HoldingState(StrEnum):
    """Where a position sits against its frozen horizon."""

    HOLDING = "HOLDING"
    EXPIRED = "EXPIRED"
    """The horizon has elapsed; the canonical MAX_HOLDING_PERIOD exit applies."""

    OVERDUE = "OVERDUE"
    """Past expiry by more than a session — the exit did not run when it should.

    A separate state from ``EXPIRED`` on purpose. Both call for the same exit,
    but ``OVERDUE`` also means the system failed to act, and an operator needs to
    see that rather than have a two-week-old position reported as a normal exit.
    """

    UNKNOWN = "UNKNOWN"
    """The entry session could not be resolved. Fails closed: no exit is derived
    from a clock that cannot say what day it is."""


@dataclass(frozen=True, slots=True)
class HoldingAge:
    """A position's age, expressed the only way the horizon is defined."""

    entry_session: date | None
    current_session: date | None
    sessions_held: int
    expiry_session: date | None
    state: HoldingState
    horizon_sessions: int = HORIZON_SESSIONS

    @property
    def should_exit(self) -> bool:
        """Whether the canonical time exit applies. ``UNKNOWN`` never does."""
        return self.state in (HoldingState.EXPIRED, HoldingState.OVERDUE)

    @property
    def sessions_overdue(self) -> int:
        if self.state is not HoldingState.OVERDUE:
            return 0
        return self.sessions_held - self.horizon_sessions


def holding_age(
    *,
    entry_filled_at: datetime,
    now: datetime,
    calendar: TradingCalendar | None = None,
    horizon: int = HORIZON_SESSIONS,
) -> HoldingAge:
    """Age a position from its fill instant to now, counted in sessions.

    Args:
        entry_filled_at: when the **broker** reported the fill. Not when the
            candidate was generated and not when the order was submitted — an
            order that sat unfilled overnight has not been held for a session.
        now: the moment being evaluated.
        calendar: exchange calendar. Defaults to NYSE.
        horizon: sessions to hold. Defaults to the frozen match-b-v1 horizon and
            should not be passed in production.

    Returns:
        A :class:`HoldingAge`. An unresolvable session yields ``UNKNOWN`` rather
        than a guess, because a wrong exit date is worse than a missing one.
    """
    cal = calendar or get_trading_calendar(DEFAULT_EXCHANGE)

    try:
        entry_session = cal.session_date_for(entry_filled_at)
        current_session = cal.session_date_for(now)
        expiry = cal.add_trading_days(entry_filled_at, horizon)
    except Exception:
        # fails closed: no exit is derived from a clock that cannot say what day
        # it is. A wrong exit date is worse than a missing one.
        return HoldingAge(
            entry_session=None,
            current_session=None,
            sessions_held=0,
            expiry_session=None,
            state=HoldingState.UNKNOWN,
            horizon_sessions=horizon,
        )

    # Sessions *elapsed since* entry, so the entry session itself is session 0 --
    # a position opened and evaluated the same day has been held zero sessions.
    held = max(0, cal.count_sessions(entry_filled_at, now) - 1)

    if held < horizon:
        state = HoldingState.HOLDING
    elif held == horizon:
        state = HoldingState.EXPIRED
    else:
        state = HoldingState.OVERDUE

    return HoldingAge(
        entry_session=entry_session,
        current_session=current_session,
        sessions_held=held,
        expiry_session=expiry,
        state=state,
        horizon_sessions=horizon,
    )
