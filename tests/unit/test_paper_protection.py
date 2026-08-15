"""Whole-share execution and broker-native protection.

The invariant these defend is the one Phase 12.5 refused to run without: a
filled position must never exist without protection attached at the broker. A
stop that lives only in a Python process disappears when the machine sleeps.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker import paper_orders
from app.broker.paper_accounts import PaperAccountSlot, PaperExecutionRefusedError
from app.broker.paper_orders import (
    ProtectedEntryPlan,
    build_protected_entry,
    whole_share_feasibility,
)
from app.market_data.calendars import get_trading_calendar
from app.strategy.holding_clock import HoldingState, holding_age


def plan(qty="2", stop="450", tp="550", slot=PaperAccountSlot.PAPER_1K):
    return ProtectedEntryPlan(
        slot=slot,
        symbol="MSFT",
        quantity=Decimal(qty),
        stop_price=Decimal(stop),
        take_profit_price=Decimal(tp) if tp else None,
        idempotency_key="paper:PAPER_1K:abc",
        candidate_id="abc",
    )


# ---------------------------------------------------------------------------
# Whole-share feasibility, applied after ranking
# ---------------------------------------------------------------------------
class TestWholeShareFeasibility:
    def test_a_risk_sized_quantity_is_floored_never_raised(self) -> None:
        """**The gate.** Rounding up breaches whichever cap produced the size."""
        result = whole_share_feasibility(
            risk_sized_quantity=Decimal("2.97"),
            price=Decimal("100"),
            max_notional=Decimal("300"),
        )
        assert result.whole_shares == Decimal("2")
        assert result.feasible

    def test_a_sub_one_share_size_is_infeasible_with_a_reason(self) -> None:
        result = whole_share_feasibility(
            risk_sized_quantity=Decimal("0.5008"),
            price=Decimal("599"),
            max_notional=Decimal("300"),
        )
        assert not result.feasible
        assert result.whole_shares == Decimal("0")
        assert "exceeds the 300 position allowance" in (result.shortfall_reason or "")

    def test_one_share_above_the_cap_is_infeasible(self) -> None:
        result = whole_share_feasibility(
            risk_sized_quantity=Decimal("1.4"),
            price=Decimal("400"),
            max_notional=Decimal("300"),
        )
        assert not result.feasible

    def test_feasibility_is_an_execution_constraint_not_a_ranking_filter(self) -> None:
        """**The gate.** Filtering expensive symbols before ranking would give
        the small account a different alpha model."""
        source = inspect.getsource(paper_orders.whole_share_feasibility)
        for forbidden in ("rank", "xs_rank", "universe", "match_b"):
            assert forbidden not in source.lower()


# ---------------------------------------------------------------------------
# Atomic protection
# ---------------------------------------------------------------------------
class TestProtectedEntry:
    def test_a_bracket_carries_both_canonical_levels(self) -> None:
        request = build_protected_entry(plan())
        assert str(request.order_class).lower().endswith("bracket")
        assert request.stop_loss.stop_price == 450.0
        assert request.take_profit.limit_price == 550.0

    def test_without_a_target_the_order_is_a_one_triggers_other(self) -> None:
        request = build_protected_entry(plan(tp=None))
        assert str(request.order_class).lower().endswith("oto")
        assert request.stop_loss.stop_price == 450.0

    def test_a_fractional_quantity_is_refused(self) -> None:
        """**The gate.** The SDK constructs a fractional bracket happily and the
        server refuses it — the result would be an unprotected position."""
        with pytest.raises(PaperExecutionRefusedError, match="whole-share orders only"):
            build_protected_entry(plan(qty="0.5"))

    def test_a_non_positive_stop_is_refused(self) -> None:
        with pytest.raises(PaperExecutionRefusedError, match="stop price"):
            build_protected_entry(plan(stop="0"))

    def test_the_entry_is_a_long_day_market_order(self) -> None:
        request = build_protected_entry(plan())
        assert str(request.side).lower().endswith("buy")
        assert str(request.time_in_force).lower().endswith("day")

    def test_the_idempotency_key_travels_to_the_broker(self) -> None:
        assert build_protected_entry(plan()).client_order_id == "paper:PAPER_1K:abc"

    def test_building_a_plan_sends_nothing(self) -> None:
        source = inspect.getsource(build_protected_entry)
        assert "submit_order" not in source

    def test_protection_levels_are_supplied_never_computed_here(self) -> None:
        """The stop comes from canonical exits widened by risk-v1's noise floor."""
        source = inspect.getsource(paper_orders.build_protected_entry)
        for forbidden in ("atr", "risk_band", "* 2", "multiple"):
            assert forbidden not in source.lower()


# ---------------------------------------------------------------------------
# The production holding clock
# ---------------------------------------------------------------------------
class TestHoldingClock:
    CAL = get_trading_calendar("XNYS")

    def age(self, entry: str, now: str):
        return holding_age(
            entry_filled_at=datetime.fromisoformat(entry + "T14:00:00+00:00"),
            now=datetime.fromisoformat(now + "T20:00:00+00:00"),
            calendar=self.CAL,
        )

    def test_the_horizon_is_three_market_sessions_not_three_days(self) -> None:
        """**The gate.** A Friday entry expires Wednesday, not Monday."""
        assert self.age("2026-08-07", "2026-08-07").sessions_held == 0
        assert self.age("2026-08-07", "2026-08-10").sessions_held == 1
        assert self.age("2026-08-07", "2026-08-12").state is HoldingState.EXPIRED
        assert self.age("2026-08-07", "2026-08-12").expiry_session.isoformat() == "2026-08-12"

    def test_a_weekend_does_not_age_a_position(self) -> None:
        friday_to_monday = self.age("2026-08-07", "2026-08-10")
        assert friday_to_monday.sessions_held == 1
        assert not friday_to_monday.should_exit

    def test_an_overdue_position_is_distinguishable_from_a_normal_expiry(self) -> None:
        """**The gate.** A two-week-old position must not report as a clean exit."""
        overdue = self.age("2026-08-07", "2026-08-21")
        assert overdue.state is HoldingState.OVERDUE
        assert overdue.sessions_overdue == 7
        assert overdue.should_exit

    def test_age_derives_from_timestamps_so_a_restart_cannot_reset_it(self) -> None:
        first = self.age("2026-08-07", "2026-08-12")
        again = self.age("2026-08-07", "2026-08-12")
        assert first.sessions_held == again.sessions_held == 3

    def test_the_clock_counts_no_process_executions(self) -> None:
        source = inspect.getsource(paper_orders).lower()
        assert "bars_processed" not in source
        from app.strategy import holding_clock

        body = inspect.getsource(holding_clock).split('"""', 2)[-1].lower()
        assert "bars_processed" not in body

    def test_an_unresolvable_calendar_fails_closed(self) -> None:
        class Broken:
            def session_date_for(self, moment):
                raise RuntimeError("no calendar")

        result = holding_age(
            entry_filled_at=datetime(2026, 8, 7, tzinfo=UTC),
            now=datetime(2026, 8, 12, tzinfo=UTC),
            calendar=Broken(),  # type: ignore[arg-type]
        )
        assert result.state is HoldingState.UNKNOWN
        assert not result.should_exit
