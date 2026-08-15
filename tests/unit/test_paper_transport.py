"""Paper event routing. Reporting must never be able to affect trading."""

from __future__ import annotations

import pytest

from app.broker.paper_accounts import PaperAccountSlot
from app.core.webhooks import WebhookChannel, WebhookRegistry
from app.notifications.paper_transport import PaperEventTransport

FAKE = {
    "DISCORD_PAPER_1K_WEBHOOK": "https://d.test/1k",
    "DISCORD_PAPER_3K_WEBHOOK": "https://d.test/3k",
    "DISCORD_PAPER_10K_WEBHOOK": "https://d.test/10k",
    "DISCORD_SYSTEM_WEBHOOK": "https://d.test/system",
    "TRADABOT_DISCORD__ENABLED": "true",
}


class FakeSender:
    def __init__(self, ok=True, raises=False):
        self.ok, self.raises = ok, raises
        self.sent: list[tuple[str, str]] = []

    def post(self, url, content):
        if self.raises:
            raise RuntimeError("discord down")
        self.sent.append((url, content))
        return self.ok


def transport(env=None, **kw):
    return PaperEventTransport(
        registry=WebhookRegistry.load(env=dict(env if env is not None else FAKE)),
        sender=FakeSender(**kw),
    )


class TestRouting:
    @pytest.mark.parametrize(
        ("slot", "expected"),
        [
            (PaperAccountSlot.PAPER_1K, "https://d.test/1k"),
            (PaperAccountSlot.PAPER_3K, "https://d.test/3k"),
            (PaperAccountSlot.PAPER_10K, "https://d.test/10k"),
        ],
    )
    def test_each_slot_posts_only_to_its_own_channel(self, slot, expected) -> None:
        t = transport()
        assert t.emit(slot, "PAPER_ENTRY", "body")
        assert t.sender.sent[0][0] == expected

    def test_infrastructure_events_go_to_the_system_channel(self) -> None:
        """An operator watching PAPER_3K wants trades, not on-call noise."""
        t = transport()
        for event in (
            "PAPER_ORDER_ERROR",
            "PAPER_SLOT_FROZEN",
            "PAPER_SLOT_RECOVERED",
            "PAPER_RECONCILIATION_ERROR",
        ):
            assert t.channel_for(PaperAccountSlot.PAPER_1K, event) is WebhookChannel.SYSTEM

    def test_a_missing_slot_webhook_drops_rather_than_reroutes(self) -> None:
        """**The gate.** Misfiling is worse than not sending."""
        partial = {k: v for k, v in FAKE.items() if "1K" not in k}
        t = transport(partial)
        assert not t.emit(PaperAccountSlot.PAPER_1K, "PAPER_ENTRY", "body")
        assert t.sender.sent == []
        assert t.dropped == 1

    def test_a_daily_summary_goes_to_its_own_slot_channel(self) -> None:
        t = transport()
        assert t.emit_daily_summary(PaperAccountSlot.PAPER_10K, "summary")
        assert t.sender.sent[0][0] == "https://d.test/10k"

    def test_a_summary_is_not_duplicated_across_channels(self) -> None:
        t = transport()
        t.emit_daily_summary(PaperAccountSlot.PAPER_3K, "summary")
        assert len(t.sender.sent) == 1


class TestDeliveryNeverAffectsTrading:
    def test_a_transport_exception_is_swallowed(self) -> None:
        """**The gate.** A Discord outage must not reach the order lifecycle."""
        t = transport(raises=True)
        assert t.emit(PaperAccountSlot.PAPER_1K, "PAPER_ENTRY", "body") is False
        assert t.dropped == 1

    def test_a_refused_post_is_reported_not_raised(self) -> None:
        t = transport(ok=False)
        assert t.emit(PaperAccountSlot.PAPER_3K, "PAPER_EXIT", "body") is False

    def test_emit_has_no_raising_path(self) -> None:
        import inspect

        from app.notifications import paper_transport

        # Body only; the docstring says "never raises".
        body = inspect.getsource(paper_transport.PaperEventTransport.emit).split('"""')[-1]
        assert "raise" not in body
        assert "except Exception" in body


class TestRejectionAggregation:
    def test_rejections_are_counted_and_never_sent_individually(self) -> None:
        """**The gate.** PAPER_1K refuses ~73% of candidates."""
        t = transport()
        for _ in range(200):
            t.record_rejection(PaperAccountSlot.PAPER_1K, "WHOLE_SHARE_NOT_FEASIBLE")
        assert t.sender.sent == []
        drained = t.rejections.drain(PaperAccountSlot.PAPER_1K)
        assert drained == {"WHOLE_SHARE_NOT_FEASIBLE": 200}

    def test_slots_aggregate_independently(self) -> None:
        t = transport()
        t.record_rejection(PaperAccountSlot.PAPER_1K, "A")
        t.record_rejection(PaperAccountSlot.PAPER_3K, "B")
        assert t.rejections.drain(PaperAccountSlot.PAPER_1K) == {"A": 1}
        assert t.rejections.drain(PaperAccountSlot.PAPER_10K) == {}


def test_no_webhook_url_is_ever_logged_or_rendered() -> None:
    t = transport()
    assert "d.test" not in repr(t.registry)
