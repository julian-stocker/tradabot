"""Every destination, proved offline with a fake backend.

**No test here reaches Discord.** A capturing backend records what would have
been sent, which is the only way to assert routing without a network and without
a webhook URL in a test file.

The question these answer is the one that matters when a channel is empty: does
silence mean healthy silence, or a missing integration? A routing test that
passes turns the first reading into the correct one.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import DiscordSettings, Environment, Settings
from app.core.events import Event, EventCategory, EventType, Severity
from app.core.time import utc_now
from app.notifications.demo import DEMO_MARKER, lifecycle_events
from app.notifications.embeds import COLOURS, build_embed, build_payload
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.service import NotificationService
from app.simulation.portfolios import PORTFOLIO_KEYS

pytestmark = pytest.mark.integration

HOOK = "https://discord.com/api/webhooks/{}/tok-{}"


class CapturingBackend:
    """Records messages instead of sending them."""

    name = "capturing"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(backend=self.name, delivered=True)

    def destinations(self) -> list[str | None]:
        return [message.routing_key for message in self.messages]


def settings() -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        log_level="WARNING",
        discord=DiscordSettings(
            enabled=True,
            market_webhook=SecretStr(HOOK.format(1, "market")),
            performance_webhook=SecretStr(HOOK.format(2, "perf")),
            system_webhook=SecretStr(HOOK.format(3, "sys")),
            paper_100_webhook=SecretStr(HOOK.format(4, "p100")),
            paper_1000_webhook=SecretStr(HOOK.format(5, "p1000")),
            paper_10000_webhook=SecretStr(HOOK.format(6, "p10000")),
        ),
    )


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


@pytest.fixture
def captured(factory: async_sessionmaker) -> tuple[NotificationService, CapturingBackend]:
    backend = CapturingBackend()
    return NotificationService(settings(), backends=[backend], session_factory=factory), backend


# ---------------------------------------------------------------------------
# Market signals
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "event_type",
    [
        EventType.MARKET_SIGNAL_QUALIFIED,
        EventType.MARKET_SIGNAL_STRENGTHENED,
        EventType.MARKET_SIGNAL_INVALIDATED,
    ],
)
async def test_market_lifecycle_events_route_to_market(
    captured: tuple[NotificationService, CapturingBackend], event_type: EventType
) -> None:
    service, backend = captured

    await service.publish(
        Event(type=event_type, occurred_at=utc_now(), payload={"symbol": "NVDA", "score": 80.0})
    )

    assert len(backend.messages) == 1
    assert backend.messages[0].category is EventCategory.MARKET


# ---------------------------------------------------------------------------
# Paper portfolios: open and close, per portfolio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("portfolio", list(PORTFOLIO_KEYS))
async def test_a_paper_open_routes_to_its_own_portfolio(
    captured: tuple[NotificationService, CapturingBackend], portfolio: str
) -> None:
    service, backend = captured

    await service.publish(
        Event.paper_trade_opened(
            symbol="NVDA", payload={"symbol": "NVDA", "profile": portfolio}, routing_key=portfolio
        )
    )

    assert backend.destinations() == [portfolio]
    assert backend.messages[0].category is EventCategory.PAPER_TRADE


@pytest.mark.parametrize("portfolio", list(PORTFOLIO_KEYS))
async def test_a_paper_close_routes_to_its_own_portfolio(
    captured: tuple[NotificationService, CapturingBackend], portfolio: str
) -> None:
    service, backend = captured

    await service.publish(
        Event.paper_trade_closed(
            symbol="NVDA",
            payload={"symbol": "NVDA", "profile": portfolio, "net_pnl": "1.00"},
            routing_key=portfolio,
        )
    )

    assert backend.destinations() == [portfolio]


async def test_one_portfolios_trade_never_reaches_another(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    """**The isolation that makes three channels worth having.**"""
    service, backend = captured

    for portfolio in PORTFOLIO_KEYS:
        await service.publish(
            Event.paper_trade_opened(
                symbol="NVDA", payload={"profile": portfolio}, routing_key=portfolio
            )
        )

    assert backend.destinations() == list(PORTFOLIO_KEYS)
    assert len(set(backend.destinations())) == len(PORTFOLIO_KEYS)


# ---------------------------------------------------------------------------
# Performance and system
# ---------------------------------------------------------------------------
async def test_the_daily_summary_routes_to_performance(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    service, backend = captured

    await service.publish(Event.daily_summary({"portfolios": []}))

    assert backend.messages[0].category is EventCategory.PERFORMANCE


async def test_a_provider_failure_routes_to_system(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    service, backend = captured

    await service.publish(
        Event(
            type=EventType.PROVIDER_DISCONNECTED,
            occurred_at=utc_now(),
            payload={"provider": "alpaca", "error": "timeout"},
        )
    )

    assert backend.messages[0].category is EventCategory.SYSTEM
    assert backend.messages[0].severity in (Severity.WARNING, Severity.CRITICAL)


async def test_a_provider_recovery_routes_to_system(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    service, backend = captured

    await service.publish(
        Event(
            type=EventType.PROVIDER_RECOVERED,
            occurred_at=utc_now(),
            payload={"provider": "alpaca"},
        )
    )

    assert backend.messages[0].category is EventCategory.SYSTEM


# ---------------------------------------------------------------------------
# Embeds and plaintext fallback
# ---------------------------------------------------------------------------
def _message(**fields: str) -> NotificationMessage:
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.SIGNAL,
        title="NVDA — STRONG BULLISH OPPORTUNITY",
        body="score 87.1",
        event_type=EventType.MARKET_SIGNAL_STRENGTHENED,
        occurred_at=utc_now(),
        fields=dict(fields),
    )


def test_an_embed_carries_title_colour_and_fields() -> None:
    embed = build_embed(_message(Score="87.1", Intraday="BULLISH"))

    assert embed["title"].startswith("NVDA")
    assert embed["color"] == COLOURS[Severity.SIGNAL]
    assert {field["name"] for field in embed["fields"]} == {"Score", "Intraday"}


def test_an_absent_value_produces_no_field() -> None:
    """**Never fabricate a field.**

    A blank "Price" is worse than no Price: the reader cannot tell an unknown
    from a zero.
    """
    embed = build_embed(_message(Score="87.1", Price=""))

    assert [field["name"] for field in embed["fields"]] == ["Score"]


def test_plaintext_is_always_sent_alongside_the_embed() -> None:
    """The fallback is not an afterthought: a client that renders no embed still
    shows the whole message."""
    payload = build_payload(_message(Score="87.1"), max_characters=2000)

    assert payload["content"]
    assert "NVDA" in payload["content"]
    assert payload["embeds"]


def test_embeds_can_be_disabled_without_losing_information() -> None:
    payload = build_payload(_message(Score="87.1"), max_characters=2000, use_embeds=False)

    assert "embeds" not in payload
    assert "NVDA" in payload["content"]


def test_an_embed_never_exceeds_discords_field_limit() -> None:
    crowded = _message(**{f"f{index}": str(index) for index in range(40)})

    embed = build_embed(crowded)

    assert len(embed["fields"]) <= 25


# ---------------------------------------------------------------------------
# The manual demo
# ---------------------------------------------------------------------------
async def test_the_demo_covers_every_destination(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    service, backend = captured

    for _destination, event in lifecycle_events(settings()):
        await service.publish(event)

    categories = {message.category for message in backend.messages}
    assert categories == {
        EventCategory.MARKET,
        EventCategory.PAPER_TRADE,
        EventCategory.PERFORMANCE,
        EventCategory.SYSTEM,
    }
    assert set(PORTFOLIO_KEYS) <= {key for key in backend.destinations() if key}


def test_every_demo_message_is_marked_as_a_test() -> None:
    """A synthetic STRONG opportunity that reads like a real one gets acted on."""
    for _destination, event in lifecycle_events(settings()):
        assert DEMO_MARKER in str(event.payload.get("title", ""))
        assert event.payload.get("demo") is True


def test_the_demo_uses_a_symbol_that_is_not_real() -> None:
    """Using NVDA would leave fabricated NVDA alerts in the channel history."""
    from app.scanner.universe import universe_symbols

    for _destination, event in lifecycle_events(settings()):
        symbol = event.payload.get("symbol")
        if symbol:
            assert symbol not in universe_symbols()


async def test_no_notification_payload_contains_a_credential(
    captured: tuple[NotificationService, CapturingBackend],
) -> None:
    service, backend = captured

    for _destination, event in lifecycle_events(settings()):
        await service.publish(event)

    for message in backend.messages:
        rendered = message.rendered(4000) + str(message.fields)
        for forbidden in ("discord.com", "tok-", "webhook", "secret"):
            assert forbidden not in rendered.lower()
