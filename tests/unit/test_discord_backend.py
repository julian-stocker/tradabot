"""Discord webhook delivery: routing, retries, and secret hygiene.

**No network.** Every request is served by an `httpx.MockTransport`, so these run
offline and deterministically. The strongest assertions are about what never
appears anywhere: the webhook URL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import DiscordSettings, NotificationSettings, Settings
from app.core.events import EventCategory, EventType, Severity
from app.notifications.backends.console import ConsoleNotifier
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.models import NotificationMessage
from app.notifications.service import build_backends

# Fake, and shaped like a real webhook so the redaction assertions mean something.
MARKET_HOOK = "https://discord.com/api/webhooks/111/market-secret-token-aaaa"
TRADES_HOOK = "https://discord.com/api/webhooks/222/trades-secret-token-bbbb"
PERFORMANCE_HOOK = "https://discord.com/api/webhooks/333/perf-secret-token-cccc"
SYSTEM_HOOK = "https://discord.com/api/webhooks/444/system-secret-token-dddd"

T0 = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def make_settings(**overrides: object) -> DiscordSettings:
    """All four channels configured, with a retry policy that does not slow tests."""
    defaults: dict[str, object] = {
        "enabled": True,
        "market_webhook": SecretStr(MARKET_HOOK),
        "trades_webhook": SecretStr(TRADES_HOOK),
        "performance_webhook": SecretStr(PERFORMANCE_HOOK),
        "system_webhook": SecretStr(SYSTEM_HOOK),
        "max_retries": 2,
        "backoff_base_seconds": 0.001,
        "backoff_max_seconds": 0.002,
    }
    return DiscordSettings(**(defaults | overrides))  # type: ignore[arg-type]


def make_message(
    category: EventCategory = EventCategory.MARKET, body: str = "body"
) -> NotificationMessage:
    return NotificationMessage(
        category=category,
        severity=Severity.SIGNAL,
        title="TITLE",
        body=body,
        event_type=EventType.MARKET_SIGNAL_QUALIFIED,
        occurred_at=T0,
        key="NVDA:1d:5d",
    )


class Recorder:
    """A mock transport that records requests and replays scripted responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            return httpx.Response(204)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (EventCategory.MARKET, MARKET_HOOK),
        (EventCategory.PAPER_TRADE, TRADES_HOOK),
        (EventCategory.PERFORMANCE, PERFORMANCE_HOOK),
        (EventCategory.SYSTEM, SYSTEM_HOOK),
    ],
)
def test_each_category_routes_to_its_own_channel(category: EventCategory, expected: str) -> None:
    notifier = DiscordWebhookNotifier(make_settings())

    assert notifier.webhook_for(category) == expected


def test_an_unconfigured_category_has_no_webhook() -> None:
    """Configuring only the channels you care about is reasonable, not an error."""
    settings = make_settings(market_webhook=SecretStr(""))
    notifier = DiscordWebhookNotifier(settings)

    assert notifier.webhook_for(EventCategory.MARKET) is None
    assert notifier.webhook_for(EventCategory.SYSTEM) == SYSTEM_HOOK


async def test_a_market_event_is_posted_to_the_market_channel() -> None:
    recorder = Recorder(httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    result = await notifier.send(make_message(EventCategory.MARKET))

    assert result.delivered
    assert str(recorder.requests[0].url) == MARKET_HOOK


async def test_a_system_event_is_posted_to_the_system_channel() -> None:
    recorder = Recorder(httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    await notifier.send(make_message(EventCategory.SYSTEM))

    assert str(recorder.requests[0].url) == SYSTEM_HOOK


async def test_sending_to_an_unconfigured_category_is_skipped_not_failed() -> None:
    notifier = DiscordWebhookNotifier(make_settings(performance_webhook=SecretStr("")))

    result = await notifier.send(make_message(EventCategory.PERFORMANCE))

    assert not result.delivered
    assert result.attempts == 0, "nothing was attempted, so nothing was retried"


# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
async def test_a_204_counts_as_delivered() -> None:
    """Discord's success response for a webhook post carries no body."""
    recorder = Recorder(httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    result = await notifier.send(make_message())

    assert result.delivered
    assert result.status_code == 204
    assert result.attempts == 1


async def test_a_200_also_counts_as_delivered() -> None:
    recorder = Recorder(httpx.Response(200, json={"id": "1"}))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    assert (await notifier.send(make_message())).delivered


async def test_a_rate_limit_is_retried_then_succeeds() -> None:
    recorder = Recorder(httpx.Response(429, json={"retry_after": 0.001}), httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    result = await notifier.send(make_message())

    assert result.delivered
    assert len(recorder.requests) == 2


async def test_a_retry_after_header_is_honoured() -> None:
    """Discord knows when it will accept traffic; ignoring it earns a longer ban."""
    recorder = Recorder(httpx.Response(429, headers={"Retry-After": "0.001"}), httpx.Response(204))
    notifier = DiscordWebhookNotifier(
        make_settings(backoff_base_seconds=30.0, backoff_max_seconds=30.0),
        client=recorder.client(),
    )

    # Completing quickly despite a 30s backoff curve proves the header set the wait.
    result = await notifier.send(make_message())

    assert result.delivered


async def test_server_errors_are_retried_up_to_the_bound() -> None:
    recorder = Recorder(httpx.Response(503))
    notifier = DiscordWebhookNotifier(make_settings(max_retries=2), client=recorder.client())

    result = await notifier.send(make_message())

    assert not result.delivered
    assert len(recorder.requests) == 3, "max_retries=2 means three attempts"
    assert result.attempts == 3


async def test_a_permanent_client_error_is_not_retried() -> None:
    """A revoked webhook returns 404. Repeating it only delays the operator learning."""
    recorder = Recorder(httpx.Response(404))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    result = await notifier.send(make_message())

    assert not result.delivered
    assert len(recorder.requests) == 1


async def test_a_transport_failure_is_reported_not_raised() -> None:
    """Delivery must never raise: an exception here would reach a trading path."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(explode))
    notifier = DiscordWebhookNotifier(make_settings(max_retries=1), client=client)

    result = await notifier.send(make_message())

    assert not result.delivered
    assert result.error is not None


async def test_delivery_suppresses_mass_mentions() -> None:
    """A monitoring channel must not be able to ping a room."""
    recorder = Recorder(httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    await notifier.send(make_message(body="@everyone look at this"))

    payload = json.loads(recorder.requests[0].content)
    assert payload["allowed_mentions"] == {"parse": []}


async def test_a_long_message_is_truncated_rather_than_rejected() -> None:
    """Discord hard-limits content at 2000 characters.

    Failing delivery because an explanation had too many reasons would lose the
    alert entirely -- the worst outcome available.
    """
    recorder = Recorder(httpx.Response(204))
    notifier = DiscordWebhookNotifier(make_settings(), client=recorder.client())

    result = await notifier.send(make_message(body="x" * 5_000))

    assert result.delivered
    content = json.loads(recorder.requests[0].content)["content"]
    assert len(content) <= 2000
    assert content.startswith("TITLE"), "the important part survives truncation"


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------
def test_webhooks_do_not_appear_in_a_settings_repr() -> None:
    settings = make_settings()

    for rendered in (repr(settings), str(settings), str(settings.model_dump())):
        assert MARKET_HOOK not in rendered
        assert "market-secret-token" not in rendered


def test_configured_categories_names_channels_never_urls() -> None:
    settings = make_settings(trades_webhook=SecretStr(""))

    categories = settings.configured_categories

    assert categories == {"market", "performance", "system"}
    assert not any(MARKET_HOOK in c for c in categories)


async def test_a_failure_message_never_contains_the_webhook() -> None:
    """The error travels into a log line, an audit row and an HTTP response."""
    recorder = Recorder(httpx.Response(500))
    notifier = DiscordWebhookNotifier(make_settings(max_retries=0), client=recorder.client())

    result = await notifier.send(make_message())

    assert result.error is not None
    assert MARKET_HOOK not in result.error
    assert "market-secret-token" not in result.error
    assert notifier.last_error is not None
    assert "market-secret-token" not in notifier.last_error


async def test_a_transport_error_echoing_the_url_is_redacted() -> None:
    """httpx puts the request URL in some error strings. It must not survive."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {MARKET_HOOK}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(explode))
    notifier = DiscordWebhookNotifier(make_settings(max_retries=0), client=client)

    result = await notifier.send(make_message())

    assert result.error is not None
    assert "market-secret-token-aaaa" not in result.error


def test_the_logger_is_never_handed_a_webhook(caplog: pytest.LogCaptureFixture) -> None:
    """Nothing in the module formats a URL into a log call.

    Asserted structurally rather than by running: a source-level check catches a
    future edit that adds one, which a behavioural test on today's paths would not.
    """
    from pathlib import Path

    import app.notifications.backends.discord as module

    source = Path(module.__file__).read_text()
    logging_lines = [line for line in source.splitlines() if "logger." in line]

    assert logging_lines, "the module does log"
    assert not any("url" in line.lower() for line in logging_lines)


# ---------------------------------------------------------------------------
# Backend construction (Part P)
# ---------------------------------------------------------------------------
def base_settings(**overrides: object) -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:", **overrides)  # type: ignore[arg-type]


def test_disabled_discord_needs_no_credentials() -> None:
    """A fresh clone must run with no Discord server anywhere."""
    settings = base_settings()

    assert settings.discord.enabled is False
    assert build_backends(settings) == []


def test_enabling_discord_without_a_webhook_builds_no_backend() -> None:
    """Rather than failing every event with 'no webhook', which looks like an outage."""
    settings = base_settings(discord=DiscordSettings(enabled=True))

    assert build_backends(settings) == []


def test_enabling_discord_with_a_webhook_builds_the_backend() -> None:
    settings = base_settings(discord=make_settings())

    backends = build_backends(settings)

    assert [b.name for b in backends] == ["discord"]


def test_the_console_backend_is_independent_of_discord() -> None:
    settings = base_settings(notifications=NotificationSettings(console=True))

    backends = build_backends(settings)

    assert [b.name for b in backends] == ["console"]


async def test_the_console_backend_always_succeeds() -> None:
    result = await ConsoleNotifier().send(make_message())

    assert result.delivered
    assert result.backend == "console"
