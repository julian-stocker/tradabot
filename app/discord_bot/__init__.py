"""Interactive Discord frontend.

One command, ``/check SYMBOL``, answered from the existing read-only production
layers and rendered with the Phase 12.39 presentation system.

This is a **second** transport beside the webhook publisher, not a replacement.
The publisher pushes what monitoring decided was material; the bot answers what
a person asked. They share the presentation vocabulary and nothing else, so a
bot outage cannot silence the weekly newsletter and a publisher failure cannot
stop the bot answering.

Observational only. Nothing here can place, cancel or close anything, and a
structural test asserts the package imports no execution client.
"""

from app.discord_bot.analysis import StockAnalyst, StockCheck
from app.discord_bot.config import (
    BotConfigurationError,
    BotSettings,
    load,
    presence,
)
from app.discord_bot.render import check_message
from app.discord_bot.resolve import Availability, Resolution, Resolved, normalise, resolve

__all__ = [
    "Availability",
    "BotConfigurationError",
    "BotSettings",
    "Resolution",
    "Resolved",
    "StockAnalyst",
    "StockCheck",
    "check_message",
    "load",
    "normalise",
    "presence",
    "resolve",
]
