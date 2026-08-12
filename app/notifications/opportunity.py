"""Curated embed fields for a market opportunity.

The generic formatter turns every scalar payload key into a field, which gives
you `net_edge_bps` and `market_data_timestamp` in raw form and in whatever order
the dict happened to be built. Readable on a laptop, unreadable on a phone.

This picks the fields a human actually decides from, names them in English, and
orders them: identity, verdict, horizons, components, freshness. Anything absent
is **omitted** -- there is no placeholder, no "n/a", and no invented value. A
field labelled "Price" that came from nowhere is worse than no Price at all,
because the reader cannot tell which they are looking at.

The plaintext body still comes from :func:`_format_signal`, so there is one
formatting implementation and this only decides what the embed grid shows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

HORIZON_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("Intraday", "intraday"),
    ("Short term", "short_term"),
    ("Medium term", "medium_term"),
    ("Long term", "long_term"),
)
"""The four horizons, always in this order.

Rendered even when the value is ``NOT_AVAILABLE``: that is a real answer and
hiding it would let a reader assume the horizon was simply neutral.
"""

COMPONENT_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("Trend", "trend"),
    ("Momentum", "momentum"),
    ("Volume", "volume"),
    ("Structure", "structure"),
    ("Volatility", "volatility"),
    ("Liquidity", "liquidity"),
)

MAX_LIST_ITEMS: Final = 4
"""Reasons and risks shown in the embed. The full list stays in the plaintext."""


def opportunity_fields(payload: Mapping[str, Any]) -> dict[str, str]:
    """Embed fields for a market-signal payload, in reading order.

    Only keys the scanner genuinely populates. Note what is *not* here and
    cannot be: support, resistance, price targets, entry zones, expected price
    and any long-term forecast. tradabot computes none of them, so no field can
    claim them.
    """
    fields: dict[str, str] = {}

    _put(fields, "Company", payload.get("company_name"))
    _put(fields, "State", payload.get("lifecycle_state"))
    _put(fields, "Direction", _direction(payload.get("direction")))

    _put(fields, "Score", _score(payload.get("score")))
    _put(fields, "Confidence", _percent(payload.get("confidence")))
    _put(fields, "Price", _price(payload.get("price")))
    _put(fields, "Bid", _price(payload.get("bid")))
    _put(fields, "Ask", _price(payload.get("ask")))

    for label, key in HORIZON_FIELDS:
        _put(fields, label, _readable(payload.get(key)))

    for label, key in COMPONENT_FIELDS:
        _put(fields, label, _readable(payload.get(key)))

    _put(fields, "Reasons", _bullets(payload.get("reasons")))
    _put(fields, "Risks", _bullets(payload.get("risks")))

    _put(fields, "Data", payload.get("market_data_timestamp"))
    _put(fields, "Freshness", payload.get("freshness"))
    _put(fields, "Source", _source(payload))

    return fields


def _put(fields: dict[str, str], label: str, value: Any) -> None:
    """Add a field, or nothing at all.

    The single rule this module exists to enforce: absent stays absent.
    """
    if value is None:
        return
    text = str(value).strip()
    if text:
        fields[label] = text


def _direction(value: Any) -> str | None:
    """Direction as a word, never a bare integer.

    Deliberately not "BUY" or "SELL": this is a setup, not an instruction.
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}.get(number)


def _score(value: Any) -> str | None:
    return None if value is None else f"{float(value):.1f} / 100"


def _percent(value: Any) -> str | None:
    """Confidence as a percentage.

    It is stored as a 0-1 fraction, which is easy to misread as a 0-100 score --
    a mistake made once already in this codebase's own analysis.
    """
    if value is None:
        return None
    number = float(value)
    return f"{number * 100:.0f}%" if number <= 1 else f"{number:.0f}%"


def _price(value: Any) -> str | None:
    return None if value is None else f"{float(value):,.2f}"


def _readable(value: Any) -> str | None:
    """`STRONG_UP` -> `STRONG UP`. Leaves NOT_AVAILABLE legible as-is."""
    if value is None:
        return None
    return str(value).replace("_", " ")


def _bullets(value: Any) -> str | None:
    if not isinstance(value, list | tuple) or not value:
        return None
    return "\n".join(f"• {item}" for item in list(value)[:MAX_LIST_ITEMS])


def _source(payload: Mapping[str, Any]) -> str | None:
    """Provider and feed together: `alpaca / iex`."""
    provider = payload.get("provider")
    feed = payload.get("feed")
    parts = [str(part) for part in (provider, feed) if part]
    return " / ".join(parts) if parts else None
