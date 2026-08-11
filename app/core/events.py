"""Domain events and the publisher boundary.

Prepares for notification delivery (Discord, phase 3.5) **without implementing
any**. Nothing here sends anything; the default publisher discards.

Why a boundary now
------------------
The alternative -- calling a webhook from inside the Alpaca provider once
notifications arrive -- would put network I/O, retry policy and a second set of
credentials inside a class whose job is parsing bars. It would also make the
provider untestable without stubbing a notifier, and would mean a Discord outage
could fail a market-data sync.

So provider code *emits* events and knows nothing about delivery. A transport is
attached at the composition root.

Events are facts, not commands
------------------------------
``MarketDataSyncCompleted`` says what happened. It does not say "send a message".
A subscriber decides whether anyone cares -- which is what lets the same event
feed a Discord channel, a metrics counter and a log line without the emitter
knowing about any of them.

**No event carries a credential.** :meth:`Event.redacted_payload` is what a
transport is expected to serialise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.core.redaction import redact
from app.core.time import utc_now

logger = get_logger(__name__)

_SENSITIVE_KEYS = frozenset(
    {"api_key", "api_secret", "secret", "token", "password", "authorization"}
)


class EventType(StrEnum):
    """Events tradabot can emit.

    Declared ahead of their subscribers so that phase 3.5 wires up a transport
    rather than hunting for emit points.
    """

    MARKET_DATA_SYNC_COMPLETED = "MarketDataSyncCompleted"
    MARKET_DATA_SYNC_FAILED = "MarketDataSyncFailed"
    STALE_MARKET_DATA_DETECTED = "StaleMarketDataDetected"
    PROVIDER_DISCONNECTED = "ProviderDisconnected"


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened, with enough context to act on it."""

    type: EventType
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)

    def redacted_payload(self) -> dict[str, Any]:
        """The payload with credential-shaped keys masked.

        Transports must serialise *this*, never :attr:`payload`. A notification
        is the easiest place in a system to leak a secret into somewhere
        permanent and public.
        """
        return {
            key: ("***REDACTED***" if key.lower() in _SENSITIVE_KEYS else value)
            for key, value in self.payload.items()
        }

    # -- Constructors, so emitters do not hand-build payload dicts ---------

    @staticmethod
    def market_data_sync_completed(
        *, provider: str, symbols: int, inserted: int, failed: int
    ) -> Event:
        return Event(
            type=EventType.MARKET_DATA_SYNC_COMPLETED,
            occurred_at=utc_now(),
            payload={
                "provider": provider,
                "symbols": symbols,
                "inserted_bars": inserted,
                "failed_symbols": failed,
            },
        )

    @staticmethod
    def market_data_sync_failed(*, provider: str, symbol: str, error: str) -> Event:
        """A failure, with the message redacted at construction.

        Redacted here rather than at delivery so that *no* transport can publish
        an unredacted one -- including a future transport written by someone who
        never read this module.
        """
        return Event(
            type=EventType.MARKET_DATA_SYNC_FAILED,
            occurred_at=utc_now(),
            payload={"provider": provider, "symbol": symbol, "error": redact(error)},
        )

    @staticmethod
    def stale_market_data_detected(
        *, provider: str, symbol: str, age_seconds: float, limit_seconds: int
    ) -> Event:
        return Event(
            type=EventType.STALE_MARKET_DATA_DETECTED,
            occurred_at=utc_now(),
            payload={
                "provider": provider,
                "symbol": symbol,
                "age_seconds": round(age_seconds, 1),
                "limit_seconds": limit_seconds,
            },
        )

    @staticmethod
    def provider_disconnected(*, provider: str, error: str) -> Event:
        return Event(
            type=EventType.PROVIDER_DISCONNECTED,
            occurred_at=utc_now(),
            payload={"provider": provider, "error": redact(error)},
        )


@runtime_checkable
class EventPublisher(Protocol):
    """Somewhere to send events.

    Implementations must **not** raise on delivery failure. A notification is an
    observation about the system, and losing one is strictly better than failing
    the operation that produced it -- a Discord outage must not fail a data sync.
    """

    async def publish(self, event: Event) -> None: ...


class NullEventPublisher:
    """Discards everything. The default.

    Not a no-op for its own sake: it means every emit point is exercised in tests
    and in production long before a transport exists, so wiring one up later is
    configuration rather than code archaeology.
    """

    async def publish(self, event: Event) -> None:  # noqa: ARG002 -- protocol shape
        return None


class LoggingEventPublisher:
    """Writes events to the structured log.

    Useful in development to see what a future Discord channel would receive.
    Serialises the *redacted* payload.
    """

    async def publish(self, event: Event) -> None:
        logger.info("domain event", event_type=event.type.value, **event.redacted_payload())


class RecordingEventPublisher:
    """Keeps events in memory, for tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type is event_type]
