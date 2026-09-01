"""The interactive bot process: one command, deferred, bounded, read-only.

Why a gateway connection at all
-------------------------------
Slash commands arrive as *interactions*, not as messages. The bot therefore
needs no ability to read what anyone types: it connects with an empty intent
set, receives only the interactions Discord routes to it, and is blind to the
rest of the server. That is a smaller permission surface than the passive
webhook publisher already uses, and it is why no privileged intent is required.

Defer first, analyse second
---------------------------
Discord closes the interaction window in three seconds. A full company analysis
reads a fact store and a year of prices and does not reliably finish in three
seconds, so the handler defers immediately and edits the original response when
the answer is ready. The user sees one thinking indicator that becomes one
report -- never an acknowledgement followed by a second standalone message.

Bounded work
------------
This runs on a laptop that is also running the scheduler. A semaphore caps
concurrent analyses; beyond it a user is told the bot is busy rather than the
machine being buried under work nobody is waiting for any more. There is no
result cache: a stale valuation served silently is worse than a slow one.

Read-only, structurally
-----------------------
Nothing in this package imports a trading client, and a test asserts it. The
only broker contact is through the existing read-only snapshot reader, which has
no mutating method to call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import discord
from discord import app_commands

from app.core.logging import get_logger
from app.discord_bot.analysis import StockAnalyst, StockCheck
from app.discord_bot.config import BotSettings
from app.discord_bot.render import check_message
from app.discord_bot.timing import Timings
from app.notifications.embeds import build_embed

logger = get_logger(__name__)

MAX_CONCURRENT_CHECKS = 3
"""Simultaneous analyses. Three is enough that a person is never queued behind
themselves, and small enough that a burst cannot saturate the machine the
scheduler shares."""

BUSY_MESSAGE = "Tradabot is analysing several requests already. Please try again in a moment."
WRONG_CHANNEL_MESSAGE = "This command is available in #stocks."


class BotHealth:
    """What the bot knows about itself. Counts and timestamps, never identity."""

    def __init__(self) -> None:
        self.connected_at: datetime | None = None
        self.last_interaction: datetime | None = None
        self.commands_registered: int = 0
        self.checks_handled: int = 0
        self.errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        state = "CONNECTED" if self.connected_at else "DISCONNECTED"
        if self.connected_at and self.errors and not self.checks_handled:
            state = "DEGRADED"
        return {
            "state": state,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "last_interaction": (
                self.last_interaction.isoformat() if self.last_interaction else None
            ),
            "commands_registered": self.commands_registered,
            "checks_handled": self.checks_handled,
            "errors": self.errors,
        }


def embed_for(check: StockCheck) -> discord.Embed:
    """The rendered check as a Discord embed.

    Built from the same :class:`~app.notifications.models.NotificationMessage`
    the webhook publisher uses, through the same builder, so a ``/check`` looks
    like everything else Tradabot sends and inherits the no-duplication rule.
    """
    return discord.Embed.from_dict(build_embed(check_message(check)))


class TradabotClient(discord.Client):
    """A Discord client that answers exactly one command.

    Args:
        settings: resolved bot configuration.
        analyst_factory: builds the analyst. Called once, lazily, so a slow
            data load does not delay the gateway connection.
    """

    def __init__(
        self,
        settings: BotSettings,
        *,
        analyst_factory: Callable[[], StockAnalyst],
    ) -> None:
        # No intents at all. Application commands arrive regardless, and a bot
        # that cannot read messages cannot leak what it never received.
        super().__init__(intents=discord.Intents.none())
        self._settings = settings
        self._factory = analyst_factory
        self._analyst: StockAnalyst | None = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
        self.tree = app_commands.CommandTree(self)
        self.health = BotHealth()
        self._register()

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self) -> None:
        """Publish the command to one guild.

        Guild-scoped rather than global: guild commands appear immediately,
        while global ones propagate on Discord's own schedule, which makes a
        registration mistake take an hour to observe.
        """
        guild = discord.Object(id=self._settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        self.health.commands_registered = len(synced)
        logger.info("slash commands registered", count=len(synced))

    async def on_ready(self) -> None:
        self.health.connected_at = datetime.now(UTC)
        logger.info("discord gateway connected")

    # -------------------------------------------------------------- command
    def _register(self) -> None:
        @self.tree.command(name="check", description="Analyse a stock with Tradabot")
        @app_commands.describe(symbol="Ticker symbol, for example NVDA")
        async def check(interaction: discord.Interaction, symbol: str) -> None:
            await self._handle_check(interaction, symbol)

    async def _handle_check(self, interaction: discord.Interaction, symbol: str) -> None:
        """One invocation, one visible answer. **Never raises into discord.py.**"""
        self.health.last_interaction = datetime.now(UTC)
        if interaction.channel_id != self._settings.stocks_channel_id:
            # Ephemeral, and it names the channel rather than its numeric ID.
            await interaction.response.send_message(WRONG_CHANNEL_MESSAGE, ephemeral=True)
            return

        if self._slots.locked():
            await interaction.response.send_message(BUSY_MESSAGE, ephemeral=True)
            return

        # Defer before doing anything slow: the interaction window is three
        # seconds and a real analysis is not reliably faster than that.
        await interaction.response.defer(thinking=True)
        clock = Timings()
        cold = self._analyst is None
        async with self._slots:
            try:
                check_result = await self._analyse(symbol, clock)
                with clock.stage("render"):
                    embed = embed_for(check_result)
                with clock.stage("discord_reply"):
                    await interaction.edit_original_response(embed=embed)
                self.health.checks_handled += 1
                clock.log(symbol=symbol, cold=cold)
            except Exception as exc:
                self.health.errors += 1
                logger.warning("check failed", symbol=symbol[:12], reason=type(exc).__name__)
                await self._say_failed(interaction)

    async def _analyse(self, symbol: str, clock: Timings) -> StockCheck:
        """Run the analysis off the event loop, so the gateway keeps beating."""
        analyst = await self._get_analyst(clock)
        return await asyncio.to_thread(analyst.check, symbol, timings=clock)

    async def _get_analyst(self, clock: Timings) -> StockAnalyst:
        """Build the analyst once. The first caller pays for it.

        Timed separately because that first build -- price history and the fact
        store -- is the obvious suspect for a slow first request, and mixing it
        into the analysis time would hide which of the two is expensive.
        """
        async with self._lock:
            if self._analyst is None:
                with clock.stage("initialise"):
                    self._analyst = await asyncio.to_thread(self._factory)
            return self._analyst

    @staticmethod
    async def _say_failed(interaction: discord.Interaction) -> None:
        """Report a failure as a state. A stack trace tells a user nothing."""
        try:
            await interaction.edit_original_response(
                content=(
                    "Tradabot could not complete that analysis. The failure has been "
                    "logged; nothing was changed."
                )
            )
        except Exception:
            logger.warning("could not deliver failure notice")


async def run(settings: BotSettings, *, analyst_factory: Callable[[], StockAnalyst]) -> None:
    """Connect and serve until cancelled.

    ``discord.py`` owns reconnection, session resumption and rate limiting --
    the parts a hand-rolled gateway client gets wrong at three in the morning.
    """
    client = TradabotClient(settings, analyst_factory=analyst_factory)
    await client.start(settings.token.get_secret_value())
