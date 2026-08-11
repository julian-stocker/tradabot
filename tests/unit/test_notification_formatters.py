"""Message formatting.

Pure functions over events, so no database and no transport. The assertions that
matter most are the negative ones: that a formatter omits what it was not given
rather than inventing it, and that a trade message never presents gross as net.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.core.events import Event, EventCategory, EventType, Severity
from app.notifications.formatters import format_event
from app.notifications.models import NotificationMessage

T0 = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)

FAKE_KEY = "PKTESTFAKE1234567890"
FAKE_HOOK = "https://discord.com/api/webhooks/111/secret-token-aaaa"


def event(event_type: EventType, payload: dict[str, object]) -> Event:
    return Event(type=event_type, occurred_at=T0, payload=payload, key="k")


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
def test_a_full_signal_renders_every_section() -> None:
    message = format_event(
        event(
            EventType.MARKET_SIGNAL_QUALIFIED,
            {
                "symbol": "NVDA",
                "score": 86.0,
                "confidence": 0.75,
                "horizon": "5d",
                "classification": "strong_bullish",
                "components": {"trend": 88, "momentum": 81, "volume": 84},
                "reasons": ["higher highs", "EMA20 > EMA50"],
                "risks": ["RSI elevated"],
                "bid": Decimal("100.10"),
                "ask": Decimal("100.20"),
                "spread_bps": Decimal("10"),
                "net_edge_bps": Decimal("281"),
            },
        )
    )

    text = message.rendered(4000)
    assert "NVDA" in text
    assert "86.00 / 100" in text
    assert "HIGH" in text, "confidence is a word, not a number"
    assert "trend" in text
    assert "higher highs" in text
    assert "RSI elevated" in text
    assert "281.00 bps" in text
    assert message.category is EventCategory.MARKET
    assert message.severity is Severity.SIGNAL


def test_a_sparse_signal_omits_what_it_lacks() -> None:
    """A historical evaluation has no live quote and may have no components.

    The sections vanish. Rendering empty scaffolding, or a placeholder that looks
    like a number, would put a fabricated metric on a channel people trust.
    """
    message = format_event(
        event(EventType.MARKET_SIGNAL_QUALIFIED, {"symbol": "NVDA", "score": 80.0})
    )

    text = message.rendered(4000)
    assert "NVDA" in text
    assert "80.00" in text
    assert "Confidence" not in text
    assert "Current quote" not in text
    assert "Reasons" not in text
    assert "Risks" not in text


def test_confidence_is_never_shown_as_a_probability() -> None:
    """It measures component agreement, not the chance of being right.

    Printing "0.72" invites reading it as a 72% likelihood, which it is not.
    """
    message = format_event(
        event(
            EventType.MARKET_SIGNAL_QUALIFIED,
            {"symbol": "NVDA", "score": 80.0, "confidence": 0.72},
        )
    )

    text = message.rendered(4000)
    assert "HIGH" in text
    assert "0.72" not in text


def test_an_invalidation_says_so_plainly() -> None:
    message = format_event(
        event(
            EventType.MARKET_SIGNAL_INVALIDATED,
            {"symbol": "NVDA", "score": 61.0, "previous_score": 86.0},
        )
    )

    text = message.rendered(4000)
    assert "INVALIDATED" in text
    assert "Previous: 86.00" in text


def test_a_long_reason_list_is_capped_and_counted() -> None:
    message = format_event(
        event(
            EventType.MARKET_SIGNAL_QUALIFIED,
            {"symbol": "NVDA", "score": 80.0, "reasons": [f"reason {i}" for i in range(12)]},
        )
    )

    text = message.rendered(4000)
    assert "7 more" in text, "the count survives even though the detail does not"


# ---------------------------------------------------------------------------
# Paper trades
# ---------------------------------------------------------------------------
def test_multi_profile_decisions_group_into_one_message() -> None:
    """Nine portfolios evaluating one signal is one thing that happened."""
    message = format_event(
        event(
            EventType.PAPER_TRADE_OPENED,
            {
                "symbol": "NVDA",
                "score": 86.0,
                "decisions": [
                    {"profile": "50eur-conservative", "decision": "skip", "reason": "cost"},
                    {"profile": "500eur-balanced", "decision": "trade"},
                    {"profile": "5000eur-aggressive", "decision": "trade"},
                ],
                "positions_opened": 2,
                "entries_rejected": 1,
            },
        )
    )

    text = message.rendered(4000)
    assert text.count("PAPER TRADE DECISION") == 1
    for profile in ("50eur-conservative", "500eur-balanced", "5000eur-aggressive"):
        assert profile in text
    assert "SKIP" in text
    assert "TRADE" in text
    assert message.category is EventCategory.PAPER_TRADE


def test_a_closed_trade_itemises_costs_beside_the_gross_figure() -> None:
    """Reporting gross as net would flatter every result on the channel humans read."""
    message = format_event(
        event(
            EventType.PAPER_TRADE_CLOSED,
            {
                "symbol": "NVDA",
                "profile": "5000eur-balanced",
                "entry_price": Decimal("100.00"),
                "exit_price": Decimal("108.00"),
                "quantity": Decimal("12"),
                "holding": "4 bars",
                "exit_reason": "TAKE_PROFIT",
                "gross_pnl": Decimal("96.00"),
                "fees": Decimal("2.00"),
                "spread_cost": Decimal("1.20"),
                "slippage_cost": Decimal("0.60"),
                "net_pnl": Decimal("92.20"),
                "net_return": 1.84,
            },
        )
    )

    text = message.rendered(4000)
    assert "Gross P/L" in text
    assert "96.00" in text
    assert "Fees" in text
    assert "Spread" in text
    assert "Slippage" in text
    assert "Net P/L: +92.20" in text
    assert "+1.84%" in text
    assert "TAKE_PROFIT" in text


def test_a_losing_trade_shows_a_signed_loss() -> None:
    message = format_event(
        event(
            EventType.PAPER_TRADE_CLOSED,
            {"symbol": "NVDA", "net_pnl": Decimal("-42.50"), "net_return": -0.85},
        )
    )

    text = message.rendered(4000)
    assert "-42.50" in text
    assert "-0.85%" in text


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
def test_a_daily_summary_lists_every_portfolio() -> None:
    message = format_event(
        event(
            EventType.DAILY_SIMULATION_SUMMARY,
            {
                "session_date": "2024-06-03",
                "signals_evaluated": 40,
                "signals_qualified": 3,
                "entries": 2,
                "exits": 1,
                "portfolios": [
                    {
                        "profile": "500eur-balanced",
                        "equity": Decimal("512.40"),
                        "net_pnl": Decimal("12.40"),
                        "return_pct": 2.48,
                        "open_positions": 1,
                        "max_drawdown": -0.031,
                    },
                    {"profile": "50eur-conservative", "equity": Decimal("50.00")},
                ],
            },
        )
    )

    text = message.rendered(4000)
    assert "DAILY TRADABOT REPORT" in text
    assert "2024-06-03" in text
    assert "Signals evaluated: 40" in text
    assert "500eur-balanced" in text
    assert "50eur-conservative" in text
    assert message.category is EventCategory.PERFORMANCE


def test_a_portfolio_without_a_drawdown_simply_omits_it() -> None:
    """A portfolio with no equity curve has no meaningful drawdown to report."""
    message = format_event(
        event(
            EventType.DAILY_SIMULATION_SUMMARY,
            {"portfolios": [{"profile": "new", "equity": Decimal("500")}]},
        )
    )

    assert "dd" not in message.rendered(4000)


def test_an_empty_summary_says_so_rather_than_rendering_blank() -> None:
    message = format_event(event(EventType.DAILY_SIMULATION_SUMMARY, {}))

    assert "No activity recorded." in message.rendered(4000)


def test_an_overview_without_candidates_does_not_invent_any() -> None:
    """The formatter exists ahead of the scanner. It renders nothing from nothing."""
    message = format_event(event(EventType.MARKET_OVERVIEW, {"candidates": []}))

    assert "No qualified opportunities." in message.rendered(4000)


def test_an_overview_ranks_the_candidates_it_is_given() -> None:
    message = format_event(
        event(
            EventType.MARKET_OVERVIEW,
            {
                "candidates": [
                    {"symbol": "NVDA", "score": 88, "direction": "bullish", "horizon": "5d"},
                    {"symbol": "AMD", "score": 79},
                ]
            },
        )
    )

    text = message.rendered(4000)
    assert "1. **NVDA**" in text
    assert "2. **AMD**" in text


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
def test_a_stale_alert_reports_the_age_in_readable_units() -> None:
    message = format_event(
        event(
            EventType.STALE_MARKET_DATA_DETECTED,
            {"provider": "alpaca", "age_seconds": 5_400, "limit_seconds": 900},
        )
    )

    text = message.rendered(4000)
    assert "MARKET DATA STALE" in text
    assert "alpaca" in text
    assert "1h 30m" in text
    assert message.category is EventCategory.SYSTEM
    assert message.severity is Severity.WARNING


def test_a_recovery_reports_the_downtime() -> None:
    message = format_event(Event.provider_recovered(provider="alpaca", downtime_seconds=1_800))

    text = message.rendered(4000)
    assert "RECOVERED" in text
    assert "30m" in text


def test_a_recovery_without_a_known_downtime_omits_it() -> None:
    message = format_event(Event.provider_recovered(provider="alpaca", downtime_seconds=None))

    assert "Downtime" not in message.rendered(4000)


def test_lifecycle_messages_name_the_environment() -> None:
    message = format_event(
        Event.lifecycle(started=True, environment="development", provider="mock")
    )

    text = message.rendered(4000)
    assert "TRADABOT STARTED" in text
    assert "development" in text


def test_an_unknown_event_type_still_renders() -> None:
    """A type added later must not raise inside the delivery path."""
    message = format_event(
        event(EventType.MARKET_DATA_SYNC_COMPLETED, {"provider": "alpaca", "symbols": 8})
    )

    text = message.rendered(4000)
    assert "MarketDataSyncCompleted" in text
    assert "alpaca" in text


# ---------------------------------------------------------------------------
# Safety (Parts S and T)
# ---------------------------------------------------------------------------
def test_a_credential_shaped_payload_key_is_masked() -> None:
    message = format_event(
        event(EventType.CRITICAL_SYSTEM_ERROR, {"component": "provider", "api_key": FAKE_KEY})
    )

    assert FAKE_KEY not in message.rendered(4000)


def test_an_error_carrying_a_key_is_redacted_by_the_event() -> None:
    message = format_event(
        Event.critical_system_error(component="alpaca", error=f"auth failed api_key={FAKE_KEY}")
    )

    assert FAKE_KEY not in message.rendered(4000)


def test_an_error_carrying_a_webhook_url_is_redacted() -> None:
    message = format_event(
        Event.critical_system_error(component="discord", error=f"POST {FAKE_HOOK} failed")
    )

    assert "secret-token-aaaa" not in message.rendered(4000)


def test_truncation_keeps_the_beginning_and_marks_itself() -> None:
    message = NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        title="HEADLINE",
        body="x" * 5_000,
        event_type=EventType.MARKET_SIGNAL_QUALIFIED,
        occurred_at=T0,
    )

    rendered = message.rendered(200)

    assert len(rendered) <= 200
    assert rendered.startswith("HEADLINE")
    assert "truncated" in rendered


def test_a_short_message_is_not_touched() -> None:
    message = NotificationMessage(
        category=EventCategory.SYSTEM,
        severity=Severity.INFO,
        title="SHORT",
        body="body",
        event_type=EventType.TRADABOT_STARTED,
        occurred_at=T0,
    )

    assert message.rendered(2000) == "SHORT\nbody"
