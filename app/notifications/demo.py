"""A manually-invoked walk through every Discord destination.

``send_test`` proves a webhook is reachable. This proves the *lifecycle* renders:
what a qualified opportunity actually looks like on a phone, what a paper close
looks like next to it, whether the embed fields fit.

Everything it sends is synthetic and labelled. It writes **nothing**: no
evaluation, no tracked signal, no position, no trade, no research row. The
events are constructed in memory and handed to the notification service, which
is the same path production uses -- so what you see is what production would
send, without production having happened.

Never wired to a schedule, never invoked by a test. It exists to be run by a
human who wants to look at the result.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from app.core.config import Settings
from app.core.events import Event, EventType
from app.core.time import utc_now
from app.simulation.portfolios import PORTFOLIO_KEYS

DEMO_MARKER: Final = "🧪 TEST"
"""Prefixed to every demo message.

Unmissable on purpose. A synthetic STRONG opportunity that reads like a real one
is a message somebody acts on.
"""

DEMO_SYMBOL: Final = "DEMOX"
"""Not a real ticker.

Using NVDA would put a fabricated NVDA opportunity in the channel next to real
ones, and nobody scrolling back a week would be able to tell them apart.
"""


def lifecycle_events(settings: Settings) -> list[tuple[str, Event]]:
    """The full sequence, in the order a real opportunity would produce it.

    Returns ``(destination, event)`` pairs so a caller can report where each one
    went without inspecting the routing itself.
    """
    now = utc_now()
    del settings

    horizons = {
        "Intraday": "BULLISH",
        "Short term": "BULLISH",
        "Medium term": "NEUTRAL",
        "Long term": "NOT AVAILABLE",
    }
    base = {
        "symbol": DEMO_SYMBOL,
        "company": "Demo Instrument (synthetic)",
        "demo": True,
        **horizons,
    }

    events: list[tuple[str, Event]] = [
        (
            "market",
            Event(
                type=EventType.MARKET_SIGNAL_QUALIFIED,
                occurred_at=now,
                key=f"demo:{DEMO_SYMBOL}:watch",
                payload={
                    **base,
                    "title": f"{DEMO_MARKER} WATCH — developing setup",
                    "score": 68.0,
                    "confidence": 0.71,
                    "state": "DISCOVERED",
                    "note": "below the 75 threshold: watch only, not a trade signal",
                },
            ),
        ),
        (
            "market",
            Event(
                type=EventType.MARKET_SIGNAL_QUALIFIED,
                occurred_at=now + timedelta(minutes=1),
                key=f"demo:{DEMO_SYMBOL}:qualified",
                payload={
                    **base,
                    "title": f"{DEMO_MARKER} QUALIFIED — bullish setup",
                    "score": 76.4,
                    "confidence": 0.80,
                    "state": "QUALIFIED",
                },
            ),
        ),
        (
            "market",
            Event(
                type=EventType.MARKET_SIGNAL_STRENGTHENED,
                occurred_at=now + timedelta(minutes=2),
                key=f"demo:{DEMO_SYMBOL}:strong",
                payload={
                    **base,
                    "title": f"{DEMO_MARKER} STRONG — bullish setup",
                    "score": 87.1,
                    "confidence": 0.86,
                    "state": "STRONG",
                },
            ),
        ),
        (
            "market",
            Event(
                type=EventType.MARKET_SIGNAL_INVALIDATED,
                occurred_at=now + timedelta(minutes=3),
                key=f"demo:{DEMO_SYMBOL}:weakened",
                payload={
                    **base,
                    "title": f"{DEMO_MARKER} WEAKENED — thesis deteriorating",
                    "score": 64.2,
                    "confidence": 0.62,
                    "state": "WEAKENED",
                },
            ),
        ),
    ]

    for key in PORTFOLIO_KEYS:
        events.append(
            (
                key,
                Event.paper_trade_opened(
                    symbol=DEMO_SYMBOL,
                    payload={
                        "title": f"{DEMO_MARKER} SIMULATED BUY",
                        "symbol": DEMO_SYMBOL,
                        "profile": key,
                        "quantity": "1",
                        "price": "100.00",
                        "demo": True,
                    },
                    routing_key=key,
                ),
            )
        )
        events.append(
            (
                key,
                Event.paper_trade_closed(
                    symbol=DEMO_SYMBOL,
                    payload={
                        "title": f"{DEMO_MARKER} SIMULATED SELL (take profit)",
                        "symbol": DEMO_SYMBOL,
                        "profile": key,
                        "exit_reason": "TAKE_PROFIT",
                        "gross_pnl": "2.00",
                        "fees": "2.00",
                        "net_pnl": "0.00",
                        "demo": True,
                    },
                    routing_key=key,
                ),
            )
        )

    events.append(
        (
            "system",
            Event(
                type=EventType.PROVIDER_DISCONNECTED,
                occurred_at=now + timedelta(minutes=4),
                key="demo:provider",
                payload={
                    "title": f"{DEMO_MARKER} PROVIDER UNAVAILABLE",
                    "provider": "demo",
                    "error": "synthetic failure for routing verification",
                    "demo": True,
                },
            ),
        )
    )
    events.append(
        (
            "system",
            Event(
                type=EventType.PROVIDER_RECOVERED,
                occurred_at=now + timedelta(minutes=5),
                key="demo:provider",
                payload={
                    "title": f"{DEMO_MARKER} PROVIDER RECOVERED",
                    "provider": "demo",
                    "demo": True,
                },
            ),
        )
    )
    events.append(
        (
            "performance",
            Event(
                type=EventType.DAILY_SIMULATION_SUMMARY,
                occurred_at=now + timedelta(minutes=6),
                key="demo:summary",
                payload={
                    "title": f"{DEMO_MARKER} DAILY SUMMARY",
                    "demo": True,
                    "portfolios": [
                        {
                            "profile": key,
                            "equity": 0.0,
                            "realized_pnl": 0.0,
                            "open_positions": 0,
                            "closed_trades": 0,
                        }
                        for key in PORTFOLIO_KEYS
                    ],
                },
            ),
        )
    )
    return events
