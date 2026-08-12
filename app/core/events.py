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


class EventCategory(StrEnum):
    """What kind of thing happened.

    The **routing key**. A transport maps a category to a destination (a Discord
    channel, a log stream, a metrics namespace) without knowing any individual
    event type. Adding an event type therefore does not touch routing, and adding
    a destination does not touch the emitters.

    Derived from the type via :data:`EVENT_CATEGORIES` rather than stored on the
    event, so an emitter cannot file the same event under two categories.
    """

    MARKET = "market"
    PAPER_TRADE = "paper_trade"
    PERFORMANCE = "performance"
    SYSTEM = "system"


class Severity(StrEnum):
    """How much attention an event deserves.

    Controls formatting and, later, routing. ``CRITICAL`` is deliberately rare:
    an alert level that fires often is an alert level nobody reads.
    """

    INFO = "info"
    SIGNAL = "signal"
    TRADE = "trade"
    WARNING = "warning"
    CRITICAL = "critical"


class EventType(StrEnum):
    """Events tradabot can emit.

    The market-data types predate notifications and are **reused, not renamed**:
    ``STALE_MARKET_DATA_DETECTED`` is the "market data stale" alert, and
    ``MARKET_DATA_SYNC_FAILED`` is the sync-failure alert. Introducing a parallel
    vocabulary for the notification layer would mean two names for one fact.
    """

    # -- Market data / provider health (phase 3b) --------------------------
    MARKET_DATA_SYNC_COMPLETED = "MarketDataSyncCompleted"
    MARKET_DATA_SYNC_FAILED = "MarketDataSyncFailed"
    STALE_MARKET_DATA_DETECTED = "StaleMarketDataDetected"
    PROVIDER_DISCONNECTED = "ProviderDisconnected"
    PROVIDER_RECOVERED = "ProviderRecovered"

    # -- Signals -----------------------------------------------------------
    MARKET_SIGNAL_QUALIFIED = "MarketSignalQualified"
    MARKET_SIGNAL_STRENGTHENED = "MarketSignalStrengthened"
    MARKET_SIGNAL_INVALIDATED = "MarketSignalInvalidated"
    MARKET_OVERVIEW = "MarketOverview"
    MARKET_TRENDS = "MarketTrends"
    """Descriptive market activity. **Never a recommendation** -- see
    :mod:`app.notifications.trends`."""

    # -- Paper trading -----------------------------------------------------
    PAPER_TRADE_OPENED = "PaperTradeOpened"
    PAPER_TRADE_CLOSED = "PaperTradeClosed"
    PAPER_TRADE_SKIPPED = "PaperTradeSkipped"

    # -- Performance -------------------------------------------------------
    PORTFOLIO_PERFORMANCE_SUMMARY = "PortfolioPerformanceSummary"
    DAILY_SIMULATION_SUMMARY = "DailySimulationSummary"

    # -- Lifecycle ---------------------------------------------------------
    TRADABOT_STARTED = "TradabotStarted"
    TRADABOT_STOPPED = "TradabotStopped"
    CRITICAL_SYSTEM_ERROR = "CriticalSystemError"
    NOTIFICATION_TEST = "NotificationTest"
    OPERATIONAL_STATUS = "OperationalStatus"
    """The #status dashboard. A heartbeat, not an alert -- failures still go to
    the system channel, and a dashboard that also alerted would mean an operator
    watching two channels for the same fact."""


EVENT_CATEGORIES: dict[EventType, EventCategory] = {
    EventType.MARKET_DATA_SYNC_COMPLETED: EventCategory.SYSTEM,
    EventType.MARKET_DATA_SYNC_FAILED: EventCategory.SYSTEM,
    EventType.STALE_MARKET_DATA_DETECTED: EventCategory.SYSTEM,
    EventType.PROVIDER_DISCONNECTED: EventCategory.SYSTEM,
    EventType.PROVIDER_RECOVERED: EventCategory.SYSTEM,
    EventType.TRADABOT_STARTED: EventCategory.SYSTEM,
    EventType.TRADABOT_STOPPED: EventCategory.SYSTEM,
    EventType.CRITICAL_SYSTEM_ERROR: EventCategory.SYSTEM,
    EventType.NOTIFICATION_TEST: EventCategory.SYSTEM,
    EventType.OPERATIONAL_STATUS: EventCategory.SYSTEM,
    EventType.MARKET_SIGNAL_QUALIFIED: EventCategory.MARKET,
    EventType.MARKET_SIGNAL_STRENGTHENED: EventCategory.MARKET,
    EventType.MARKET_SIGNAL_INVALIDATED: EventCategory.MARKET,
    EventType.MARKET_OVERVIEW: EventCategory.MARKET,
    EventType.MARKET_TRENDS: EventCategory.MARKET,
    EventType.PAPER_TRADE_OPENED: EventCategory.PAPER_TRADE,
    EventType.PAPER_TRADE_CLOSED: EventCategory.PAPER_TRADE,
    EventType.PAPER_TRADE_SKIPPED: EventCategory.PAPER_TRADE,
    EventType.PORTFOLIO_PERFORMANCE_SUMMARY: EventCategory.PERFORMANCE,
    EventType.DAILY_SIMULATION_SUMMARY: EventCategory.PERFORMANCE,
}

EVENT_SEVERITIES: dict[EventType, Severity] = {
    EventType.MARKET_DATA_SYNC_FAILED: Severity.WARNING,
    EventType.STALE_MARKET_DATA_DETECTED: Severity.WARNING,
    EventType.PROVIDER_DISCONNECTED: Severity.CRITICAL,
    EventType.CRITICAL_SYSTEM_ERROR: Severity.CRITICAL,
    EventType.MARKET_SIGNAL_QUALIFIED: Severity.SIGNAL,
    EventType.MARKET_SIGNAL_STRENGTHENED: Severity.SIGNAL,
    EventType.MARKET_SIGNAL_INVALIDATED: Severity.SIGNAL,
    EventType.PAPER_TRADE_OPENED: Severity.TRADE,
    EventType.PAPER_TRADE_CLOSED: Severity.TRADE,
    EventType.PAPER_TRADE_SKIPPED: Severity.TRADE,
}
"""Severity by type. Anything absent is :attr:`Severity.INFO` -- the default has
to be the quiet one, or severity stops meaning anything."""


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happened, with enough context to act on it."""

    type: EventType
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
    key: str | None = None
    """Stable identity for deduplication, e.g. ``"NVDA:1d:5d"``. Two events with
    the same type and key describe the same subject, which is what lets a policy
    suppress a repeat without inspecting the payload."""
    routing_key: str | None = None
    """Which destination within a category, e.g. ``"paper-100"``.

    Distinct from :attr:`key`: that identifies the *subject*, this identifies the
    *destination*. It comes from persistent portfolio identity, never from
    message content -- routing on what a message happens to say would break the
    moment the wording changed."""

    @property
    def category(self) -> EventCategory:
        """Routing key, derived from the type."""
        return EVENT_CATEGORIES.get(self.type, EventCategory.SYSTEM)

    @property
    def severity(self) -> Severity:
        return EVENT_SEVERITIES.get(self.type, Severity.INFO)

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
            key=f"provider:{provider}",
        )

    @staticmethod
    def provider_recovered(*, provider: str, downtime_seconds: float | None) -> Event:
        return Event(
            type=EventType.PROVIDER_RECOVERED,
            occurred_at=utc_now(),
            payload={"provider": provider, "downtime_seconds": downtime_seconds},
            key=f"provider:{provider}",
        )

    @staticmethod
    def signal_event(
        *,
        event_type: EventType,
        symbol: str,
        payload: dict[str, Any],
        timeframe: str,
        horizon: str,
    ) -> Event:
        """One of the three signal-lifecycle events.

        The key is ``symbol:timeframe:horizon`` because that triple is what makes
        two evaluations comparable. Keying on symbol alone would let a daily
        signal suppress an intraday one.
        """
        return Event(
            type=event_type,
            occurred_at=utc_now(),
            payload=payload,
            key=f"{symbol}:{timeframe}:{horizon}",
        )

    @staticmethod
    def paper_trade_opened(
        *, symbol: str, payload: dict[str, Any], routing_key: str | None = None
    ) -> Event:
        return Event(
            type=EventType.PAPER_TRADE_OPENED,
            occurred_at=utc_now(),
            payload=payload,
            key=f"trade-open:{routing_key or 'all'}:{symbol}",
            routing_key=routing_key,
        )

    @staticmethod
    def paper_trade_closed(
        *, symbol: str, payload: dict[str, Any], routing_key: str | None = None
    ) -> Event:
        return Event(
            type=EventType.PAPER_TRADE_CLOSED,
            occurred_at=utc_now(),
            payload=payload,
            key=f"trade-close:{routing_key or 'all'}:{symbol}",
            routing_key=routing_key,
        )

    @staticmethod
    def daily_summary(payload: dict[str, Any]) -> Event:
        return Event(
            type=EventType.DAILY_SIMULATION_SUMMARY,
            occurred_at=utc_now(),
            payload=payload,
        )

    @staticmethod
    def critical_system_error(*, component: str, error: str) -> Event:
        """An unhandled failure worth waking someone for.

        The message is redacted here: a stack-trace-adjacent string is exactly
        where a connection URL or a key tends to appear.
        """
        return Event(
            type=EventType.CRITICAL_SYSTEM_ERROR,
            occurred_at=utc_now(),
            payload={"component": component, "error": redact(error)},
            key=f"error:{component}",
        )

    @staticmethod
    def lifecycle(*, started: bool, environment: str, provider: str) -> Event:
        return Event(
            type=EventType.TRADABOT_STARTED if started else EventType.TRADABOT_STOPPED,
            occurred_at=utc_now(),
            payload={"environment": environment, "provider": provider},
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
