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

**One payload, one visual representation.** When an embed is sent, ``content``
is left empty. Sending both put the same report on screen twice -- once as a
plain paragraph and again inside the coloured card -- which doubled the length
of every alert and made a phone screen show one message where it should show
three. The plaintext rendering is still produced when embeds are switched off,
so nothing is lost for a client that cannot render them; it is simply no longer
sent *as well*.
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
        # A formatter-supplied semantic colour wins: it knows whether a state is
        # unusual, uncertain or genuinely bad, which severity cannot express.
        "color": (
            message.colour
            if message.colour is not None
            else COLOURS.get(message.severity, COLOURS[Severity.INFO])
        ),
    }
    if message.show_timestamp:
        embed["timestamp"] = message.occurred_at.isoformat()

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

    footer = message.footer or " · ".join(
        part for part in (message.event_type.value, message.routing_key) if part
    )
    if footer:
        embed["footer"] = {"text": footer}

    return embed


def build_payload(
    message: NotificationMessage, *, max_characters: int, use_embeds: bool = True
) -> dict[str, Any]:
    """The webhook payload: an embed, **or** plaintext, never both.

    Discord renders ``content`` above the embed, so populating both shows the
    same report twice. The embed carries everything -- title, body, fields,
    timestamp -- so ``content`` is empty whenever an embed is present.

    With ``use_embeds=False`` the full rendered text goes in ``content``, which
    is what the console backend and any embed-less destination receive.
    """
    if not use_embeds:
        return {"content": message.rendered(max_characters)}
    # Empty rather than absent: Discord accepts an empty content field beside an
    # embed, and being explicit documents that the omission is deliberate.
    return {"content": "", "embeds": [build_embed(message)]}


def _clip(value: str, limit: int) -> str:
    """Truncate to Discord's limit, marking that it happened.

    Silent truncation would drop the reason codes at the end of a signal message
    without any indication that something was cut.
    """
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
