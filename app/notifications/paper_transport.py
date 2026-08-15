"""Delivering paper events. **Delivery failure never touches trading.**

Routing is one hop: the slot picks its own webhook through the canonical
resolver, and there is no fallback between slots. A PAPER_1K entry appearing in
the PAPER_3K channel would be indistinguishable from a real PAPER_3K entry, so a
missing webhook drops the message rather than misfiling it.

Why every send is wrapped
-------------------------
:meth:`PaperEventTransport.emit` cannot raise. A Discord outage, a revoked
webhook or a network drop would otherwise propagate into the order lifecycle and
turn a *reporting* fault into a *trading* fault — freezing a slot, or worse,
interrupting a sequence between a cancel and a re-read. Reporting is downstream
of safety and must never be upstream of it.

Rejections are counted, not announced. Phase 12.7 measured PAPER_1K refusing
72.6% of candidates; one message per refusal would be hundreds a day in the
channel that also carries entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.broker.paper_accounts import PaperAccountSlot
from app.core.logging import get_logger
from app.core.webhooks import WebhookChannel, WebhookRegistry
from app.notifications.paper_events import PaperEvent, RejectionAggregator

logger = get_logger(__name__)

SYSTEM_EVENTS: frozenset[str] = frozenset(
    {"PAPER_ORDER_ERROR", "PAPER_RECONCILIATION_ERROR", "PAPER_SLOT_FROZEN", "PAPER_SLOT_RECOVERED"}
)
"""Infrastructure events go to the system channel, not a trading channel.

An operator watching PAPER_3K wants entries and exits; a frozen slot is an
on-call concern and belongs where the other operational failures are.
"""


class Sender(Protocol):
    """Minimal transport. Injected so tests never reach the network."""

    def post(self, url: str, content: str) -> bool: ...


@dataclass(slots=True)
class PaperEventTransport:
    """Routes one slot's events to that slot's channel, and errors to system."""

    registry: WebhookRegistry
    sender: Sender
    rejections: RejectionAggregator = field(default_factory=RejectionAggregator.empty)
    delivered: int = 0
    dropped: int = 0

    def channel_for(self, slot: PaperAccountSlot, event: str) -> WebhookChannel | None:
        """Which channel an event belongs to. Never another slot's."""
        if event in SYSTEM_EVENTS:
            return WebhookChannel.SYSTEM
        return {
            PaperAccountSlot.PAPER_1K: WebhookChannel.PAPER_1K,
            PaperAccountSlot.PAPER_3K: WebhookChannel.PAPER_3K,
            PaperAccountSlot.PAPER_10K: WebhookChannel.PAPER_10K,
        }[slot]

    def emit(self, slot: PaperAccountSlot, event: str, body: str) -> bool:
        """Deliver one event. **Returns; never raises.**

        A False result is a reporting failure and nothing more: the caller is a
        trading lifecycle and must not branch on it.
        """
        channel = self.channel_for(slot, event)
        if channel is None:
            self.dropped += 1
            return False

        url = self.registry.url(channel)
        if url is None:
            # Dropped rather than rerouted. Logged by channel *name*, never URL.
            self.dropped += 1
            logger.warning(
                "paper event not delivered: channel unconfigured",
                slot=slot.value,
                paper_event=event,
                channel=channel.value,
            )
            return False

        try:
            ok = self.sender.post(url.get_secret_value(), body)
        except Exception as exc:
            self.dropped += 1
            logger.warning(
                "paper event delivery failed",
                slot=slot.value,
                paper_event=event,
                error=type(exc).__name__,
            )
            return False

        if ok:
            self.delivered += 1
        else:
            self.dropped += 1
        return ok

    def record_rejection(self, slot: PaperAccountSlot, reason: str) -> None:
        """Count a refused candidate for the daily summary. Sends nothing."""
        self.rejections.record(slot, reason)

    def emit_daily_summary(self, slot: PaperAccountSlot, render: Any) -> bool:
        """One summary per slot, to that slot's own channel.

        Chosen ownership model: each account owns its channel end to end, so a
        reader of PAPER_1K sees that account's whole story and nothing else. The
        alternative -- one consolidated message to the performance channel --
        would put three accounts' figures side by side in a channel that already
        carries other content, and duplicating it across four destinations would
        make every number appear multiple times.
        """
        return self.emit(slot, PaperEvent.DAILY_SUMMARY.value, str(render))
