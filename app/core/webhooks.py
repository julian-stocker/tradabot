"""One canonical name per Discord channel, resolved once.

The problem this fixes
----------------------
``DiscordSettings`` reads nested ``TRADABOT_DISCORD__*`` variables. The current
``.env`` uses flat ``DISCORD_*`` names, so pydantic-settings — which applies the
``TRADABOT_`` prefix — silently ignored every webhook. Nothing raised; the
channels simply appeared unconfigured.

So resolution happens here, in one place, with a documented precedence:

1. the flat canonical name (``DISCORD_PAPER_1K_WEBHOOK``);
2. the legacy nested name (``TRADABOT_DISCORD__PAPER_100_WEBHOOK``), where one
   genuinely corresponds.

Two names per channel and no more. A third would make "which one is live?"
unanswerable without reading code.

Never a fallback between paper slots
------------------------------------
:func:`paper_webhook` returns ``None`` for an unconfigured slot. It does **not**
fall back to another slot's channel. Three accounts run the same strategy
simultaneously, so a PAPER_1K entry appearing in the PAPER_3K channel would be
indistinguishable from a real PAPER_3K entry — the reader would draw a false
conclusion from a true message.

Trading never depends on Discord
--------------------------------
A missing webhook is a *reporting* fault, not a trading fault. Delivery failure
must never freeze a slot or block an order, because that would make an outage in
a notification service into a trading outage. Configuration health is surfaced
through :func:`configuration_health` instead.

No value is ever returned in a repr, log or health report — only whether it is
present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from app.broker.paper_accounts import PaperAccountSlot


class WebhookChannel(StrEnum):
    """Every destination tradabot can post to."""

    PAPER_1K = "PAPER_1K"
    PAPER_3K = "PAPER_3K"
    PAPER_10K = "PAPER_10K"
    MARKET = "MARKET"
    TRENDS = "TRENDS"
    WATCH = "WATCH"
    BUY = "BUY"
    SELL = "SELL"
    PERFORMANCE = "PERFORMANCE"
    SYSTEM = "SYSTEM"
    STATUS = "STATUS"


CANONICAL_NAMES: Final[dict[WebhookChannel, str]] = {
    WebhookChannel.PAPER_1K: "DISCORD_PAPER_1K_WEBHOOK",
    WebhookChannel.PAPER_3K: "DISCORD_PAPER_3K_WEBHOOK",
    WebhookChannel.PAPER_10K: "DISCORD_PAPER_10K_WEBHOOK",
    WebhookChannel.MARKET: "DISCORD_MARKET_WEBHOOK",
    WebhookChannel.TRENDS: "DISCORD_TRENDS_WEBHOOK",
    WebhookChannel.WATCH: "DISCORD_MARKET_WATCH_WEBHOOK",
    WebhookChannel.BUY: "DISCORD_MARKET_BUY_WEBHOOK",
    WebhookChannel.SELL: "DISCORD_MARKET_SELL_WEBHOOK",
    WebhookChannel.PERFORMANCE: "DISCORD_PERFORMANCE_WEBHOOK",
    WebhookChannel.SYSTEM: "DISCORD_SYSTEM_WEBHOOK",
    WebhookChannel.STATUS: "DISCORD_STATUS_WEBHOOK",
}

LEGACY_NAMES: Final[dict[WebhookChannel, str]] = {
    WebhookChannel.MARKET: "TRADABOT_DISCORD__MARKET_WEBHOOK",
    WebhookChannel.TRENDS: "TRADABOT_DISCORD__TRENDS_WEBHOOK",
    WebhookChannel.WATCH: "TRADABOT_DISCORD__WATCH_WEBHOOK",
    WebhookChannel.BUY: "TRADABOT_DISCORD__BUY_WEBHOOK",
    WebhookChannel.SELL: "TRADABOT_DISCORD__SELL_EXIT_WEBHOOK",
    WebhookChannel.PERFORMANCE: "TRADABOT_DISCORD__PERFORMANCE_WEBHOOK",
    WebhookChannel.SYSTEM: "TRADABOT_DISCORD__SYSTEM_WEBHOOK",
    WebhookChannel.STATUS: "TRADABOT_DISCORD__STATUS_WEBHOOK",
}
"""Legacy nested names, kept only where the channel means the same thing.

The three **paper** slots have no legacy entry on purpose. The old
``TRADABOT_DISCORD__PAPER_100/1000/10000_WEBHOOK`` variables belong to the
retired 50/500/5000 EUR *simulation profiles*, not to the Alpaca forward
accounts. Mapping them across would silently attach a live experiment's output
to a channel named for something else.
"""

PAPER_SLOT_CHANNELS: Final[dict[PaperAccountSlot, WebhookChannel]] = {
    PaperAccountSlot.PAPER_1K: WebhookChannel.PAPER_1K,
    PaperAccountSlot.PAPER_3K: WebhookChannel.PAPER_3K,
    PaperAccountSlot.PAPER_10K: WebhookChannel.PAPER_10K,
}


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@dataclass(frozen=True, slots=True)
class WebhookRegistry:
    """Resolved destinations. Values are secret; presence is not."""

    urls: dict[WebhookChannel, SecretStr]
    sources: dict[WebhookChannel, str]
    enabled: bool = True

    @classmethod
    def load(
        cls, *, env: dict[str, str] | None = None, dotenv: Path | None = None
    ) -> WebhookRegistry:
        source: dict[str, str] = {}
        if dotenv is not None:
            source.update(_dotenv_values(dotenv))
        source.update(env if env is not None else os.environ)

        urls: dict[WebhookChannel, SecretStr] = {}
        sources: dict[WebhookChannel, str] = {}
        for channel in WebhookChannel:
            for name in (CANONICAL_NAMES[channel], LEGACY_NAMES.get(channel)):
                if name and source.get(name):
                    urls[channel] = SecretStr(source[name])
                    sources[channel] = name
                    break

        flag = (
            str(source.get("TRADABOT_DISCORD__ENABLED") or source.get("DISCORD_ENABLED", "false"))
            .strip()
            .lower()
        )
        return cls(urls=urls, sources=sources, enabled=flag in {"1", "true", "yes", "on"})

    def url(self, channel: WebhookChannel) -> SecretStr | None:
        """The destination, or ``None`` when unconfigured. **Never a fallback.**"""
        return self.urls.get(channel)

    def paper_webhook(self, slot: PaperAccountSlot) -> SecretStr | None:
        """One slot's channel, and only that slot's.

        Returning another slot's webhook would put PAPER_1K output into a
        channel a reader believes is PAPER_3K — a true message producing a false
        conclusion.
        """
        return self.url(PAPER_SLOT_CHANNELS[slot])

    def is_configured(self, channel: WebhookChannel) -> bool:
        return channel in self.urls

    def source_of(self, channel: WebhookChannel) -> str | None:
        """Which variable supplied it. The **name**, never the value."""
        return self.sources.get(channel)

    @property
    def missing(self) -> tuple[WebhookChannel, ...]:
        return tuple(c for c in WebhookChannel if c not in self.urls)

    @property
    def paper_slots_configured(self) -> bool:
        return all(self.is_configured(c) for c in PAPER_SLOT_CHANNELS.values())


@dataclass(frozen=True, slots=True)
class ConfigurationHealth:
    """Presence-only view of configuration. **Contains no secret.**"""

    market_data_provider: str
    market_data_credentials: bool
    database_configured: bool
    paper_accounts: dict[str, bool]
    webhooks: dict[str, bool]
    discord_enabled: bool

    @property
    def trading_config_complete(self) -> bool:
        """Whether *trading* can proceed. Discord is deliberately excluded --
        a reporting outage must never become a trading outage."""
        return (
            self.market_data_credentials
            and self.database_configured
            and all(self.paper_accounts.values())
        )

    @property
    def reporting_config_complete(self) -> bool:
        return self.discord_enabled and all(self.webhooks.values())


def configuration_health(
    *, env: dict[str, str] | None = None, dotenv: Path | None = None
) -> ConfigurationHealth:
    """Report what is configured, never what it is set to."""
    from app.broker.paper_accounts import PaperAccountRegistry  # noqa: PLC0415

    source: dict[str, str] = {}
    if dotenv is not None:
        source.update(_dotenv_values(dotenv))
    source.update(env if env is not None else os.environ)

    accounts = PaperAccountRegistry.load(env=env, dotenv=dotenv)
    hooks = WebhookRegistry.load(env=env, dotenv=dotenv)

    return ConfigurationHealth(
        market_data_provider=source.get("TRADABOT_MARKET_DATA_PROVIDER", "unset"),
        market_data_credentials=bool(
            source.get("TRADABOT_ALPACA__API_KEY")
            and (
                source.get("TRADABOT_ALPACA__SECRET_KEY")
                or source.get("TRADABOT_ALPACA__API_SECRET")
            )
        ),
        database_configured=bool(source.get("TRADABOT_DATABASE_URL")),
        paper_accounts={s.value: s in accounts.accounts for s in PaperAccountSlot},
        webhooks={c.value: hooks.is_configured(c) for c in WebhookChannel},
        discord_enabled=hooks.enabled,
    )
