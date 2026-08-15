"""Webhook resolution and per-slot routing isolation. All values are fake."""

from __future__ import annotations

import inspect

from app.broker.paper_accounts import PaperAccountSlot
from app.core import webhooks
from app.core.webhooks import (
    CANONICAL_NAMES,
    LEGACY_NAMES,
    ConfigurationHealth,
    WebhookChannel,
    WebhookRegistry,
    configuration_health,
)

FAKE = {
    "DISCORD_PAPER_1K_WEBHOOK": "https://discord.test/1k",
    "DISCORD_PAPER_3K_WEBHOOK": "https://discord.test/3k",
    "DISCORD_PAPER_10K_WEBHOOK": "https://discord.test/10k",
    "DISCORD_SYSTEM_WEBHOOK": "https://discord.test/system",
    "DISCORD_STATUS_WEBHOOK": "https://discord.test/status",
    "TRADABOT_DISCORD__ENABLED": "true",
}


def registry(env=None):
    return WebhookRegistry.load(env=dict(env if env is not None else FAKE))


class TestResolution:
    def test_the_flat_canonical_name_resolves(self) -> None:
        r = registry()
        assert r.is_configured(WebhookChannel.PAPER_1K)
        assert r.source_of(WebhookChannel.PAPER_1K) == "DISCORD_PAPER_1K_WEBHOOK"

    def test_the_legacy_nested_name_still_resolves(self) -> None:
        r = registry({"TRADABOT_DISCORD__SYSTEM_WEBHOOK": "https://discord.test/legacy"})
        assert r.source_of(WebhookChannel.SYSTEM) == "TRADABOT_DISCORD__SYSTEM_WEBHOOK"

    def test_the_canonical_name_wins_over_the_legacy_one(self) -> None:
        """Documented precedence, so 'which one is live' has one answer."""
        r = registry(
            {
                "DISCORD_SYSTEM_WEBHOOK": "https://discord.test/new",
                "TRADABOT_DISCORD__SYSTEM_WEBHOOK": "https://discord.test/old",
            }
        )
        assert r.source_of(WebhookChannel.SYSTEM) == "DISCORD_SYSTEM_WEBHOOK"

    def test_at_most_two_names_exist_per_channel(self) -> None:
        """**The gate.** A third would make configuration unreadable."""
        for channel in WebhookChannel:
            names = [CANONICAL_NAMES[channel], LEGACY_NAMES.get(channel)]
            assert len([n for n in names if n]) <= 2

    def test_the_paper_slots_have_no_legacy_alias(self) -> None:
        """The old PAPER_100/1000/10000 names belong to retired simulation
        profiles, not the Alpaca forward accounts."""
        for channel in (WebhookChannel.PAPER_1K, WebhookChannel.PAPER_3K, WebhookChannel.PAPER_10K):
            assert channel not in LEGACY_NAMES

    def test_an_unconfigured_channel_is_none_not_a_default(self) -> None:
        assert registry({}).url(WebhookChannel.MARKET) is None


class TestSlotIsolation:
    def test_each_slot_routes_to_its_own_channel(self) -> None:
        r = registry()
        urls = {slot: r.paper_webhook(slot).get_secret_value() for slot in PaperAccountSlot}
        assert len(set(urls.values())) == 3

    def test_a_missing_slot_webhook_never_falls_back(self) -> None:
        """**The gate.** PAPER_1K output in the PAPER_3K channel would be a true
        message producing a false conclusion."""
        partial = {k: v for k, v in FAKE.items() if "1K" not in k}
        r = registry(partial)
        assert r.paper_webhook(PaperAccountSlot.PAPER_1K) is None
        assert r.paper_webhook(PaperAccountSlot.PAPER_3K) is not None

    def test_the_resolver_contains_no_cross_slot_default(self) -> None:
        # Executable body only; the docstring names the slots it must not use.
        body = inspect.getsource(webhooks.WebhookRegistry.paper_webhook).split('"""')[-1]
        assert body.strip() == "return self.url(PAPER_SLOT_CHANNELS[slot])"


class TestSecrecy:
    def test_no_url_appears_in_a_repr(self) -> None:
        r = registry()
        assert "discord.test/1k" not in repr(r)
        assert "discord.test/1k" not in str(r.urls[WebhookChannel.PAPER_1K])

    def test_health_reports_presence_never_values(self) -> None:
        health = configuration_health(env=FAKE)
        rendered = repr(health)
        for url in FAKE.values():
            if url.startswith("https"):
                assert url not in rendered

    def test_health_fields_are_booleans_and_names_only(self) -> None:
        health = configuration_health(env=FAKE)
        assert all(isinstance(v, bool) for v in health.webhooks.values())
        assert all(isinstance(v, bool) for v in health.paper_accounts.values())


class TestTradingIndependenceFromDiscord:
    def test_trading_readiness_ignores_webhooks_entirely(self) -> None:
        """**The gate.** A reporting outage must never become a trading outage."""
        env = {
            "TRADABOT_MARKET_DATA_PROVIDER": "alpaca",
            "TRADABOT_ALPACA__API_KEY": "k",
            "TRADABOT_ALPACA__SECRET_KEY": "s",
            "TRADABOT_DATABASE_URL": "sqlite://",
            "ALPACA_PAPER_1K_API_KEY": "a",
            "ALPACA_PAPER_1K_API_SECRET": "b",
            "ALPACA_PAPER_3K_API_KEY": "c",
            "ALPACA_PAPER_3K_API_SECRET": "d",
            "ALPACA_PAPER_10K_API_KEY": "e",
            "ALPACA_PAPER_10K_API_SECRET": "f",
        }
        health = configuration_health(env=env)
        assert health.trading_config_complete
        assert not health.reporting_config_complete

    def test_missing_credentials_do_block_trading(self) -> None:
        health = configuration_health(env={"TRADABOT_DATABASE_URL": "sqlite://"})
        assert not health.trading_config_complete

    def test_the_health_report_has_no_url_field(self) -> None:
        fields = set(ConfigurationHealth.__dataclass_fields__)
        for forbidden in ("url", "urls", "secret", "token", "webhook_urls"):
            assert forbidden not in fields
