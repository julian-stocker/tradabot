"""Turning events into readable messages.

Pure functions: event in, :class:`NotificationMessage` out. No I/O, no
transport, no database. That makes every formatting rule testable without a
network and keeps message shape out of the delivery path.

Two rules run through all of it.

**Never fabricate a metric.** A formatter renders what the event carries and
silently omits what it does not. A message showing "Confidence: —" is honest; one
showing a plausible number nobody computed is not, and a monitoring channel is
precisely where an invented figure would be believed.

**Never report gross as net.** Trade messages show fees, spread and slippage
alongside the gross figure, because the gap between the two is the entire point
of tradabot's cost modelling. A message reporting only gross P&L would flatter
every result on the channel a human actually reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.events import Event, EventType
from app.notifications.models import NotificationMessage
from app.notifications.opportunity import opportunity_fields
from app.notifications.trends import DISCLAIMER

EMOJI: dict[EventType, str] = {
    EventType.MARKET_SIGNAL_QUALIFIED: "📈",
    EventType.MARKET_SIGNAL_STRENGTHENED: "🚀",
    EventType.MARKET_SIGNAL_INVALIDATED: "📉",
    EventType.MARKET_OVERVIEW: "🔎",
    EventType.MARKET_TRENDS: "📈",
    EventType.OPERATIONAL_STATUS: "🖥️",
    EventType.PAPER_TRADE_OPENED: "🟢",
    EventType.PAPER_TRADE_CLOSED: "🔵",
    EventType.PAPER_TRADE_SKIPPED: "⚪",
    EventType.PORTFOLIO_PERFORMANCE_SUMMARY: "📊",
    EventType.DAILY_SIMULATION_SUMMARY: "📊",
    EventType.MARKET_DATA_SYNC_FAILED: "⚠️",
    EventType.STALE_MARKET_DATA_DETECTED: "🚨",
    EventType.PROVIDER_DISCONNECTED: "🚨",
    EventType.PROVIDER_RECOVERED: "✅",
    EventType.CRITICAL_SYSTEM_ERROR: "🚨",
    EventType.TRADABOT_STARTED: "▶️",
    EventType.TRADABOT_STOPPED: "⏹️",
    EventType.NOTIFICATION_TEST: "🧪",
}

MAX_REASONS = 5
MAX_RISKS = 4
HIGH_CONFIDENCE = 0.7
MEDIUM_CONFIDENCE = 0.4


def format_event(event: Event) -> NotificationMessage:
    """Render any event. Dispatches on type, with a usable generic fallback.

    The fallback matters: an event type added later still produces a legible
    message rather than an exception in the delivery path.
    """
    payload = event.redacted_payload()
    formatter = _FORMATTERS.get(event.type, _format_generic)
    title, body = formatter(event, payload)
    return NotificationMessage(
        category=event.category,
        severity=event.severity,
        title=title,
        body=body,
        event_type=event.type,
        occurred_at=event.occurred_at,
        key=event.key,
        routing_key=event.routing_key,
        fields=_fields_for(event, payload),
    )


def _fields_for(event: Event, payload: Mapping[str, Any]) -> dict[str, str]:
    """Embed fields for an event.

    Market opportunities get a curated, ordered, human-named set -- they are the
    messages read on a phone while deciding something. Everything else falls back
    to the scalar payload, which stays legible without needing a bespoke layout
    per event type.
    """
    if event.type in _OPPORTUNITY_EVENTS:
        return opportunity_fields(payload)
    if event.type is EventType.OPERATIONAL_STATUS:
        # The dashboard grid *is* the message. It arrives as a dict and would
        # otherwise be dropped by the scalar filter below, leaving an embed with
        # a title and no content.
        grid = payload.get("fields")
        return {str(k): str(v) for k, v in grid.items()} if isinstance(grid, dict) else {}
    return {k: _scalar(v) for k, v in payload.items() if not isinstance(v, list | dict)}


_OPPORTUNITY_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.MARKET_SIGNAL_QUALIFIED,
        EventType.MARKET_SIGNAL_STRENGTHENED,
        EventType.MARKET_SIGNAL_INVALIDATED,
    }
)


# ---------------------------------------------------------------------------
# Signals (Part H)
# ---------------------------------------------------------------------------
def _format_signal(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """A signal message: score, components, reasons, risks, quote.

    Every section is conditional. The signal engine does not always populate
    components, and a historical evaluation has no live quote -- so those
    sections vanish rather than render as empty scaffolding.
    """
    emoji = EMOJI.get(event.type, "📈")
    symbol = payload.get("symbol", "?")
    classification = str(payload.get("classification", "")).replace("_", " ").upper()
    headline = {
        EventType.MARKET_SIGNAL_QUALIFIED: classification or "SIGNAL",
        EventType.MARKET_SIGNAL_STRENGTHENED: f"{classification} — STRENGTHENED".strip(" —"),
        EventType.MARKET_SIGNAL_INVALIDATED: "SIGNAL INVALIDATED",
    }.get(event.type, classification or "SIGNAL")

    lines: list[str] = [f"**{symbol}**", ""]

    score = payload.get("score")
    if score is not None:
        lines.append(f"Score: {_number(score)} / 100")
    previous = payload.get("previous_score")
    if previous is not None:
        lines.append(f"Previous: {_number(previous)}")
    confidence = payload.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: {_confidence_label(confidence)}")
    for label, key in (("Horizon", "horizon"), ("Timeframe", "timeframe")):
        if payload.get(key):
            lines.append(f"{label}: {payload[key]}")

    components = payload.get("components")
    if isinstance(components, dict) and components:
        lines.extend(["", "```"])
        width = max(len(str(name)) for name in components)
        lines.extend(
            f"{str(name).ljust(width)}  {_number(value)}" for name, value in components.items()
        )
        lines.append("```")

    net_edge = payload.get("net_edge_bps")
    if net_edge is not None:
        # The gate that actually decides whether a signal is worth acting on.
        lines.extend(["", f"Net edge: {_number(net_edge)} bps after costs"])

    lines.extend(_bullets("Reasons", payload.get("reasons"), MAX_REASONS))
    lines.extend(_bullets("Risks", payload.get("risks"), MAX_RISKS))

    bid, ask = payload.get("bid"), payload.get("ask")
    if bid is not None and ask is not None:
        lines.extend(["", "Current quote:", f"  Bid {_number(bid)}   Ask {_number(ask)}"])
        if payload.get("spread_bps") is not None:
            lines[-1] += f"   Spread {_number(payload['spread_bps'])} bps"

    return f"{emoji} {headline}", "\n".join(lines)


# ---------------------------------------------------------------------------
# Paper trading (Part I)
# ---------------------------------------------------------------------------
def _format_trade_opened(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """One grouped message per signal, not one per profile.

    Nine portfolios evaluating one signal is nine decisions and **one** thing
    that happened. Sending nine messages would make the channel unreadable on the
    exact days it matters most.
    """
    symbol = payload.get("symbol", "?")
    lines: list[str] = []

    score = payload.get("score")
    if score is not None:
        lines.append(f"Signal: {_number(score)} / 100")

    decisions = payload.get("decisions")
    if isinstance(decisions, list) and decisions:
        lines.extend(["", "```", _decision_table(decisions), "```"])

    opened = payload.get("positions_opened")
    rejected = payload.get("entries_rejected")
    if opened is not None:
        summary = f"Opened: {opened}"
        if rejected:
            summary += f"   Rejected: {rejected}"
        lines.append(summary)

    return f"{EMOJI[EventType.PAPER_TRADE_OPENED]} PAPER TRADE DECISION — {symbol}", "\n".join(
        lines
    )


def _decision_table(decisions: Sequence[Any]) -> str:
    """A profile x outcome grid.

    Rendered as a table because the interesting information is the *pattern*:
    the small portfolios declining a trade the large ones take is the cost model
    working, and a list of nine lines hides it.
    """
    rows = [d for d in decisions if isinstance(d, dict)]
    if not rows:
        return "(no decisions)"
    name_width = max(len(str(r.get("profile", "?"))) for r in rows)
    return "\n".join(
        f"{str(r.get('profile', '?')).ljust(name_width)}  {str(r.get('decision', '?')).upper()}"
        + (f"  ({r['reason']})" if r.get("reason") else "")
        for r in rows
    )


def _format_trade_closed(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """A closed round trip, with costs itemised beside the gross figure."""
    symbol = payload.get("symbol", "?")
    profile = payload.get("profile", "")
    title = f"{EMOJI[EventType.PAPER_TRADE_CLOSED]} PAPER TRADE CLOSED — {symbol}"

    lines: list[str] = []
    if profile:
        lines.append(f"Profile: {profile}")

    entry, exit_price = payload.get("entry_price"), payload.get("exit_price")
    if entry is not None and exit_price is not None:
        lines.append(f"Entry {_number(entry)} → Exit {_number(exit_price)}")
    for label, key in (
        ("Quantity", "quantity"),
        ("Held", "holding"),
        ("Exit reason", "exit_reason"),
    ):
        if payload.get(key) is not None:
            lines.append(f"{label}: {payload[key]}")

    # Gross first, then every cost, then net. Presenting net alone would hide the
    # thing the cost model exists to reveal.
    cost_lines = [
        (label, payload.get(key))
        for label, key in (
            ("Gross P/L", "gross_pnl"),
            ("Fees", "fees"),
            ("Spread", "spread_cost"),
            ("Slippage", "slippage_cost"),
        )
    ]
    shown = [(label, value) for label, value in cost_lines if value is not None]
    if shown:
        lines.extend(["", "```"])
        width = max(len(label) for label, _ in shown)
        lines.extend(f"{label.ljust(width)}  {_number(value)}" for label, value in shown)
        lines.append("```")

    net = payload.get("net_pnl")
    if net is not None:
        line = f"**Net P/L: {_signed(net)}**"
        if payload.get("net_return") is not None:
            line += f"  ({_signed_pct(payload['net_return'])})"
        lines.append(line)

    return title, "\n".join(lines)


def _format_trade_skipped(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    symbol = payload.get("symbol", "?")
    lines = [f"Profile: {payload['profile']}"] if payload.get("profile") else []
    if payload.get("reason"):
        lines.append(f"Reason: {payload['reason']}")
    return f"{EMOJI[EventType.PAPER_TRADE_SKIPPED]} SKIPPED — {symbol}", "\n".join(lines)


# ---------------------------------------------------------------------------
# Performance (Part J)
# ---------------------------------------------------------------------------
def _format_summary(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """A daily report across every portfolio.

    Metrics with too few observations behind them are **omitted**, not shown as
    zero or null. A win rate over two trades is noise wearing a percentage sign,
    and a monitoring channel is where such a number gets quoted back as fact.
    """
    title = f"{EMOJI[EventType.DAILY_SIMULATION_SUMMARY]} DAILY TRADABOT REPORT"
    lines: list[str] = []

    if payload.get("session_date"):
        lines.extend([f"Session: {payload['session_date']}", ""])

    activity = [
        (label, payload.get(key))
        for label, key in (
            ("Symbols scanned", "symbols_scanned"),
            ("Signals evaluated", "signals_evaluated"),
            ("Qualified signals", "signals_qualified"),
            ("Currently qualified", "currently_qualified"),
            ("Paper entries", "entries"),
            ("Paper exits", "exits"),
        )
    ]
    shown = [(label, value) for label, value in activity if value is not None]
    lines.extend(f"{label}: {value}" for label, value in shown)

    portfolios = payload.get("portfolios")
    if isinstance(portfolios, list) and portfolios:
        lines.extend(["", "Portfolio results:", "```"])
        lines.extend(_portfolio_line(p) for p in portfolios if isinstance(p, dict))
        lines.append("```")

    if not lines:
        lines.append("No activity recorded.")
    return title, "\n".join(lines)


def _portfolio_line(portfolio: Mapping[str, Any]) -> str:
    """One portfolio per line, omitting whatever is unavailable."""
    parts = [str(portfolio.get("profile", "?"))]
    if portfolio.get("equity") is not None:
        parts.append(f"equity {_number(portfolio['equity'])}")
    if portfolio.get("net_pnl") is not None:
        parts.append(f"net {_signed(portfolio['net_pnl'])}")
    if portfolio.get("return_pct") is not None:
        parts.append(f"({_signed_pct(portfolio['return_pct'])})")
    if portfolio.get("open_positions") is not None:
        parts.append(f"open {portfolio['open_positions']}")
    # Drawdown needs an equity curve behind it; a fresh portfolio has none.
    if portfolio.get("max_drawdown") is not None:
        parts.append(f"dd {_pct(portfolio['max_drawdown'])}")
    return "  ".join(parts)


def _format_overview(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """Top current opportunities (Part K).

    The formatter exists ahead of the scanner that will feed it. It renders
    whatever candidates it is given and says so plainly when given none -- it
    never manufactures a list.
    """
    title = f"{EMOJI[EventType.MARKET_OVERVIEW]} MARKET OVERVIEW"
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return title, "No qualified opportunities."

    lines: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        parts = [f"{index}. **{candidate.get('symbol', '?')}**"]
        if candidate.get("score") is not None:
            parts.append(f"{_number(candidate['score'])}/100")
        for key in ("direction", "horizon"):
            if candidate.get(key):
                parts.append(str(candidate[key]))
        if candidate.get("confidence") is not None:
            parts.append(_confidence_label(candidate["confidence"]))
        lines.append("  ".join(parts))
        reasons = candidate.get("reasons")
        if isinstance(reasons, list) and reasons:
            lines.append(f"   • {reasons[0]}")
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# System (Part M)
# ---------------------------------------------------------------------------
def _format_stale(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [f"Provider: {payload.get('provider', '?')}"]
    if payload.get("symbol"):
        lines.append(f"Symbol: {payload['symbol']}")
    if payload.get("age_seconds") is not None:
        lines.append(f"Age: {_duration(payload['age_seconds'])}")
    if payload.get("limit_seconds") is not None:
        lines.append(f"Limit: {_duration(payload['limit_seconds'])}")
    return f"{EMOJI[EventType.STALE_MARKET_DATA_DETECTED]} MARKET DATA STALE", "\n".join(lines)


def _format_recovered(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [f"Provider: {payload.get('provider', '?')}"]
    if payload.get("downtime_seconds") is not None:
        lines.append(f"Downtime: {_duration(payload['downtime_seconds'])}")
    return f"{EMOJI[EventType.PROVIDER_RECOVERED]} MARKET DATA RECOVERED", "\n".join(lines)


def _format_sync_failed(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [f"Provider: {payload.get('provider', '?')}"]
    if payload.get("symbol"):
        lines.append(f"Symbol: {payload['symbol']}")
    if payload.get("error"):
        # Already redacted by the event constructor; truncated so a verbose
        # provider error cannot crowd out the fact that a sync failed.
        lines.append(f"Error: {_clip(str(payload['error']), 300)}")
    return f"{EMOJI[EventType.MARKET_DATA_SYNC_FAILED]} MARKET DATA SYNC FAILED", "\n".join(lines)


def _format_disconnected(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [f"Provider: {payload.get('provider', '?')}"]
    if payload.get("error"):
        lines.append(f"Error: {_clip(str(payload['error']), 300)}")
    return f"{EMOJI[EventType.PROVIDER_DISCONNECTED]} PROVIDER DISCONNECTED", "\n".join(lines)


def _format_critical(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [f"Component: {payload.get('component', '?')}"]
    if payload.get("error"):
        # No stack traces on a chat channel: they leak paths and connection
        # strings, and the one line that matters is already here.
        lines.append(f"Error: {_clip(str(payload['error']), 400)}")
    return f"{EMOJI[EventType.CRITICAL_SYSTEM_ERROR]} CRITICAL SYSTEM ERROR", "\n".join(lines)


def _format_lifecycle(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    started = event.type is EventType.TRADABOT_STARTED
    lines = [f"Environment: {payload.get('environment', '?')}"]
    if payload.get("provider"):
        lines.append(f"Provider: {payload['provider']}")
    return f"{EMOJI[event.type]} TRADABOT {'STARTED' if started else 'STOPPED'}", "\n".join(lines)


def _format_test(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    lines = [
        f"Channel: {payload.get('channel', '?')}",
        f"Environment: {payload.get('environment', '?')}",
        "",
        "If you can read this, delivery works. No credentials are included.",
    ]
    return f"{EMOJI[EventType.NOTIFICATION_TEST]} TRADABOT TEST", "\n".join(lines)


def _format_trends(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """Ranked market activity. **Descriptive only.**

    Renders what it is given and nothing more -- no interpretation, no implied
    action, no score. The disclaimer is appended here rather than by the caller
    so it cannot be forgotten by a future emitter, and
    :func:`~app.notifications.trends.assert_no_recommendation_language` checks
    the finished text at the boundary.
    """
    title = str(payload.get("title") or f"{EMOJI[EventType.MARKET_TRENDS]} MARKET ACTIVITY")
    movers = payload.get("movers")
    if not isinstance(movers, list) or not movers:
        # Should be unreachable: the caller does not publish an empty list,
        # because "nothing notable happened" is not worth a message.
        return title, DISCLAIMER

    lines: list[str] = []
    for index, mover in enumerate(movers, start=1):
        if not isinstance(mover, dict):
            continue
        line = f"{index}. **{mover.get('symbol', '?')}**  {mover.get('headline', '')}".rstrip()
        if mover.get("detail"):
            line += f"   ({mover['detail']})"
        lines.append(line)

    lines.extend(("", str(payload.get("disclaimer") or DISCLAIMER)))
    return title, "\n".join(lines)


def _format_status(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """The #status dashboard body.

    Deliberately thin: :func:`app.notifications.dashboard.build_fields` already
    decided what to show and how to word it, and the embed renders those fields
    directly. Reformatting them here would give two answers to "what does the
    dashboard say", and the plaintext fallback would drift from the embed.
    """
    title = str(payload.get("title") or f"{EMOJI[EventType.OPERATIONAL_STATUS]} TRADABOT STATUS")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        return title, "No status available."
    return title, "\n".join(f"{name}: {value}" for name, value in fields.items())


def _format_generic(event: Event, payload: Mapping[str, Any]) -> tuple[str, str]:
    """Fallback for a type with no dedicated formatter."""
    emoji = EMOJI.get(event.type, "📄")
    body = "\n".join(f"{key}: {_scalar(value)}" for key, value in payload.items())
    return f"{emoji} {event.type.value}", body


_FORMATTERS: dict[EventType, Any] = {
    EventType.MARKET_SIGNAL_QUALIFIED: _format_signal,
    EventType.MARKET_SIGNAL_STRENGTHENED: _format_signal,
    EventType.MARKET_SIGNAL_INVALIDATED: _format_signal,
    EventType.MARKET_OVERVIEW: _format_overview,
    EventType.MARKET_TRENDS: _format_trends,
    EventType.OPERATIONAL_STATUS: _format_status,
    EventType.PAPER_TRADE_OPENED: _format_trade_opened,
    EventType.PAPER_TRADE_CLOSED: _format_trade_closed,
    EventType.PAPER_TRADE_SKIPPED: _format_trade_skipped,
    EventType.DAILY_SIMULATION_SUMMARY: _format_summary,
    EventType.PORTFOLIO_PERFORMANCE_SUMMARY: _format_summary,
    EventType.STALE_MARKET_DATA_DETECTED: _format_stale,
    EventType.PROVIDER_RECOVERED: _format_recovered,
    EventType.MARKET_DATA_SYNC_FAILED: _format_sync_failed,
    EventType.PROVIDER_DISCONNECTED: _format_disconnected,
    EventType.CRITICAL_SYSTEM_ERROR: _format_critical,
    EventType.TRADABOT_STARTED: _format_lifecycle,
    EventType.TRADABOT_STOPPED: _format_lifecycle,
    EventType.NOTIFICATION_TEST: _format_test,
}


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def _bullets(heading: str, items: Any, limit: int) -> list[str]:
    """A bulleted section, or nothing at all if there is nothing to list."""
    if not isinstance(items, list) or not items:
        return []
    shown = [f"• {_clip(str(item), 120)}" for item in items[:limit]]
    if len(items) > limit:
        shown.append(f"• … {len(items) - limit} more")
    return ["", f"{heading}:", *shown]


def _number(value: Any) -> str:
    """Two decimals for anything numeric, the raw string otherwise."""
    if isinstance(value, Decimal | float | int) and not isinstance(value, bool):
        return f"{float(value):.2f}"
    return str(value)


def _signed(value: Any) -> str:
    if isinstance(value, Decimal | float | int) and not isinstance(value, bool):
        return f"{float(value):+.2f}"
    return str(value)


def _pct(value: Any) -> str:
    if isinstance(value, Decimal | float | int) and not isinstance(value, bool):
        return f"{float(value) * 100:.2f}%"
    return str(value)


def _signed_pct(value: Any) -> str:
    if isinstance(value, Decimal | float | int) and not isinstance(value, bool):
        return f"{float(value):+.2f}%"
    return str(value)


def _confidence_label(value: Any) -> str:
    """A word rather than a number, because the number is not a probability.

    ``confidence`` measures agreement between components, not the chance of being
    right. Printing "0.72" invites reading it as a 72% likelihood, which it is
    emphatically not.
    """
    if not isinstance(value, Decimal | float | int) or isinstance(value, bool):
        return str(value)
    number = float(value)
    if number >= HIGH_CONFIDENCE:
        return "HIGH"
    return "MEDIUM" if number >= MEDIUM_CONFIDENCE else "LOW"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, Decimal | float | int) or isinstance(seconds, bool):
        return str(seconds)
    total = int(float(seconds))
    if total < 60:  # noqa: PLR2004 -- a minute
        return f"{total}s"
    if total < 3_600:  # noqa: PLR2004 -- an hour
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def _scalar(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _clip(str(value), 200)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
