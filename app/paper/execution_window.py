"""When a decision may still be acted on, and when it has gone stale.

The economic meaning being protected
------------------------------------
The frozen research convention is: **decide at session t, enter at the open of
t+1**. Every measured result rests on it. A candidate submitted three days late
because the machine was offline is not the hypothesis being tested — it is a
different trade at a different price on different information, and letting it
through would quietly contaminate the forward record.

So a persisted ``READY_TO_SUBMIT`` decision is not permission to submit forever.
It is permission to submit **into one specific session**, and that permission
expires.

Why revalidation exists on top of the window
--------------------------------------------
Even inside the window the world moves: cash gets committed elsewhere, a position
opens in the same symbol, risk data goes stale, an account stops being reachable.
The decision recorded what was true when it was made; :func:`revalidate` asks
whether it is still true now. Anything that changed materially yields a
disposition other than ``READY_TO_SUBMIT``, and nothing is submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.broker.paper_accounts import PaperAccountState, effective_capital
from app.market_data.calendars import TradingCalendar, get_trading_calendar
from app.paper.fanout import DecisionOutcome

DEFAULT_EXCHANGE = "XNYS"


class ExecutionDisposition(StrEnum):
    """What may now be done with a previously stored decision."""

    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    MISSED_WINDOW = "MISSED_WINDOW"
    """The intended entry session has passed. **Never backdated.**"""

    NOT_YET_OPEN = "NOT_YET_OPEN"
    """Decided, but the entry session has not arrived. A normal overnight state."""

    INVALIDATED = "INVALIDATED"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"
    RISK_STALE = "RISK_STALE"
    POSITION_CONFLICT = "POSITION_CONFLICT"
    ORDER_CONFLICT = "ORDER_CONFLICT"


@dataclass(frozen=True, slots=True)
class ExecutionWindow:
    """The single session a decision may be executed into."""

    decision_session: date
    intended_entry_session: date
    valid_from: datetime
    expires_at: datetime

    def disposition_at(self, moment: datetime) -> ExecutionDisposition:
        if moment < self.valid_from:
            return ExecutionDisposition.NOT_YET_OPEN
        if moment > self.expires_at:
            return ExecutionDisposition.MISSED_WINDOW
        return ExecutionDisposition.READY_TO_SUBMIT

    def is_open_at(self, moment: datetime) -> bool:
        return self.disposition_at(moment) is ExecutionDisposition.READY_TO_SUBMIT


def execution_window(
    *, decision_session: datetime, calendar: TradingCalendar | None = None
) -> ExecutionWindow:
    """The window for a decision taken on ``decision_session``.

    Opens at the next session's open and closes at that same session's close.
    One session wide, deliberately: the research convention is the *next* open,
    so a two-session window would already be a different rule.
    """
    cal = calendar or get_trading_calendar(DEFAULT_EXCHANGE)
    entry_session = cal.next_session(decision_session)
    if entry_session is None:  # pragma: no cover -- calendar exhausted
        msg = f"no trading session follows {decision_session.isoformat()}"
        raise ValueError(msg)

    opens = cal.session_open(entry_session)
    closes = cal.session_close(entry_session)
    if opens is None or closes is None:  # pragma: no cover
        msg = f"session {entry_session.isoformat()} has no open/close"
        raise ValueError(msg)

    return ExecutionWindow(
        decision_session=cal.session_date_for(decision_session),
        intended_entry_session=entry_session,
        valid_from=opens,
        expires_at=closes,
    )


@dataclass(frozen=True, slots=True)
class RevalidationResult:
    """Whether a stored decision may still be submitted, and why not."""

    disposition: ExecutionDisposition
    detail: str = ""

    @property
    def may_submit(self) -> bool:
        return self.disposition is ExecutionDisposition.READY_TO_SUBMIT


def revalidate(  # noqa: PLR0911 -- one return per way the world can have changed;
    # merging any two would report the wrong reason for refusing to submit
    *,
    stored_outcome: str,
    window: ExecutionWindow,
    now: datetime,
    state: PaperAccountState,
    proposed_notional: object,
    risk_is_fresh: bool,
    has_open_position: bool,
    has_open_order: bool,
    asset_tradable: bool = True,
) -> RevalidationResult:
    """Re-check a stored decision against the world as it is now.

    Ordered cheapest-first, and every check is a reason to refuse rather than a
    reason to adjust: a decision whose premises changed is discarded, never
    resized. Resizing it here would put a second sizer in the system.
    """
    if stored_outcome != DecisionOutcome.READY_TO_SUBMIT.value:
        return RevalidationResult(
            ExecutionDisposition.INVALIDATED, f"stored decision was {stored_outcome}"
        )

    timing = window.disposition_at(now)
    if timing is not ExecutionDisposition.READY_TO_SUBMIT:
        return RevalidationResult(
            timing,
            f"entry session {window.intended_entry_session.isoformat()} "
            f"window is {window.valid_from.isoformat()}..{window.expires_at.isoformat()}",
        )

    if not state.can_execute:
        return RevalidationResult(
            ExecutionDisposition.ACCOUNT_CHANGED,
            f"account not executable (status={state.status}, error={state.error})",
        )
    if not risk_is_fresh:
        return RevalidationResult(
            ExecutionDisposition.RISK_STALE, "risk-v1 estimate is no longer fresh"
        )
    if has_open_position:
        return RevalidationResult(
            ExecutionDisposition.POSITION_CONFLICT, "a position is already open in this symbol"
        )
    if has_open_order:
        return RevalidationResult(
            ExecutionDisposition.ORDER_CONFLICT, "an order is already working for this symbol"
        )
    if not asset_tradable:
        return RevalidationResult(ExecutionDisposition.INVALIDATED, "asset is no longer tradable")

    capital = effective_capital(state)
    if proposed_notional is None:
        return RevalidationResult(ExecutionDisposition.INVALIDATED, "no proposed notional")
    if proposed_notional > capital.usable_cash:  # type: ignore[operator]
        return RevalidationResult(
            ExecutionDisposition.ACCOUNT_CHANGED,
            f"notional {proposed_notional} exceeds usable cash {capital.usable_cash}",
        )
    if proposed_notional > capital.max_exposure:  # type: ignore[operator]
        return RevalidationResult(
            ExecutionDisposition.ACCOUNT_CHANGED,
            f"notional {proposed_notional} exceeds the no-leverage cap {capital.max_exposure}",
        )

    return RevalidationResult(ExecutionDisposition.READY_TO_SUBMIT)
