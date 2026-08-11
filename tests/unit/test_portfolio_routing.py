"""Portfolio-aware Discord routing.

Offline: every request is an `httpx.MockTransport`. The assertions that matter
most are that routing follows **persistent portfolio identity** rather than
message content, and that no webhook reaches a log, an error or a plist.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import DiscordSettings, Settings
from app.core.events import Event, EventCategory, EventType, Severity
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.models import NotificationMessage
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles

# Fake, shaped like real webhooks so the redaction assertions mean something.
HOOKS = {
    "market": "https://discord.com/api/webhooks/1/market-tok-aaaa",
    "paper-100": "https://discord.com/api/webhooks/2/p100-tok-bbbb",
    "paper-1000": "https://discord.com/api/webhooks/3/p1000-tok-cccc",
    "paper-10000": "https://discord.com/api/webhooks/4/p10000-tok-dddd",
    "performance": "https://discord.com/api/webhooks/5/perf-tok-eeee",
    "system": "https://discord.com/api/webhooks/6/sys-tok-ffff",
}

T0 = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)


def make_discord(**overrides: object) -> DiscordSettings:
    defaults: dict[str, object] = {
        "enabled": True,
        "market_webhook": SecretStr(HOOKS["market"]),
        "performance_webhook": SecretStr(HOOKS["performance"]),
        "system_webhook": SecretStr(HOOKS["system"]),
        "paper_100_webhook": SecretStr(HOOKS["paper-100"]),
        "paper_1000_webhook": SecretStr(HOOKS["paper-1000"]),
        "paper_10000_webhook": SecretStr(HOOKS["paper-10000"]),
        "max_retries": 0,
        "backoff_base_seconds": 0.001,
    }
    return DiscordSettings(**(defaults | overrides))  # type: ignore[arg-type]


def message(
    *, routing_key: str | None, category: EventCategory = EventCategory.PAPER_TRADE, body: str = "b"
) -> NotificationMessage:
    return NotificationMessage(
        category=category,
        severity=Severity.TRADE,
        title="TITLE",
        body=body,
        event_type=EventType.PAPER_TRADE_OPENED,
        occurred_at=T0,
        routing_key=routing_key,
    )


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self._response = response or httpx.Response(204)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["paper-100", "paper-1000", "paper-10000"])
async def test_each_portfolio_posts_to_its_own_channel(key: str) -> None:
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    result = await notifier.send(message(routing_key=key))

    assert result.delivered
    assert str(recorder.requests[0].url) == HOOKS[key]


async def test_a_portfolio_never_receives_another_portfolios_message() -> None:
    """#paper-100 must not show what paper-10000 did."""
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    for key in PORTFOLIO_KEYS:
        await notifier.send(message(routing_key=key))

    urls = [str(request.url) for request in recorder.requests]
    assert urls == [HOOKS[key] for key in PORTFOLIO_KEYS]
    assert len(set(urls)) == len(PORTFOLIO_KEYS), "each went somewhere different"


async def test_routing_ignores_message_content() -> None:
    """Routing follows persistent identity, not what the message happens to say.

    A body naming a different portfolio must not change where it goes -- keying
    off text would break the moment the wording changed.
    """
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    await notifier.send(
        message(routing_key="paper-100", body="paper-10000 paper-1000 €10000 huge position")
    )

    assert str(recorder.requests[0].url) == HOOKS["paper-100"]


async def test_a_market_signal_still_goes_to_the_global_channel() -> None:
    """#market-signals stays global; no routing key means the category default."""
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    await notifier.send(message(routing_key=None, category=EventCategory.MARKET))

    assert str(recorder.requests[0].url) == HOOKS["market"]


async def test_performance_goes_to_the_performance_channel() -> None:
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    await notifier.send(message(routing_key=None, category=EventCategory.PERFORMANCE))

    assert str(recorder.requests[0].url) == HOOKS["performance"]


async def test_an_unconfigured_portfolio_reports_which_one() -> None:
    """'routing is broken' sends an operator hunting; naming the channel does not."""
    settings = make_discord(paper_1000_webhook=SecretStr(""), trades_webhook=SecretStr(""))
    notifier = DiscordWebhookNotifier(settings)

    result = await notifier.send(message(routing_key="paper-1000"))

    assert not result.delivered
    assert result.error is not None
    assert "paper-1000" in result.error


async def test_a_new_portfolio_routes_with_no_code_change() -> None:
    """Part B, asserted directly.

    ``paper-250`` exists nowhere in the codebase. It routes because the settings
    validator collects any ``PAPER_*_WEBHOOK`` generically.
    """
    hook = "https://discord.com/api/webhooks/9/p250-tok-gggg"
    recorder = Recorder()
    settings = make_discord(paper_250_webhook=SecretStr(hook))
    notifier = DiscordWebhookNotifier(settings, client=recorder.client())

    result = await notifier.send(message(routing_key="paper-250"))

    assert result.delivered
    assert str(recorder.requests[0].url) == hook


def test_the_legacy_channel_is_a_fallback_not_a_default() -> None:
    """An installation mid-migration keeps delivering rather than going quiet."""
    legacy = "https://discord.com/api/webhooks/7/legacy-tok"
    settings = make_discord(paper_100_webhook=SecretStr(""), trades_webhook=SecretStr(legacy))

    resolved = settings.webhook_for_portfolio("paper-100")

    assert resolved is not None
    assert resolved.get_secret_value() == legacy


def test_configured_portfolios_reports_names_never_urls() -> None:
    settings = make_discord()

    names = settings.configured_portfolios

    assert names == set(PORTFOLIO_KEYS)
    assert not any("discord.com" in name for name in names)


def test_missing_destinations_are_reported_by_name() -> None:
    settings = make_discord(paper_10000_webhook=SecretStr(""), trades_webhook=SecretStr(""))

    missing = settings.missing_portfolio_destinations(list(PORTFOLIO_KEYS))

    assert missing == ["paper-10000"]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------
def test_no_webhook_appears_in_a_settings_repr() -> None:
    rendered = repr(make_discord()) + str(make_discord().model_dump())

    for hook in HOOKS.values():
        assert hook not in rendered
    assert "p100-tok" not in rendered


async def test_a_failed_portfolio_delivery_redacts_the_url() -> None:
    recorder = Recorder(httpx.Response(500))
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    result = await notifier.send(message(routing_key="paper-100"))

    assert result.error is not None
    assert "p100-tok-bbbb" not in result.error
    assert notifier.last_error is not None
    assert "p100-tok-bbbb" not in notifier.last_error


async def test_mass_mentions_stay_suppressed_on_portfolio_channels() -> None:
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    await notifier.send(message(routing_key="paper-100", body="@everyone"))

    payload = json.loads(recorder.requests[0].content)
    assert payload["allowed_mentions"] == {"parse": []}


# ---------------------------------------------------------------------------
# Portfolio definitions
# ---------------------------------------------------------------------------
def test_the_three_portfolios_are_defined_from_data() -> None:
    profiles = build_personal_profiles()

    assert [p.name for p in profiles] == list(PORTFOLIO_KEYS)
    assert [float(p.initial_capital) for p in profiles] == [100.0, 1000.0, 10000.0]


def test_every_portfolio_carries_its_own_routing_key() -> None:
    """The key is stored on the profile, which is what makes routing persistent."""
    for profile in build_personal_profiles():
        assert profile.notification_channel == profile.name


def test_all_three_share_one_risk_profile() -> None:
    """Capital is the only variable, so a difference in outcome is attributable."""
    risk_names = {p.risk.name for p in build_personal_profiles()}

    assert risk_names == {"balanced"}


def test_capital_changes_the_position_cap_but_not_the_risk_rule() -> None:
    small, medium, large = build_personal_profiles()

    assert small.max_position_notional < medium.max_position_notional
    assert medium.max_position_notional < large.max_position_notional
    assert small.risk.max_position_percent == large.risk.max_position_percent


def test_the_generic_profiles_are_untouched() -> None:
    """Phase 3's nine profiles remain; these three are instances, not replacements."""
    from app.simulation.defaults import build_default_profiles

    generic = build_default_profiles()

    assert len(generic) == 9
    assert not (set(PORTFOLIO_KEYS) & {p.name for p in generic})


def test_a_routing_event_carries_the_key_not_the_capital() -> None:
    event = Event.paper_trade_opened(
        symbol="NVDA", payload={"symbol": "NVDA"}, routing_key="paper-1000"
    )

    assert event.routing_key == "paper-1000"
    assert event.category is EventCategory.PAPER_TRADE


def test_settings_expose_no_portfolio_capital_to_the_router() -> None:
    """Routing must not be derivable from capital -- that is the `if capital == 100`
    coupling Part B forbids."""
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:", discord=make_discord())

    assert not hasattr(settings.discord, "capital")
    assert set(settings.discord.portfolio_webhooks) == set(PORTFOLIO_KEYS)


# ---------------------------------------------------------------------------
# Third-party HTTP logging
# ---------------------------------------------------------------------------
def test_http_client_loggers_never_reach_info() -> None:
    """httpx logs the full request URL at INFO, and for a webhook that URL *is*
    the credential.

    Found in production: `make notify-test` printed six working webhook URLs to
    the terminal. tradabot's own redaction cannot help -- the record comes from a
    third-party logger and never passes through `app/core/redaction.py` -- so the
    only reliable fix is to stop it being emitted.
    """
    import logging

    from app.core.logging import _URL_LOGGING_LIBRARIES, configure_logging

    configure_logging(level="INFO", fmt="console")

    for name in _URL_LOGGING_LIBRARIES:
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), f"{name} would log request URLs"
        assert logger.isEnabledFor(logging.WARNING), f"{name} must still report problems"


def test_httpx_is_covered_by_name() -> None:
    """Guard the list itself: httpx is the client tradabot actually uses."""
    from app.core.logging import _URL_LOGGING_LIBRARIES

    assert "httpx" in _URL_LOGGING_LIBRARIES
    assert "httpcore" in _URL_LOGGING_LIBRARIES


async def test_a_real_delivery_emits_no_url_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end: deliver a message and confirm no URL appears in the log."""
    import logging

    from app.core.logging import configure_logging

    configure_logging(level="INFO", fmt="console")
    recorder = Recorder()
    notifier = DiscordWebhookNotifier(make_discord(), client=recorder.client())

    with caplog.at_level(logging.INFO):
        await notifier.send(message(routing_key="paper-100"))

    captured = caplog.text
    assert "discord.com/api/webhooks" not in captured
    assert "p100-tok-bbbb" not in captured
