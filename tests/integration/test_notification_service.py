"""The notification service against a database.

Covers the two properties the whole phase rests on:

* a Discord failure never touches trading state;
* notification filtering never touches persistence.

Both are asserted by *doing the thing* -- persisting a trade with delivery
broken, and suppressing a notification while checking the row is still there --
rather than by inspecting the code that is supposed to guarantee them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import DiscordSettings, NotificationSettings, Settings
from app.core.events import Event, EventCategory, EventType
from app.db.models import NotificationAttempt, NotificationState, SignalRow
from app.domain.enums import (
    Classification,
    Horizon,
    PriceSeriesAdjustment,
    Timeframe,
)
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from tests.integration.test_paper_lifecycle import make_instrument

pytestmark = pytest.mark.integration

T0 = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)
HOOK = "https://discord.com/api/webhooks/111/secret-token-aaaa"


class CapturingBackend:
    """Records what it was asked to deliver."""

    name = "capturing"

    def __init__(self, *, succeed: bool = True) -> None:
        self.messages: list[NotificationMessage] = []
        self._succeed = succeed

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(
            backend=self.name,
            delivered=self._succeed,
            error=None if self._succeed else "backend refused",
        )


class ExplodingBackend:
    """A backend with a bug in it. Must not take anything else down."""

    name = "exploding"

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        msg = "backend is broken"
        raise RuntimeError(msg)


def make_settings(**overrides: object) -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", **overrides)  # type: ignore[arg-type]


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    """A session factory on the test engine.

    The service opens its **own** session for audit rows and policy state -- that
    separation is the design, not a test artefact -- so it needs a factory rather
    than the caller's session.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event_type", "category"),
    [
        (EventType.MARKET_SIGNAL_QUALIFIED, EventCategory.MARKET),
        (EventType.PAPER_TRADE_OPENED, EventCategory.PAPER_TRADE),
        (EventType.PAPER_TRADE_CLOSED, EventCategory.PAPER_TRADE),
        (EventType.DAILY_SIMULATION_SUMMARY, EventCategory.PERFORMANCE),
        (EventType.STALE_MARKET_DATA_DETECTED, EventCategory.SYSTEM),
        (EventType.CRITICAL_SYSTEM_ERROR, EventCategory.SYSTEM),
    ],
)
async def test_events_route_to_their_category(
    session: AsyncSession, event_type: EventType, category: EventCategory
) -> None:
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend])

    await service.publish(Event(type=event_type, occurred_at=T0, payload={"symbol": "NVDA"}))

    assert backend.messages[0].category is category


async def test_a_disabled_service_delivers_nothing(session: AsyncSession) -> None:
    backend = CapturingBackend()
    settings = make_settings(notifications=NotificationSettings(enabled=False))
    service = NotificationService(settings, backends=[backend])

    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    assert backend.messages == []
    assert not service.enabled


# ---------------------------------------------------------------------------
# Failure isolation -- the property everything else depends on
# ---------------------------------------------------------------------------
async def test_a_backend_that_raises_does_not_propagate(session: AsyncSession) -> None:
    """An exception escaping publish() is how a Discord outage becomes a lost trade."""
    service = NotificationService(make_settings(), backends=[ExplodingBackend()])

    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    assert service.last_error is not None


async def test_a_persisted_signal_survives_a_delivery_failure(
    session: AsyncSession,
) -> None:
    """Part X, asserted directly: the row is written and stays written.

    The signal is persisted first, exactly as a caller would; delivery then
    fails. If the two were coupled, the row would be missing -- and the future ML
    dataset would have a hole shaped like whatever Discord was doing that day.
    """
    instrument = await make_instrument(session, "NVDA")
    # A score of 60 is deliberately *below* the notification threshold: this is
    # exactly the case Part X protects. It is never announced, and it must still
    # be stored -- its future outcome is a training example either way.
    session.add(
        SignalRow(
            instrument_id=instrument.id,
            bar_timestamp=T0,
            generated_at=T0,
            timeframe=Timeframe.D1,
            horizon=Horizon.D5,
            price_adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            score=60.0,
            classification=Classification.NEUTRAL,
            confidence=0.5,
            reference_price=Decimal("100"),
            spread_bps=Decimal("10"),
            expected_move_bps=Decimal("100"),
            cost_bps=Decimal("20"),
            net_edge_bps=Decimal("80"),
            bars_used=200,
            engine_version="test",
            feature_snapshot={},
            components=[],
        )
    )
    await session.flush()

    service = NotificationService(make_settings(), backends=[ExplodingBackend()])
    await service.publish(
        Event.signal_event(
            event_type=EventType.MARKET_SIGNAL_QUALIFIED,
            symbol="NVDA",
            payload={},
            timeframe="1d",
            horizon="5d",
        )
    )

    stored = (
        (await session.execute(select(SignalRow).where(SignalRow.instrument_id == instrument.id)))
        .scalars()
        .all()
    )
    assert len(stored) == 1, "the signal must survive a broken notifier"


async def test_a_discord_outage_leaves_the_session_usable(session: AsyncSession) -> None:
    """A failed delivery must not poison the transaction a caller is inside."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    discord = DiscordSettings(
        enabled=True,
        system_webhook=SecretStr(HOOK),
        max_retries=0,
        backoff_base_seconds=0.001,
    )
    notifier = DiscordWebhookNotifier(
        discord, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    service = NotificationService(make_settings(), backends=[notifier])

    await service.publish(Event.critical_system_error(component="x", error="y"))

    # Still writable afterwards, which is what "did not poison the session" means.
    session.add(NotificationState(scope="probe", key="k", phase="none", updated_at=T0))
    await session.flush()


# ---------------------------------------------------------------------------
# Deduplication against persisted state (Part G)
# ---------------------------------------------------------------------------
async def test_signal_state_persists_across_service_instances(
    session: AsyncSession,
    factory: async_sessionmaker,
) -> None:
    """A restart must not re-announce every open signal.

    Two services sharing a database stand in for a process restart: the second
    one must remember what the first announced.
    """
    backend = CapturingBackend()
    payload: dict[str, object] = {"classification": "strong_bullish"}

    first = NotificationService(make_settings(), backends=[backend], session_factory=factory)
    assert await first.notify_signal(
        symbol="NVDA", timeframe="1d", horizon="5d", score=80.0, payload=payload
    )

    second = NotificationService(make_settings(), backends=[backend], session_factory=factory)
    notified = await second.notify_signal(
        symbol="NVDA", timeframe="1d", horizon="5d", score=81.0, payload=payload
    )

    assert not notified, "a fresh instance re-announced a signal it should remember"
    assert len(backend.messages) == 1


async def test_suppression_still_records_the_state(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """Policy state advances even when nothing is sent."""
    service = NotificationService(
        make_settings(), backends=[CapturingBackend()], session_factory=factory
    )

    await service.notify_signal(symbol="AMD", timeframe="1d", horizon="5d", score=50.0, payload={})

    async with factory() as check:
        state = await NotificationRepository(check).signal_state("AMD:1d:5d")
    assert state.score == 50.0


async def test_different_timeframes_are_independent_subjects(
    session: AsyncSession,
    factory: async_sessionmaker,
) -> None:
    """A daily signal must not suppress an intraday one for the same symbol."""
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    await service.notify_signal(symbol="NVDA", timeframe="1d", horizon="5d", score=80.0, payload={})
    await service.notify_signal(symbol="NVDA", timeframe="5m", horizon="2h", score=80.0, payload={})

    assert len(backend.messages) == 2


async def test_health_transitions_notify_once_then_recover(
    session: AsyncSession,
    factory: async_sessionmaker,
) -> None:
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    assert await service.notify_health(component="alpaca", healthy=False, error="down")
    assert not await service.notify_health(component="alpaca", healthy=False, error="down")
    assert await service.notify_health(component="alpaca", healthy=True)

    types = [m.event_type for m in backend.messages]
    assert types == [EventType.PROVIDER_DISCONNECTED, EventType.PROVIDER_RECOVERED]


# ---------------------------------------------------------------------------
# Audit (Part R)
# ---------------------------------------------------------------------------
async def test_a_delivery_is_recorded(session: AsyncSession, factory: async_sessionmaker) -> None:
    service = NotificationService(
        make_settings(), backends=[CapturingBackend()], session_factory=factory
    )

    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    async with factory() as check:
        attempts = await NotificationRepository(check).recent_attempts()
    assert len(attempts) == 1
    assert attempts[0].status == "delivered"
    assert attempts[0].event_type == EventType.TRADABOT_STARTED.value


async def test_a_failure_is_recorded_with_its_error(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """A missing alert must be visible rather than silent."""
    service = NotificationService(
        make_settings(), backends=[CapturingBackend(succeed=False)], session_factory=factory
    )

    await service.publish(Event.lifecycle(started=True, environment="test", provider="mock"))

    async with factory() as check:
        attempts = await NotificationRepository(check).recent_attempts()
    assert attempts[0].status == "failed"
    assert attempts[0].last_error == "backend refused"


async def test_an_attempt_row_never_stores_a_webhook(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """Only the category is stored -- a destination name, not a credential."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    discord = DiscordSettings(
        enabled=True, system_webhook=SecretStr(HOOK), max_retries=0, backoff_base_seconds=0.001
    )
    notifier = DiscordWebhookNotifier(
        discord, client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    service = NotificationService(make_settings(), backends=[notifier], session_factory=factory)

    await service.publish(Event.critical_system_error(component="x", error="y"))

    async with factory() as check:
        rows = (await check.execute(select(NotificationAttempt))).scalars().all()
    assert rows
    for row in rows:
        assert row.category == "system"
        assert HOOK not in (row.last_error or "")
        assert "secret-token" not in (row.last_error or "")


async def test_the_audit_reports_last_success_and_last_failure(
    session: AsyncSession,
    factory: async_sessionmaker,
) -> None:
    good = NotificationService(
        make_settings(), backends=[CapturingBackend()], session_factory=factory
    )
    bad = NotificationService(
        make_settings(), backends=[CapturingBackend(succeed=False)], session_factory=factory
    )

    await good.publish(Event.lifecycle(started=True, environment="t", provider="mock"))
    await bad.publish(Event.lifecycle(started=False, environment="t", provider="mock"))

    async with factory() as check:
        last_success, last_failure = await NotificationRepository(check).last_outcome()
    assert last_success is not None
    assert last_failure is not None


# ---------------------------------------------------------------------------
# Test command (Part O)
# ---------------------------------------------------------------------------
async def test_the_test_command_targets_every_category(session: AsyncSession) -> None:
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend])

    sent = await service.send_test()

    assert set(sent) == {c.value for c in EventCategory}
    assert {m.category for m in backend.messages} == set(EventCategory)


async def test_the_test_command_can_target_one_category(session: AsyncSession) -> None:
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend])

    sent = await service.send_test(EventCategory.MARKET)

    assert sent == ["market"]
    assert len(backend.messages) == 1


async def test_a_test_message_is_labelled_and_carries_no_secret(
    session: AsyncSession,
) -> None:
    backend = CapturingBackend()
    settings = make_settings(discord=DiscordSettings(enabled=True, market_webhook=SecretStr(HOOK)))
    service = NotificationService(settings, backends=[backend])

    await service.send_test(EventCategory.MARKET)

    text = backend.messages[0].rendered(4000)
    assert "TEST" in text
    assert HOOK not in text
    assert "secret-token" not in text


async def test_a_failed_signal_alert_is_retried_on_the_next_evaluation(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """The only retry this design offers, and the reason a dropped alert is not
    silently permanent.

    Delivery fails, so the "already announced" state is not committed. The next
    evaluation of the same symbol therefore sees the same transition and tries
    again -- rather than concluding it had already told someone.
    """
    failing = CapturingBackend(succeed=False)
    service = NotificationService(make_settings(), backends=[failing], session_factory=factory)

    assert not await service.notify_signal(
        symbol="NVDA", timeframe="1d", horizon="5d", score=80.0, payload={}
    )

    working = CapturingBackend()
    retry = NotificationService(make_settings(), backends=[working], session_factory=factory)
    notified = await retry.notify_signal(
        symbol="NVDA", timeframe="1d", horizon="5d", score=80.0, payload={}
    )

    assert notified, "a failed alert was treated as delivered"
    assert working.messages[0].event_type is EventType.MARKET_SIGNAL_QUALIFIED


async def test_a_successful_alert_is_not_repeated(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """The counterpart: once it arrives, the state does advance."""
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    await service.notify_signal(symbol="AMD", timeframe="1d", horizon="5d", score=80.0, payload={})
    await service.notify_signal(symbol="AMD", timeframe="1d", horizon="5d", score=81.0, payload={})

    assert len(backend.messages) == 1
