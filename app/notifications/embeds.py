"""Rendering a notification as a Discord embed.

Discord's plaintext is a wall on a phone. An embed gives a coloured spine, a
title and a grid of short fields, which is the difference between a message you
read on the way to work and one you scroll past.

Two rules shape everything here:

**Never fabricate a field.** A value that is absent is omitted, not filled with
a plausible-looking placeholder. tradabot has no price targets, no support and
resistance levels and no probability estimates, so an embed must never show
them -- a field labelled "Target" that came from nowhere is worse than no field,
because the reader cannot tell the difference.

**Plaintext is the fallback, not an afterthought.** ``content`` is always sent
alongside, so a client that cannot render embeds, a webhook that rejects them,
and the console backend all still show the whole message.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.events import Severity
from app.notifications.models import NotificationMessage

MAX_FIELDS: Final = 25
"""Discord's own limit. Exceeding it rejects the whole message."""

MAX_FIELD_NAME: Final = 256
MAX_FIELD_VALUE: Final = 1024
MAX_TITLE: Final = 256
MAX_DESCRIPTION: Final = 4096

COLOURS: Final[dict[Severity, int]] = {
    Severity.INFO: 0x3498DB,
    Severity.SIGNAL: 0x2ECC71,
    Severity.TRADE: 0x9B59B6,
    Severity.WARNING: 0xF39C12,
    Severity.CRITICAL: 0xE74C3C,
}
"""Severity as a colour, so the spine carries meaning before a word is read."""

INLINE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "Score",
        "Confidence",
        "Price",
        "Bid",
        "Ask",
        "Direction",
        "State",
        "Intraday",
        "Short term",
        "Medium term",
        "Long term",
        "Trend",
        "Momentum",
        "Volume",
        "Structure",
        "Volatility",
        "Liquidity",
        "Equity",
        "Net P/L",
        "Return",
        "Open positions",
    }
)
"""Fields short enough to sit three-per-row. Longer ones get their own line."""


def build_embed(message: NotificationMessage) -> dict[str, Any]:
    """One Discord embed for a message.

    Fields come from :attr:`NotificationMessage.fields`, which formatters
    populate only with values they actually have. Empty values are dropped here
    as a second line of defence -- a formatter that emits ``""`` for a missing
    price should not produce a blank field labelled "Price".
    """
    embed: dict[str, Any] = {
        "title": _clip(message.title, MAX_TITLE),
        "color": COLOURS.get(message.severity, COLOURS[Severity.INFO]),
        "timestamp": message.occurred_at.isoformat(),
    }

    if message.body:
        embed["description"] = _clip(message.body, MAX_DESCRIPTION)

    fields = [
        {
            "name": _clip(name, MAX_FIELD_NAME),
            "value": _clip(value, MAX_FIELD_VALUE),
            "inline": name in INLINE_FIELDS,
        }
        for name, value in message.fields.items()
        if value
    ][:MAX_FIELDS]
    if fields:
        embed["fields"] = fields

    footer = " · ".join(part for part in (message.event_type.value, message.routing_key) if part)
    if footer:
        embed["footer"] = {"text": footer}

    return embed


def build_payload(
    message: NotificationMessage, *, max_characters: int, use_embeds: bool = True
) -> dict[str, Any]:
    """The full webhook payload: embed plus plaintext fallback.

    ``content`` is always present. Discord shows both, and the duplication is
    deliberate -- it costs a few hundred characters and guarantees the message
    survives a client that renders no embeds at all.
    """
    payload: dict[str, Any] = {"content": message.rendered(max_characters)}
    if use_embeds:
        payload["embeds"] = [build_embed(message)]
    return payload


def _clip(value: str, limit: int) -> str:
    """Truncate to Discord's limit, marking that it happened.

    Silent truncation would drop the reason codes at the end of a signal message
    without any indication that something was cut.
    """
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
