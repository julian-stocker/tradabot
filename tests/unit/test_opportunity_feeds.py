"""Future WATCH / BUY / SELL feeds, and the fallback that keeps today working.

The whole point of this layer is that it changes nothing until a webhook exists.
These tests assert both halves: the vocabulary is correct, *and* the absence of
the new channels leaves current production untouched.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.config import DiscordSettings
from app.core.events import EventCategory
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.feeds import HEADLINES, WATCH_STATUS, FeedKey, classify
from app.scanner.enums import SignalLifecycle

MARKET = "https://discord.com/api/webhooks/1/tok-market"


def notifier(**overrides: object) -> DiscordWebhookNotifier:
    settings = DiscordSettings(
        enabled=True,
        market_webhook=SecretStr(MARKET),
        **overrides,  # type: ignore[arg-type]
    )
    return DiscordWebhookNotifier(settings)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lifecycle", [SignalLifecycle.QUALIFIED, SignalLifecycle.STRONG])
def test_a_bullish_qualified_setup_is_a_buy_opportunity(
    lifecycle: SignalLifecycle,
) -> None:
    assert classify(lifecycle, direction=1) is FeedKey.BUY


def test_a_qualified_bearish_setup_is_not_a_buy() -> None:
    """Production is long-only; a bearish qualification is not a buy signal."""
    assert classify(SignalLifecycle.QUALIFIED, direction=-1) is not FeedKey.BUY


@pytest.mark.parametrize("lifecycle", [SignalLifecycle.WEAKENED, SignalLifecycle.INVALIDATED])
def test_a_broken_thesis_is_an_exit_signal(lifecycle: SignalLifecycle) -> None:
    assert classify(lifecycle, direction=1) is FeedKey.SELL_EXIT


def test_an_exit_signal_is_never_a_short_opportunity() -> None:
    """**The conflation this module exists to prevent.**

    "Close the position" and "go short" are opposite instructions that happen to
    share a direction of trade. Production is long-only and must not imply the
    second.
    """
    key = classify(SignalLifecycle.INVALIDATED, direction=1)

    assert key is FeedKey.SELL_EXIT
    assert "exit" in HEADLINES[key].lower()
    assert "short" not in HEADLINES[key].lower()


def test_a_discovered_setup_produces_no_message() -> None:
    assert classify(SignalLifecycle.DISCOVERED, direction=1) is None


def test_an_expired_setup_produces_no_message() -> None:
    """Ageing out is bookkeeping. Announcing it would put a message in a channel
    for something that by definition stopped being interesting."""
    assert classify(SignalLifecycle.EXPIRED, direction=1) is None


def test_watch_is_declared_unimplemented_rather_than_guessed() -> None:
    """**The 70-75 band is the worst in the dataset.**

    Defining WATCH as "just below threshold" would promote exactly the weakest
    evidence available (49.0% positive at 1d vs a 51.6% baseline) to its own
    channel.
    """
    assert WATCH_STATUS == "NOT_IMPLEMENTED"
    assert classify(SignalLifecycle.DISCOVERED, direction=1) is not FeedKey.WATCH


def test_the_wording_never_says_buy_outright() -> None:
    """tradabot places no orders and its measured edge is a few points on 57
    episodes. Absolute language would claim more than the numbers support."""
    assert HEADLINES[FeedKey.BUY] == "BULLISH OPPORTUNITY"
    for headline in HEADLINES.values():
        assert not headline.startswith("BUY ")
        assert "guaranteed" not in headline.lower()


# ---------------------------------------------------------------------------
# Fallback: absent feed webhooks must not break anything
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("feed", list(FeedKey))
def test_an_unconfigured_feed_falls_back_to_market_signals(feed: FeedKey) -> None:
    """**Part 7.** No new webhook is required for current behaviour."""
    resolved = notifier().webhook_for(EventCategory.MARKET, feed.value)

    assert resolved == MARKET


def test_an_unconfigured_portfolio_still_stays_silent() -> None:
    """Portfolio keys must NOT fall back: merging one portfolio's trades into a
    shared channel would misattribute them, and a wrong number is worse than a
    missing one."""
    resolved = notifier().webhook_for(EventCategory.PAPER_TRADE, "paper-100")

    assert resolved is None


def test_a_configured_feed_webhook_wins_over_the_fallback() -> None:
    """Adding the channel later is configuration, not a code change."""
    dedicated = "https://discord.com/api/webhooks/9/tok-buy"
    settings = DiscordSettings(
        enabled=True,
        market_webhook=SecretStr(MARKET),
        portfolio_webhooks={FeedKey.BUY.value: SecretStr(dedicated)},
    )

    resolved = DiscordWebhookNotifier(settings).webhook_for(EventCategory.MARKET, FeedKey.BUY.value)

    assert resolved == dedicated


def test_market_messages_without_a_feed_key_are_unchanged() -> None:
    """The path production uses today."""
    assert notifier().webhook_for(EventCategory.MARKET, None) == MARKET


def test_the_feed_keys_are_stable_channel_names() -> None:
    assert FeedKey.WATCH.value == "watch-opportunities"
    assert FeedKey.BUY.value == "buy-opportunities"
    assert FeedKey.SELL_EXIT.value == "sell-exit-signals"
