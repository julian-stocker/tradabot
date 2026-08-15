"""A decision is permission to enter one session, not permission forever."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.broker.paper_accounts import PaperAccountSlot, PaperAccountState
from app.market_data.calendars import get_trading_calendar
from app.paper.execution_window import (
    ExecutionDisposition,
    execution_window,
    revalidate,
)

CAL = get_trading_calendar("XNYS")
FRIDAY = datetime(2026, 8, 7, 20, tzinfo=UTC)


def state(ok=True, cash="3000", equity="3000", bp=None):
    return PaperAccountState(
        slot=PaperAccountSlot.PAPER_3K,
        account_number="PA1",
        is_paper=ok,
        status="ACTIVE" if ok else "UNKNOWN",
        currency="USD",
        cash=Decimal(cash),
        equity=Decimal(equity),
        buying_power=Decimal(bp or equity),
        trading_blocked=not ok,
        account_blocked=not ok,
        open_positions=0,
        open_orders=0,
        error=None if ok else "APIError",
    )


def check(window, now, **over):
    base = {
        "stored_outcome": "READY_TO_SUBMIT",
        "window": window,
        "now": now,
        "state": state(),
        "proposed_notional": Decimal("900"),
        "risk_is_fresh": True,
        "has_open_position": False,
        "has_open_order": False,
        "asset_tradable": True,
    }
    base.update(over)
    return revalidate(**base)


class TestWindow:
    def test_a_friday_decision_enters_on_monday(self) -> None:
        """**The gate.** Decide at t, enter at t+1 — the frozen convention."""
        w = execution_window(decision_session=FRIDAY, calendar=CAL)
        assert w.intended_entry_session.isoformat() == "2026-08-10"
        assert w.decision_session.isoformat() == "2026-08-07"

    def test_the_window_is_exactly_one_session_wide(self) -> None:
        w = execution_window(decision_session=FRIDAY, calendar=CAL)
        assert w.valid_from.date() == w.expires_at.date()

    def test_before_the_open_the_decision_is_not_yet_actionable(self) -> None:
        w = execution_window(decision_session=FRIDAY, calendar=CAL)
        assert w.disposition_at(FRIDAY) is ExecutionDisposition.NOT_YET_OPEN

    def test_inside_the_window_it_is_ready(self) -> None:
        w = execution_window(decision_session=FRIDAY, calendar=CAL)
        assert w.is_open_at(w.valid_from + timedelta(minutes=5))

    def test_after_the_close_the_window_is_missed_and_never_reopens(self) -> None:
        """**The gate.** A late fill is not the hypothesis that was tested."""
        w = execution_window(decision_session=FRIDAY, calendar=CAL)
        for later in (timedelta(hours=1), timedelta(days=3), timedelta(days=14)):
            assert w.disposition_at(w.expires_at + later) is ExecutionDisposition.MISSED_WINDOW


class TestRevalidation:
    def window(self):
        return execution_window(decision_session=FRIDAY, calendar=CAL)

    def now(self):
        return self.window().valid_from + timedelta(minutes=5)

    def test_an_unchanged_world_permits_submission(self) -> None:
        assert check(self.window(), self.now()).may_submit

    def test_a_missed_window_refuses(self) -> None:
        w = self.window()
        assert check(w, w.expires_at + timedelta(days=2)).disposition is (
            ExecutionDisposition.MISSED_WINDOW
        )

    def test_a_non_actionable_stored_decision_refuses(self) -> None:
        result = check(self.window(), self.now(), stored_outcome="WHOLE_SHARE_NOT_FEASIBLE")
        assert result.disposition is ExecutionDisposition.INVALIDATED

    def test_an_unreachable_account_refuses(self) -> None:
        result = check(self.window(), self.now(), state=state(ok=False))
        assert result.disposition is ExecutionDisposition.ACCOUNT_CHANGED

    def test_stale_risk_refuses(self) -> None:
        result = check(self.window(), self.now(), risk_is_fresh=False)
        assert result.disposition is ExecutionDisposition.RISK_STALE

    def test_a_position_opened_since_the_decision_refuses(self) -> None:
        result = check(self.window(), self.now(), has_open_position=True)
        assert result.disposition is ExecutionDisposition.POSITION_CONFLICT

    def test_a_working_order_refuses(self) -> None:
        result = check(self.window(), self.now(), has_open_order=True)
        assert result.disposition is ExecutionDisposition.ORDER_CONFLICT

    def test_cash_spent_elsewhere_refuses(self) -> None:
        result = check(self.window(), self.now(), state=state(cash="100"))
        assert result.disposition is ExecutionDisposition.ACCOUNT_CHANGED

    def test_a_delisted_asset_refuses(self) -> None:
        result = check(self.window(), self.now(), asset_tradable=False)
        assert result.disposition is ExecutionDisposition.INVALIDATED

    def test_revalidation_never_resizes_a_decision(self) -> None:
        """**The gate.** Adjusting here would put a second sizer in the system."""
        import inspect

        from app.paper import execution_window as module

        source = inspect.getsource(module.revalidate)
        for forbidden in ("size_position", "quantity =", "notional ="):
            assert forbidden not in source
