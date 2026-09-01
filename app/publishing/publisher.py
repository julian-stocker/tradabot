"""Sending what the monitor decided was worth sending.

This module owns delivery and nothing else. It does not judge materiality, does
not deduplicate findings, does not apply cooldowns and does not rank — all four
belong to :mod:`app.monitoring`, which is handed the same events by every
channel. What happens here is routing, batching, idempotency and failure
handling.

Failure is contained here
-------------------------
:meth:`Publisher.publish_events` never raises. A Discord outage must not fail a
market-data sync, an Advisor calculation, a Portfolio Fit report or paper
accounting, and the only way to guarantee that is for the transport boundary to
return outcomes rather than throw them. Retries are bounded by the notifier's
own policy.

Silence is a valid outcome
--------------------------
A quiet day sends nothing. Not "nothing happened today" — nothing. A channel
that posts every day trains its reader to skim, and the one day something
matters it will be skimmed too.

Recovery is bounded
-------------------
A failed delivery is recorded as failed, not left unseen. If it were left unseen
the next pass would find it eligible again, and a day-long outage would
discharge a day of alerts the moment Discord returned. Instead the backlog is
summarised once, to the system channel, and dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.core.webhooks import WebhookChannel, WebhookRegistry
from app.monitoring.schemas import ChangeEvent, MonitoringRun
from app.notifications.models import DeliveryResult, NotificationMessage
from app.publishing import format as render
from app.publishing.channels import SYSTEM, channel_for
from app.publishing.ledger import DeliveryLedger, DeliveryStatus, event_id

logger = get_logger(__name__)


def _embed_characters(embed: dict[str, Any]) -> int:
    """Everything Discord counts toward its 6000-character embed budget."""
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    for entry in embed.get("fields", []):
        total += len(str(entry["name"])) + len(str(entry["value"]))
    total += len(str((embed.get("footer") or {}).get("text", "")))
    return total


RECOVERY_THRESHOLD = 10
"""Failed deliveries that must accumulate before recovery is summarised rather
than retried one by one."""

RECOVERY_HIGHLIGHTS = 5


@dataclass(slots=True)
class PublishOutcome:
    """What one publishing pass did. Counts and destinations, never URLs."""

    delivered: int = 0
    failed: int = 0
    suppressed_already_delivered: int = 0
    unrouted: int = 0
    not_configured: int = 0
    baselined: int = 0
    messages: int = 0
    destinations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def quiet(self) -> bool:
        return self.messages == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "failed": self.failed,
            "suppressed_already_delivered": self.suppressed_already_delivered,
            "unrouted": self.unrouted,
            "not_configured": self.not_configured,
            "baselined": self.baselined,
            "messages": self.messages,
            "destinations": sorted(set(self.destinations)),
            "errors": self.errors,
            "dry_run": self.dry_run,
            "quiet": self.quiet,
        }


class Publisher:
    """Routes monitored changes to Discord channels.

    Args:
        notifier: transport. ``None`` renders without sending, which is what the
            dry run uses.
        registry: canonical destination resolver.
        ledger: delivery memory, for idempotency and recovery.
        dry_run: render and record nothing; report what would have been sent.
    """

    def __init__(
        self,
        *,
        notifier: Any = None,
        registry: WebhookRegistry | None = None,
        ledger: DeliveryLedger | None = None,
        dry_run: bool = False,
    ) -> None:
        self._notifier = notifier
        self._registry = registry
        self._ledger = ledger if ledger is not None else DeliveryLedger()
        self._dry_run = dry_run
        self.rendered: list[dict[str, Any]] = []

    # ------------------------------------------------------------- delivery
    async def _send(self, channel: WebhookChannel, message: NotificationMessage) -> DeliveryResult:
        """Deliver one message. **Never raises into the caller.**"""
        from app.notifications.embeds import build_payload  # noqa: PLC0415

        payload = build_payload(message, max_characters=2000)
        embed = payload["embeds"][0]
        self.rendered.append(
            {
                "channel": channel.value,
                "title": message.title,
                "body": message.body,
                "fields": dict(message.fields),
                "colour": f"#{embed['color']:06X}",
                "timestamp": embed.get("timestamp"),
                "content": payload["content"],
                "characters": _embed_characters(embed),
                "severity": str(message.severity),
            }
        )
        if self._dry_run or self._notifier is None:
            return DeliveryResult(backend="dry-run", delivered=False, attempts=0)
        try:
            sent: DeliveryResult = await self._notifier.send_to(channel, message)
            return sent
        except Exception as exc:
            logger.warning("publish failed", channel=channel.value, error=type(exc).__name__)
            return DeliveryResult(
                backend="discord", delivered=False, error=type(exc).__name__, attempts=1
            )

    def _record(
        self,
        outcome: PublishOutcome,
        identity: str,
        channel: WebhookChannel,
        result: DeliveryResult,
        *,
        now: datetime,
        subject: str | None = None,
        kind: str | None = None,
    ) -> None:
        outcome.messages += 1
        outcome.destinations.append(channel.value)
        if self._dry_run:
            return
        if result.delivered:
            outcome.delivered += 1
            status = DeliveryStatus.DELIVERED
        elif result.attempts == 0 and result.error and "no webhook configured" in result.error:
            outcome.not_configured += 1
            status = DeliveryStatus.NOT_CONFIGURED
        else:
            outcome.failed += 1
            outcome.errors.append(result.error or "delivery failed")
            status = DeliveryStatus.DELIVERY_FAILED
        self._ledger.record(
            identity,
            channel.value,
            status,
            now=now,
            attempts=result.attempts,
            error=result.error,
            subject=subject,
            kind=kind,
        )

    # --------------------------------------------------------------- events
    async def publish_events(
        self, run: MonitoringRun, *, now: datetime | None = None
    ) -> PublishOutcome:
        """Publish one monitoring pass. Quiet passes send nothing at all."""
        when = now or datetime.now(UTC)
        outcome = PublishOutcome(dry_run=self._dry_run)
        if run.quiet:
            return outcome

        # Per channel, not globally: a first portfolio publish must not count as
        # this channel having been seen.
        routed = {c for c in (channel_for(e) for e in run.events) if c is not None}
        if routed and all(self._ledger.is_empty(c.value) for c in routed):
            # First publishing run against an existing monitoring baseline. The
            # monitor already reports level-based findings -- unusual volume,
            # unusual volatility, a large sector week -- on its very first pass,
            # because those are conditions rather than transitions. Sending them
            # would open the channel with a burst of history nobody asked for, so
            # they are recorded as seen and the channel starts quiet. The
            # monitoring baseline itself is unaffected.
            for event in run.events:
                channel = channel_for(event)
                if channel is None:
                    outcome.unrouted += 1
                    continue
                outcome.baselined += 1
                self._ledger.record(
                    event_id(event),
                    channel.value,
                    DeliveryStatus.BASELINE,
                    now=when,
                    subject=event.subject,
                    kind=str(event.kind),
                )
            self._ledger.flush()
            return outcome

        by_channel: dict[WebhookChannel, list[ChangeEvent]] = {}
        for event in run.events:
            channel = channel_for(event)
            if channel is None:
                outcome.unrouted += 1
                continue
            identity = event_id(event)
            if self._ledger.should_skip(identity, channel.value):
                outcome.suppressed_already_delivered += 1
                continue
            by_channel.setdefault(channel, []).append(event)

        for channel, events in by_channel.items():
            if len(events) > render.BURST_THRESHOLD:
                await self._publish_burst(outcome, channel, events, when)
            else:
                for event in events:
                    result = await self._send(channel, render.event_message(event))
                    self._record(
                        outcome,
                        event_id(event),
                        channel,
                        result,
                        now=when,
                        subject=event.subject,
                        kind=str(event.kind),
                    )
        self._ledger.flush()
        return outcome

    async def _publish_burst(
        self,
        outcome: PublishOutcome,
        channel: WebhookChannel,
        events: Sequence[ChangeEvent],
        now: datetime,
    ) -> None:
        """One ranked digest, but every event marked individually.

        Marking each one is what makes the digest idempotent: a rerun finds them
        all delivered and sends nothing, rather than resending a digest whose
        composition happens to differ by one row.
        """
        result = await self._send(channel, render.burst_message(events))
        outcome.messages += 1
        outcome.destinations.append(channel.value)
        if self._dry_run:
            return
        status = DeliveryStatus.DELIVERED if result.delivered else DeliveryStatus.DELIVERY_FAILED
        if result.delivered:
            outcome.delivered += 1
        else:
            outcome.failed += 1
            outcome.errors.append(result.error or "delivery failed")
        for event in events:
            self._ledger.record(
                event_id(event),
                channel.value,
                status,
                now=now,
                attempts=result.attempts,
                error=result.error,
                subject=event.subject,
                kind=str(event.kind),
            )

    # -------------------------------------------------------------- one-off
    async def publish_message(
        self,
        channel: WebhookChannel,
        message: NotificationMessage,
        *,
        identity: str,
        now: datetime | None = None,
    ) -> PublishOutcome:
        """Publish a single composed message — a newsletter, a portfolio update."""
        when = now or datetime.now(UTC)
        outcome = PublishOutcome(dry_run=self._dry_run)
        if self._ledger.should_skip(identity, channel.value):
            outcome.suppressed_already_delivered += 1
            return outcome
        result = await self._send(channel, message)
        self._record(outcome, identity, channel, result, now=when, subject=message.title)
        self._ledger.flush()
        return outcome

    # ------------------------------------------------------------- recovery
    async def reconcile(
        self, *, now: datetime | None = None, current: Sequence[ChangeEvent] = ()
    ) -> PublishOutcome:
        """Summarise a delivery backlog once, then clear it.

        Called after delivery starts succeeding again. Below the threshold the
        backlog is small enough to leave alone; above it, one notice goes to the
        system channel and the failed records are dropped so they can never be
        replayed as fresh alerts.
        """
        when = now or datetime.now(UTC)
        outcome = PublishOutcome(dry_run=self._dry_run)
        failures = self._ledger.pending_failures()
        if len(failures) < RECOVERY_THRESHOLD:
            return outcome

        still = list(current[:RECOVERY_HIGHLIGHTS])
        message = render.recovery_message(
            accumulated=len(failures), still_relevant=still, occurred_at=when
        )
        result = await self._send(SYSTEM, message)
        self._record(outcome, f"recovery:{when.date().isoformat()}", SYSTEM, result, now=when)
        if result.delivered and not self._dry_run:
            self._ledger.forget(failures)
        self._ledger.flush()
        return outcome

    # ---------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        """Publisher health, for the status dashboard. Counts only."""
        failures = self._ledger.pending_failures()
        return {
            "delivery": "DEGRADED" if failures else "HEALTHY",
            "pending_failed_deliveries": len(failures),
            "last_delivery": self._ledger.last_delivery(),
            "counts": self._ledger.counts(),
        }
