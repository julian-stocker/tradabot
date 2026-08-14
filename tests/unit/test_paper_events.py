"""Paper notification payloads. Nothing in this module may send anything."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

from app.broker.paper_accounts import PaperAccountSlot
from app.notifications import paper_events
from app.notifications.paper_events import (
    DailySummaryPayload,
    EntryPayload,
    ExitPayload,
    PaperEvent,
    RejectionAggregator,
    RiskEventPayload,
)

WHEN = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)


def summary(slot=PaperAccountSlot.PAPER_1K, rejections=None):
    return DailySummaryPayload(
        slot=slot,
        trading_date=WHEN,
        equity=Decimal("1000"),
        cash=Decimal("700"),
        gross_exposure=Decimal("300"),
        realized_pnl=Decimal("12"),
        unrealized_pnl=Decimal("-4"),
        total_cost=Decimal("1.5"),
        drawdown_pct=-0.03,
        open_positions=1,
        trades_today=2,
        rejections=rejections or {},
    )


class TestPaperMarking:
    def test_every_payload_is_marked_paper_and_names_its_account(self) -> None:
        """**The gate.** A message that could read as live trading is dangerous."""
        payloads = [
            EntryPayload(
                PaperAccountSlot.PAPER_1K,
                "LMT",
                Decimal("0.5"),
                Decimal("300"),
                Decimal("599"),
                "LOW_VOL",
                Decimal("10"),
                Decimal("15"),
                Decimal("0.3"),
            ),
            ExitPayload(
                PaperAccountSlot.PAPER_3K,
                "LMT",
                Decimal("601"),
                Decimal("1"),
                Decimal("0.7"),
                Decimal("0.3"),
                "3 sessions",
                "TAKE_PROFIT",
            ),
            RiskEventPayload(
                PaperAccountSlot.PAPER_10K, "LMT", "RISK_INCREASED", "HIGH_VOL", Decimal("3.4")
            ),
            summary(),
        ]
        for payload in payloads:
            rendered = payload.render()
            assert rendered.startswith("[PAPER] [")
            assert payload.slot.value in rendered

    def test_the_three_slots_are_distinguishable_in_output(self) -> None:
        rendered = {slot: summary(slot).render() for slot in PaperAccountSlot}
        assert len(set(rendered.values())) == 3

    def test_no_event_name_implies_an_instruction(self) -> None:
        for event in PaperEvent:
            assert "SELL" not in event.value
            assert "BUY" not in event.value

    def test_a_risk_event_states_it_implies_no_action(self) -> None:
        payload = RiskEventPayload(
            PaperAccountSlot.PAPER_1K, "LMT", "RISK_EXTREME", "EXTREME_VOL", Decimal("6")
        )
        assert "no action implied" in payload.render()


class TestBrokerAndEconomicPnlStaySeparate:
    def test_an_exit_reports_both_and_merges_neither(self) -> None:
        """Alpaca paper charges no commission; tradabot models spread and slippage."""
        payload = ExitPayload(
            PaperAccountSlot.PAPER_1K,
            "LMT",
            Decimal("601"),
            broker_pnl=Decimal("2.00"),
            economic_pnl=Decimal("1.40"),
            costs=Decimal("0.60"),
            holding_duration="3 sessions",
            exit_reason="TAKE_PROFIT",
        )
        rendered = payload.render()
        assert "broker P&L 2.00" in rendered
        assert "economic P&L 1.40" in rendered

    def test_the_payload_carries_no_combined_total(self) -> None:
        fields = set(ExitPayload.__dataclass_fields__)
        for forbidden in ("total_pnl", "net_pnl", "combined_pnl"):
            assert forbidden not in fields


class TestRejectionAggregation:
    def test_rejections_are_counted_not_announced(self) -> None:
        """**The gate.** One message per refusal would be hundreds a day."""
        agg = RejectionAggregator.empty()
        for _ in range(120):
            agg.record(PaperAccountSlot.PAPER_1K, "RISK_LIMIT")
        for _ in range(5):
            agg.record(PaperAccountSlot.PAPER_1K, "MAX_OPEN_POSITIONS")
        drained = agg.drain(PaperAccountSlot.PAPER_1K)
        assert drained == {"RISK_LIMIT": 120, "MAX_OPEN_POSITIONS": 5}
        assert summary(rejections=drained).rejected_total == 125

    def test_accounts_aggregate_independently(self) -> None:
        agg = RejectionAggregator.empty()
        agg.record(PaperAccountSlot.PAPER_1K, "RISK_LIMIT")
        agg.record(PaperAccountSlot.PAPER_3K, "MAX_OPEN_POSITIONS")
        assert agg.drain(PaperAccountSlot.PAPER_1K) == {"RISK_LIMIT": 1}
        assert agg.drain(PaperAccountSlot.PAPER_3K) == {"MAX_OPEN_POSITIONS": 1}
        assert agg.drain(PaperAccountSlot.PAPER_10K) == {}

    def test_draining_clears_so_a_day_is_not_double_counted(self) -> None:
        agg = RejectionAggregator.empty()
        agg.record(PaperAccountSlot.PAPER_1K, "RISK_LIMIT")
        agg.drain(PaperAccountSlot.PAPER_1K)
        assert agg.drain(PaperAccountSlot.PAPER_1K) == {}

    def test_the_summary_reports_zero_rejections_explicitly(self) -> None:
        assert "rejected 0" in summary().render()


def test_the_module_cannot_send_anything() -> None:
    """**The gate.** It builds strings; it holds no transport."""
    # Scanned below the module docstring, which legitimately says "no webhook".
    source = inspect.getsource(paper_events).split('"""', 2)[-1].lower()
    for forbidden in ("webhook", "httpx", "requests", "aiohttp", "async def send"):
        assert forbidden not in source
