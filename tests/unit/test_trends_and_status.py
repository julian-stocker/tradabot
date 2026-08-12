"""Market-trends observation and the #status dashboard.

Two channels with opposite failure modes. Trends fails by becoming advice or
becoming noise; status fails by becoming a log. Both are guarded here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import DiscordSettings
from app.notifications.dashboard import (
    DEGRADED,
    HEARTBEAT,
    OFFLINE,
    ONLINE,
    DashboardState,
    build_fields,
    fingerprint,
    server_state,
    should_publish,
)
from app.notifications.trends import (
    COOLDOWN,
    DISCLAIMER,
    FORBIDDEN_WORDS,
    TrendEvent,
    TrendSignal,
    TrendState,
    assert_no_recommendation_language,
    build_payload,
    detect,
    rank,
    session_allows_trends,
    should_notify,
)
from app.scanner.enums import SessionPhase

NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)


class Portfolio:
    def __init__(self, key: str) -> None:
        self.key = key
        self.equity = 1000.0
        self.open_positions = 0
        self.closed_trades = 0


class Status:
    """Shaped like OperationalStatus, which the dashboard renders."""

    def __init__(self, **kw: object) -> None:
        self.session_phase = "REGULAR"
        self.last_sync: datetime | None = NOW - timedelta(minutes=3)
        self.last_scan: datetime | None = NOW - timedelta(minutes=10)
        self.last_error: str | None = None
        self.last_sync_duration = 60.0
        self.last_sync_symbols = 52
        self.last_sync_failures = 0
        self.last_scan_duration = 45.0
        self.last_scan_evaluated = 52
        self.last_scan_qualified = 0
        self.last_scan_strong = 0
        self.universe_size = 52
        self.watchlist_size = 52
        self.evaluations_stored = 1234
        self.portfolios = [Portfolio(k) for k in ("paper-100", "paper-1000", "paper-10000")]
        for name, value in kw.items():
            setattr(self, name, value)


def fields_for(status: Status, **kw: object) -> dict[str, str]:
    base: dict[str, object] = {
        "environment": "development",
        "provider": "alpaca",
        "feed": "iex",
        "revision": "0010",
        "db_bytes": 1_500_000_000,
        "candles": 2_860_167,
        "discord_destinations": 6,
        "now": NOW,
    }
    return build_fields(status, **(base | kw))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Trends: observation, never advice
# ---------------------------------------------------------------------------
def test_a_large_move_is_notable() -> None:
    found = detect(symbol="NVDA", change_1d_pct=4.2, change_5d_pct=8.1)

    assert found[0].event is TrendEvent.STRONG_MOVE_UP
    assert "+4.2%" in found[0].headline


def test_a_large_fall_is_equally_notable() -> None:
    """Descriptive, so down moves matter as much as up ones."""
    found = detect(symbol="AMD", change_1d_pct=-3.7)

    assert found[0].event is TrendEvent.STRONG_MOVE_DOWN


def test_an_ordinary_move_is_not_notable() -> None:
    assert detect(symbol="KO", change_1d_pct=0.4, relative_volume=1.1) == []


def test_volume_and_volatility_are_detected_independently() -> None:
    found = detect(symbol="NVDA", relative_volume=2.4, volatility=0.55)
    events = {signal.event for signal in found}

    assert TrendEvent.VOLUME_SPIKE in events
    assert TrendEvent.VOLATILITY_EXPANSION in events


def test_detection_never_consults_the_signal_score() -> None:
    """**The point of the channel.** A 4% move on heavy volume is worth seeing
    whether or not the setup qualified at 75."""
    import inspect

    from app.notifications import trends

    source = inspect.getsource(trends.detect)
    body = source[source.index('"""', source.index('"""') + 3) + 3 :]
    assert "score" not in body, "detection must not read the signal score"
    assert "75" not in body, "detection must not reference the qualification threshold"


@pytest.mark.parametrize("word", FORBIDDEN_WORDS)
def test_recommendation_language_is_rejected(word: str) -> None:
    """Phase 5.8 classified the score PROMISING_BUT_INSUFFICIENT, so this channel
    may describe the market and must never advise on it."""
    with pytest.raises(ValueError, match=word):
        assert_no_recommendation_language(f"NVDA looks like a {word}")


def test_a_normal_observation_passes_the_language_guard() -> None:
    assert_no_recommendation_language("NVDA +4.2% today, volume 1.9x average")


def test_the_payload_carries_the_disclaimer() -> None:
    payload = build_payload(detect(symbol="NVDA", change_1d_pct=4.2), context={})

    assert payload["disclaimer"] == DISCLAIMER
    assert "not a trade recommendation" in payload["disclaimer"]


def test_ranking_returns_only_the_few_most_notable() -> None:
    """Five is a glance; fifty-two is a spreadsheet."""
    signals = [
        TrendSignal(f"S{i}", TrendEvent.STRONG_MOVE_UP, float(i), f"+{i}%") for i in range(20)
    ]

    top = rank(signals, limit=5)

    assert len(top) == 5
    assert top[0].value == 19.0


# ---------------------------------------------------------------------------
# Trends: noise policy
# ---------------------------------------------------------------------------
def test_a_new_observation_is_announced() -> None:
    signal = TrendSignal("NVDA", TrendEvent.STRONG_MOVE_UP, 4.2, "+4.2%")

    assert should_notify(signal, None, now=NOW)


def test_the_same_condition_stays_silent() -> None:
    """**A stock up 4% at 15:00 is still up 4% at 16:00.**"""
    signal = TrendSignal("NVDA", TrendEvent.STRONG_MOVE_UP, 4.3, "+4.3%")
    state = TrendState(signal.key, last_notified_at=NOW - timedelta(hours=1), last_value=4.2)

    assert not should_notify(signal, state, now=NOW)


def test_a_materially_larger_move_speaks_again() -> None:
    signal = TrendSignal("NVDA", TrendEvent.STRONG_MOVE_UP, 7.0, "+7.0%")
    state = TrendState(signal.key, last_notified_at=NOW - timedelta(hours=1), last_value=4.2)

    assert should_notify(signal, state, now=NOW)


def test_the_cooldown_eventually_expires() -> None:
    signal = TrendSignal("NVDA", TrendEvent.STRONG_MOVE_UP, 4.2, "+4.2%")
    state = TrendState(signal.key, last_notified_at=NOW - COOLDOWN, last_value=4.2)

    assert should_notify(signal, state, now=NOW)


# ---------------------------------------------------------------------------
# Trends: session policy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session", [SessionPhase.CLOSED, SessionPhase.WEEKEND, SessionPhase.HOLIDAY]
)
def test_a_closed_market_produces_no_trend_messages(session: SessionPhase) -> None:
    allowed, reason = session_allows_trends(session)

    assert not allowed
    assert "market" in reason


@pytest.mark.parametrize("session", [SessionPhase.PRE_MARKET, SessionPhase.AFTER_HOURS])
def test_extended_hours_are_suppressed_for_a_data_reason(session: SessionPhase) -> None:
    """A 'volume spike' from a handful of IEX prints is a feed artefact."""
    allowed, reason = session_allows_trends(session)

    assert not allowed
    assert "thin" in reason


def test_the_regular_session_allows_trends() -> None:
    allowed, _ = session_allows_trends(SessionPhase.REGULAR)

    assert allowed


def test_extended_hours_can_be_enabled_deliberately() -> None:
    allowed, _ = session_allows_trends(SessionPhase.PRE_MARKET, conservative_extended=False)

    assert allowed


# ---------------------------------------------------------------------------
# Status dashboard
# ---------------------------------------------------------------------------
def test_recent_jobs_report_online() -> None:
    assert server_state(Status(), now=NOW) == ONLINE


def test_a_recorded_error_degrades_the_status() -> None:
    assert server_state(Status(last_error="provider timeout"), now=NOW) == DEGRADED


def test_a_late_sync_degrades_but_does_not_offline() -> None:
    late = Status(last_sync=NOW - timedelta(hours=2))

    assert server_state(late, now=NOW) == DEGRADED


def test_everything_late_reports_offline() -> None:
    dead = Status(last_sync=NOW - timedelta(hours=5), last_scan=NOW - timedelta(hours=5))

    assert server_state(dead, now=NOW) == OFFLINE


def test_no_recorded_run_at_all_is_offline() -> None:
    assert server_state(Status(last_sync=None, last_scan=None), now=NOW) == OFFLINE


def test_the_dashboard_covers_every_required_area() -> None:
    fields = fields_for(Status())

    for required in ("Server", "Environment", "Provider", "Last sync", "Last scan", "Database"):
        assert required in fields
    for portfolio in ("paper-100", "paper-1000", "paper-10000"):
        assert portfolio in fields


def test_absent_values_are_named_not_shown_as_zero() -> None:
    """'No scan recorded' and 'scanned zero symbols' are different situations.

    Said explicitly rather than left out: a dashboard that silently drops the
    line makes a fresh installation and a scanner that stopped working look
    identical, and the second one is the reason anyone opens #status.
    """
    fields = fields_for(Status(last_scan=None))

    assert fields["Last scan"] == "never"
    assert "Scan result" not in fields, "there is no result to report"

    empty = fields_for(Status(evaluations_stored=0), candles=None)
    assert empty["Candles"] == "N/A"
    assert empty["Evaluations"] == "N/A"


def test_the_dashboard_contains_no_secret() -> None:
    blob = str(fields_for(Status())).lower()

    for forbidden in ("discord.com", "webhook", "tok-", "secret", "api_key"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# Dashboard cadence
# ---------------------------------------------------------------------------
def test_the_first_publication_always_happens() -> None:
    publish, reason = should_publish(DashboardState(), fields_for(Status()), now=NOW)

    assert publish
    assert reason == "first publication"


def test_an_unchanged_dashboard_stays_quiet() -> None:
    """**Otherwise #status becomes a log.**"""
    fields = fields_for(Status())
    state = DashboardState(
        message_id="1", published_at=NOW - timedelta(minutes=5), fingerprint=fingerprint(fields)
    )

    publish, reason = should_publish(state, fields, now=NOW)

    assert not publish
    assert reason == "unchanged"


def test_a_changed_value_publishes_promptly() -> None:
    before = fields_for(Status())
    state = DashboardState(
        message_id="1", published_at=NOW - timedelta(minutes=1), fingerprint=fingerprint(before)
    )
    after = fields_for(Status(last_error="provider timeout"))

    publish, reason = should_publish(state, after, now=NOW)

    assert publish
    assert reason == "status changed"


def test_the_heartbeat_refreshes_even_when_nothing_changed() -> None:
    """A dashboard that stops updating looks identical to a dead process."""
    fields = fields_for(Status())
    state = DashboardState(
        message_id="1", published_at=NOW - HEARTBEAT, fingerprint=fingerprint(fields)
    )

    publish, reason = should_publish(state, fields, now=NOW)

    assert publish
    assert reason == "heartbeat"


def test_the_fingerprint_ignores_everything_that_moves_with_the_clock() -> None:
    """**Otherwise the dashboard republishes on every tick.**

    `Last sync: 3m ago` becomes `4m ago` a minute later with nothing having
    changed. Including that in the fingerprint would mark every run as a change
    and defeat the deduplication entirely.
    """
    early = fields_for(Status(), now=NOW)
    later = fields_for(Status(), now=NOW + timedelta(minutes=7))

    assert early["Checked"] != later["Checked"]
    assert early["Last sync"] != later["Last sync"]
    assert fingerprint(early) == fingerprint(later)


def test_the_fingerprint_still_notices_a_real_change() -> None:
    """Suppression must not become blindness."""
    before = fields_for(Status())
    after = fields_for(Status(last_scan_qualified=3))

    assert fingerprint(before) != fingerprint(after)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_the_new_channels_are_optional() -> None:
    """Startup must not fail because a future channel does not exist."""
    settings = DiscordSettings()

    assert settings.trends_webhook.get_secret_value() == ""
    assert settings.status_webhook.get_secret_value() == ""


def test_configuring_them_registers_routing_keys() -> None:
    from pydantic import SecretStr

    settings = DiscordSettings(
        enabled=True,
        market_webhook=SecretStr("https://x/webhooks/1/a"),
        trends_webhook=SecretStr("https://x/webhooks/2/b"),
        status_webhook=SecretStr("https://x/webhooks/3/c"),
    )

    assert "market-trends" in settings.portfolio_webhooks
    assert "status" in settings.portfolio_webhooks


def test_recommendation_feeds_remain_disabled() -> None:
    """Phase 5.8.1 ships intelligence, not advice."""
    from app.notifications.feeds import WATCH_STATUS

    assert WATCH_STATUS == "NOT_IMPLEMENTED"
