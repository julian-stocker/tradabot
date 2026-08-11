"""The notification service.

``NotificationService`` **is an** :class:`~app.core.events.EventPublisher`. That
one fact is the whole integration: every place that already publishes an event
gains notifications by having a different publisher passed in at the composition
root. No domain service imports anything from this package, nothing calls
``send_discord(...)``, and turning notifications off is constructing a different
object.

Delivery reliability, honestly
------------------------------
This is **not** a transactional outbox, and does not claim to be. A real outbox
needs a relay that polls and retries independently of the request that wrote the
row, and the scheduler for that is explicitly deferred to the deployment
environment. Pretending otherwise would be worse than the simpler model.

What is guaranteed:

* **Trading state is never rolled back by a delivery failure.** ``publish`` catches
  everything. A backend that raised would be a bug in the backend, and it still
  would not escape this method.
* **Every attempt is recorded**, delivered or not, so a missing alert is visible
  rather than silent.
* **Duplicates are minimised** by the policy layer and by transition-based state.

What is *not* guaranteed: at-least-once delivery. If the process dies between the
business commit and the send, that notification is gone. The attempt row records
the failure, but it cannot be replayed from -- message *content* is deliberately
not stored, since it would duplicate the signal and trade tables and grow without
bound.

There is one partial recovery, and it is worth stating precisely because it is
easy to overstate: :meth:`notify_signal` only commits its "already announced"
state when delivery **succeeded**. So a failed signal alert is retried by the
next evaluation of that symbol, for as long as the condition persists. Nothing
retries a failed *system* alert; the next occurrence will alert again, and a
one-off that failed to deliver is lost.

That is at-most-once with an audit trail and opportunistic retry, and calling it
anything stronger would mislead whoever relies on it during an incident.

Ordering: notifications are sent **after** the business operation has committed.
Sending inside a transaction risks announcing a trade that then rolls back, and
announcing something that did not happen is worse than not announcing something
that did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.events import Event, EventCategory, EventType
from app.core.logging import get_logger
from app.core.redaction import redact, safe_message
from app.core.time import utc_now
from app.db.session import session_scope
from app.notifications.backends.console import ConsoleNotifier
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.formatters import format_event
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.policy import evaluate_health, evaluate_signal
from app.notifications.repository import NotificationRepository

logger = get_logger(__name__)


@runtime_checkable
class NotificationBackend(Protocol):
    """Somewhere a rendered message can be delivered.

    A `Protocol`, matching the project's other seams: backends are independent
    adapters sharing no implementation, so inheritance would buy nothing and a
    test double needs no registration.
    """

    @property
    def name(self) -> str: ...

    async def send(self, message: NotificationMessage) -> DeliveryResult: ...


class NotificationService:
    """Formats events, applies policy, delivers, and records the attempt."""

    def __init__(
        self,
        settings: Settings,
        *,
        backends: Sequence[NotificationBackend] | None = None,
        session_factory: async_sessionmaker | None = None,  # type: ignore[type-arg]
    ) -> None:
        """
        Args:
            settings: thresholds, and which backends to build by default.
            backends: explicit backends. Tests pass their own; production lets
                :func:`build_backends` decide from configuration.
            session_factory: for audit rows and policy state. **Its own session**,
                separate from any business transaction -- see the module
                docstring. Without it the service still works, and simply keeps
                no record and no cross-restart state.
        """
        self._settings = settings
        self._backends = list(backends) if backends is not None else build_backends(settings)
        self._session_factory = session_factory
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.notifications.enabled and bool(self._backends)

    @property
    def backend_names(self) -> tuple[str, ...]:
        return tuple(backend.name for backend in self._backends)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- EventPublisher ----------------------------------------------------

    async def publish(self, event: Event) -> bool:
        """Deliver an event as a notification. **Never raises.**

        Returns whether at least one backend accepted it, which is what lets
        :meth:`notify_signal` decline to remember an announcement that never
        arrived.

        The bare ``except`` is deliberate and is the point of the method: this is
        called from inside market-data sync and paper trading, and no failure
        here may propagate into either. A notification that cannot be sent is a
        logged warning; an exception escaping this method is a lost trade.
        """
        if not self.enabled:
            return False
        try:
            return await self._deliver(format_event(event))
        # Catching everything is the contract, not laziness: this runs inside
        # market-data sync and paper trading, and nothing here may reach them.
        except Exception as exc:
            self._last_error = safe_message(exc)
            logger.warning(
                "notification delivery raised; continuing",
                event_type=event.type.value,
                error=self._last_error,
            )
            return False

    async def _deliver(self, message: NotificationMessage) -> bool:
        """Send to every backend. Returns whether any accepted it."""
        delivered = False
        for backend in self._backends:
            result = _redacted(await backend.send(message))
            delivered = delivered or result.delivered
            if not result.delivered and result.error:
                self._last_error = result.error
            await self._record(message, result)
        return delivered

    async def _record(self, message: NotificationMessage, result: DeliveryResult) -> None:
        """Write the audit row in its own transaction.

        Separate from any business transaction on purpose: an audit row must not
        be able to fail a trade, and a rolled-back trade must not erase the
        record that its notification went out.
        """
        if self._session_factory is None:
            return
        try:
            async with session_scope(self._session_factory) as session:
                await NotificationRepository(session).record_attempt(message, result)
        # An audit row is a record of delivery, never a precondition for it.
        except Exception as exc:
            logger.warning("could not record notification attempt", error=safe_message(exc))

    # -- Policy-aware entry points -----------------------------------------

    async def notify_signal(
        self,
        *,
        symbol: str,
        timeframe: str,
        horizon: str,
        score: float,
        payload: dict[str, object],
    ) -> bool:
        """Announce a signal **only if** the policy says it is a transition.

        Returns whether a notification was sent.

        The caller has already persisted the signal before reaching here. This
        method cannot and must not affect that: a suppressed notification is a
        message not sent, never a row not written.
        """
        key = f"{symbol}:{timeframe}:{horizon}"
        if self._session_factory is None:
            # Without persistence there is no memory of what was announced, so
            # every evaluation would look like a first. Announcing on threshold
            # alone would spam; staying silent is the safer failure.
            logger.debug("no session factory; signal notifications suppressed", symbol=symbol)
            return False

        async with session_scope(self._session_factory) as session:
            state = await NotificationRepository(session).signal_state(key)

        decision = evaluate_signal(
            settings=self._settings.notifications, state=state, score=score, now=utc_now()
        )

        if not decision.notify or decision.event_type is None:
            # Nothing to send, so the new state is unconditionally correct.
            async with session_scope(self._session_factory) as session:
                await NotificationRepository(session).save_signal_state(key, decision.next_state)
            logger.debug("signal notification suppressed", symbol=symbol, reason=decision.reason)
            return False

        delivered = await self.publish(
            Event.signal_event(
                event_type=decision.event_type,
                symbol=symbol,
                payload={**payload, "symbol": symbol, "score": score},
                timeframe=timeframe,
                horizon=horizon,
            )
        )

        if not delivered:
            # Do not record an announcement that never arrived. Leaving the old
            # state in place means the next evaluation sees the same transition
            # and tries again -- the only retry this design offers, and the
            # reason a dropped alert is not silently permanent.
            logger.warning("signal notification not delivered; state not advanced", symbol=symbol)
            return False

        async with session_scope(self._session_factory) as session:
            await NotificationRepository(session).save_signal_state(key, decision.next_state)
        return True

    async def notify_health(
        self, *, component: str, healthy: bool, error: str | None = None
    ) -> bool:
        """Announce a health **transition** only.

        healthy -> unhealthy notifies, unhealthy -> unhealthy stays quiet,
        unhealthy -> healthy sends a recovery with the measured downtime.
        """
        key = f"provider:{component}"
        if self._session_factory is None:
            return False

        async with session_scope(self._session_factory) as session:
            repository = NotificationRepository(session)
            state = await repository.health_state(key)
            decision = evaluate_health(state=state, healthy=healthy, now=utc_now())
            # Health state advances regardless of delivery: it tracks what the
            # component is doing, not what was announced about it. Re-alerting on
            # every check because one message failed is the spam this prevents.
            await repository.save_health_state(key, decision.next_state)

        if not decision.notify:
            return False

        event = (
            Event.provider_recovered(provider=component, downtime_seconds=decision.downtime_seconds)
            if decision.recovered
            else Event.provider_disconnected(provider=component, error=error or "unreachable")
        )
        await self.publish(event)
        return True

    async def send_test(self, category: EventCategory | None = None) -> list[str]:
        """Send a clearly-labelled test message to each configured channel.

        Returns the categories attempted. Contains nothing secret: the payload
        names the channel and the environment, and neither is a credential.
        """
        categories = [category] if category is not None else list(EventCategory)
        sent: list[str] = []
        for target in categories:
            event = Event(
                type=EventType.NOTIFICATION_TEST,
                occurred_at=utc_now(),
                payload={
                    "channel": target.value,
                    "environment": self._settings.env.value,
                },
                key=f"test:{target.value}",
            )
            message = format_event(event)
            # Override the routing category: a test targets a channel directly
            # rather than being routed by what kind of event it is.
            await self._deliver(
                NotificationMessage(
                    category=target,
                    severity=message.severity,
                    title=message.title,
                    body=message.body,
                    event_type=message.event_type,
                    occurred_at=message.occurred_at,
                    key=message.key,
                    fields=message.fields,
                )
            )
            sent.append(target.value)
        return sent


def _redacted(result: DeliveryResult) -> DeliveryResult:
    """Scrub a backend's error text before it goes anywhere.

    A backend is an adapter, and its error string is whatever some third-party
    client produced -- frequently including the request URL, which for a webhook
    *is* the credential. The built-in Discord backend already redacts its own,
    but this is the boundary where any backend's text enters our audit table, our
    logs and our HTTP responses, so it is scrubbed once, here, for all of them.
    """
    if result.error is None:
        return result
    return replace(result, error=redact(result.error))


def build_backends(settings: Settings) -> list[NotificationBackend]:
    """Construct the configured backends.

    Discord is included only when it is both enabled **and** has at least one
    webhook. Enabling it with nothing configured would produce a "no webhook"
    failure for every event, filling the audit table with noise that looks like
    an outage.
    """
    backends: list[NotificationBackend] = []
    limit = settings.notifications.max_message_characters

    if settings.notifications.console:
        backends.append(ConsoleNotifier(max_characters=limit))

    if settings.discord.enabled:
        if settings.discord.is_configured:
            backends.append(DiscordWebhookNotifier(settings.discord, max_characters=limit))
        else:
            # Named channels, never values. An operator needs to know *what* is
            # missing; printing the URL of what is present would be the leak.
            logger.warning(
                "Discord is enabled but no webhook is configured; "
                "set TRADABOT_DISCORD__*_WEBHOOK or TRADABOT_DISCORD__ENABLED=false"
            )
    return backends
