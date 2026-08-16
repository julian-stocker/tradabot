"""Bot credentials and identity, resolved once and never printed.

A bot token is not like a webhook URL: it is the whole account. Anyone holding
it can read every channel the bot can see, post as the bot, and stay connected
until it is revoked. So it is a :class:`~pydantic.SecretStr` from the moment it
is read, it never enters a log line, an exception message, a report or an
artifact, and the only way to reach the real value is an explicit
``.get_secret_value()`` call that greps as a visible line of code.

The three identifiers -- application, guild, channel -- are not secrets, but
they are not published either. They are Discord snowflakes, and this module
validates their shape rather than trusting a pasted string: a channel ID with a
stray character silently becomes "this command works nowhere", which looks
exactly like a bot that is broken for some other reason.

Separate from the webhook publisher
-----------------------------------
:class:`~app.core.webhooks.WebhookRegistry` resolves the passive publishing
destinations and is untouched by any of this. The bot is a second, interactive
transport that happens to talk to the same service; merging their configuration
would mean a bot misconfiguration could silence the weekly newsletter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr

APPLICATION_ID_ENV: Final = "DISCORD_APPLICATION_ID"
BOT_TOKEN_ENV: Final = "DISCORD_BOT_TOKEN"
GUILD_ID_ENV: Final = "DISCORD_GUILD_ID"
STOCKS_CHANNEL_ENV: Final = "DISCORD_STOCKS_CHANNEL_ID"

REQUIRED: Final[tuple[str, ...]] = (
    APPLICATION_ID_ENV,
    BOT_TOKEN_ENV,
    GUILD_ID_ENV,
    STOCKS_CHANNEL_ENV,
)

_SNOWFLAKE = re.compile(r"^\d{17,20}$")
"""Discord IDs are 64-bit snowflakes rendered as decimal. Seventeen to twenty
digits covers every ID the platform has issued and every one it will."""


class BotConfigurationError(RuntimeError):
    """Configuration is missing or malformed. **Never carries a value.**"""


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Everything the interactive bot needs. Values are secret; presence is not."""

    application_id: int
    guild_id: int
    stocks_channel_id: int
    token: SecretStr

    @property
    def configured(self) -> bool:
        return bool(self.token.get_secret_value())

    def describe(self) -> dict[str, Any]:
        """Presence-only view, safe for a health report or an artifact.

        Deliberately reports *whether* each value is set and nothing about what
        it is -- not even a prefix, because a snowflake prefix identifies the
        Discord account that created it.
        """
        return {
            "application_id_configured": bool(self.application_id),
            "guild_id_configured": bool(self.guild_id),
            "stocks_channel_configured": bool(self.stocks_channel_id),
            "bot_token_configured": self.configured,
            "bot_token_type": "SecretStr",
        }


def _dotenv(path: Path) -> dict[str, str]:
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


def presence(
    *, env: dict[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> dict[str, bool]:
    """Which required names are set. **Values are never read into the result.**"""
    source: dict[str, str] = {}
    if dotenv is not None:
        source.update(_dotenv(dotenv))
    source.update(env if env is not None else os.environ)
    return {name: bool(source.get(name, "").strip()) for name in REQUIRED}


def load(
    *, env: dict[str, str] | None = None, dotenv: Path | None = Path(".env")
) -> BotSettings:
    """Resolve bot configuration, or refuse with a reason that names no value.

    Raises:
        BotConfigurationError: when a required name is absent or an identifier is
            not a valid snowflake. The message names the *variable*, which is
            what an operator needs, and never its contents.
    """
    source: dict[str, str] = {}
    if dotenv is not None:
        source.update(_dotenv(dotenv))
    source.update(env if env is not None else os.environ)

    missing = [name for name in REQUIRED if not source.get(name, "").strip()]
    if missing:
        msg = f"missing Discord bot configuration: {', '.join(missing)}"
        raise BotConfigurationError(msg)

    ids: dict[str, int] = {}
    for name in (APPLICATION_ID_ENV, GUILD_ID_ENV, STOCKS_CHANNEL_ENV):
        raw = source[name].strip()
        if not _SNOWFLAKE.match(raw):
            msg = f"{name} is not a valid Discord snowflake ID (expected 17-20 digits)"
            raise BotConfigurationError(msg)
        ids[name] = int(raw)

    return BotSettings(
        application_id=ids[APPLICATION_ID_ENV],
        guild_id=ids[GUILD_ID_ENV],
        stocks_channel_id=ids[STOCKS_CHANNEL_ENV],
        token=SecretStr(source[BOT_TOKEN_ENV].strip()),
    )
