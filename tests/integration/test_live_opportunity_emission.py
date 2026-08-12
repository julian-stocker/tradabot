"""The live scanner emission path, end to end, with a fake backend.

These go through the real code: `_notification_payload` builds it,
`format_event` renders it, `opportunity_fields` curates the embed grid. An
isolated policy test proves a rule; this proves the message a phone actually
receives.

**No test here reaches Discord.**
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import DiscordSettings, Environment, Settings
from app.core.events import Event, EventCategory, EventType
from app.db.models import Instrument, SignalEvaluation
from app.domain.enums import AssetType, Timeframe
from app.domain.quotes import Quote
from app.notifications.embeds import build_embed, build_payload
from app.notifications.formatters import format_event
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.service import NotificationService
from app.scanner.enums import DataQuality, SignalLifecycle, StructureState, TrendState
from app.scanner.service import _notification_payload
from app.scanner.timeframes import MultiTimeframeContext, TimeframeAssessment

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
FORBIDDEN_FIELDS = ("support", "resistance", "target", "entry zone", "expected price")


class Capturing:
    name = "capturing"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(backend=self.name, delivered=True)


def settings() -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        log_level="WARNING",
        discord=DiscordSettings(
            enabled=True,
            market_webhook=SecretStr("https://discord.com/api/webhooks/1/tok-market"),
        ),
    )


def instrument() -> Instrument:
    return Instrument(
        id=1,
        symbol="NVDA",
        name="NVIDIA Corporation Common Stock",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        is_active=True,
    )


def context() -> MultiTimeframeContext:
    """A realistic four-timeframe read: bullish intraday and short term."""

    def assessment(timeframe: Timeframe, trend: TrendState, role: str) -> TimeframeAssessment:
        return TimeframeAssessment(
            timeframe=timeframe,
            role=role,
            quality=DataQuality.OK,
            trend=trend,
            structure=StructureState.BREAKOUT,
            bar_timestamp=NOW,
            bars_used=80,
            close=182.45,
            rsi=64.2,
            atr_pct=1.8,
            relative_volume=2.4,
            volatility=0.31,
        )

    return MultiTimeframeContext(
        symbol="NVDA",
        assessments={
            Timeframe.M5: assessment(Timeframe.M5, TrendState.STRONG_UP, "entry"),
            Timeframe.M15: assessment(Timeframe.M15, TrendState.UP, "confirmation"),
            Timeframe.H1: assessment(Timeframe.H1, TrendState.UP, "primary"),
            Timeframe.D1: assessment(Timeframe.D1, TrendState.SIDEWAYS, "macro"),
        },
    )


def evaluation(score: float) -> SignalEvaluation:
    return SignalEvaluation(
        id=1,
        instrument_id=1,
        evaluated_at=NOW,
        market_data_timestamp=NOW,
        score=score,
        confidence=0.82,
        classification="STRONG_BULLISH",
        direction=1,
        qualified=score >= 75,
        data_quality=DataQuality.OK.value,
        session_phase="REGULAR",
        feature_set_version="features-v1",
        signal_model_version="signal-v1",
        scanner_policy_version="scanner-v1",
        timeframe_states={"1h": {"trend": "UP", "close": 182.45}},
    )


def live_payload(score: float, lifecycle: SignalLifecycle) -> dict[str, object]:
    """Exactly what the scanner builds, via the real function."""
    return _notification_payload(
        symbol="NVDA",
        signal=None,
        quote=Quote(symbol="NVDA", timestamp=NOW, bid=Decimal("182.40"), ask=Decimal("182.50")),
        evaluation=evaluation(score),
        instrument=instrument(),
        context=context(),
        lifecycle=lifecycle,
        settings=settings(),
        now=NOW,
    )


def rendered(
    score: float, lifecycle: SignalLifecycle, event_type: EventType
) -> NotificationMessage:
    return format_event(
        Event(type=event_type, occurred_at=NOW, payload=live_payload(score, lifecycle))
    )


# ---------------------------------------------------------------------------
# The live payload carries the real fields
# ---------------------------------------------------------------------------
def test_the_live_payload_carries_identity_and_verdict() -> None:
    payload = live_payload(76.4, SignalLifecycle.QUALIFIED)

    assert payload["symbol"] == "NVDA"
    assert payload["company_name"] == "NVIDIA Corporation Common Stock"
    assert payload["lifecycle_state"] == "QUALIFIED"
    assert payload["direction"] == 1
    assert payload["score"] == 76.4


def test_the_live_payload_carries_all_four_horizons() -> None:
    """LONG_TERM is present *because* it is unavailable -- omitting it would read
    as neutral."""
    payload = live_payload(87.1, SignalLifecycle.STRONG)

    assert payload["intraday"] == "BULLISH"
    assert payload["short_term"] == "BULLISH"
    assert payload["long_term"] == "NOT_AVAILABLE"


def test_the_live_payload_carries_components_and_freshness() -> None:
    payload = live_payload(76.4, SignalLifecycle.QUALIFIED)

    assert payload["trend"] == "UP"
    assert "positive" in str(payload["momentum"])
    assert "surging" in str(payload["volume"])
    assert payload["structure"] == "BREAKOUT"
    assert "normal" in str(payload["volatility"])
    assert payload["price"] == pytest.approx(182.45)
    assert "UTC" in str(payload["market_data_timestamp"])
    assert "min old" in str(payload["freshness"])
    assert payload["provider"] == "mock"


def test_the_live_payload_never_carries_a_price_target() -> None:
    """**The constraint that keeps this honest.** None of these are computed."""
    payload = live_payload(87.1, SignalLifecycle.STRONG)

    for key in payload:
        assert not any(word in key.lower() for word in FORBIDDEN_FIELDS), key


# ---------------------------------------------------------------------------
# Rendered embeds: QUALIFIED, STRONG, INVALIDATED
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "lifecycle", "event_type"),
    [
        (76.4, SignalLifecycle.QUALIFIED, EventType.MARKET_SIGNAL_QUALIFIED),
        (87.1, SignalLifecycle.STRONG, EventType.MARKET_SIGNAL_STRENGTHENED),
        (64.2, SignalLifecycle.INVALIDATED, EventType.MARKET_SIGNAL_INVALIDATED),
    ],
)
def test_each_transition_renders_a_usable_embed(
    score: float, lifecycle: SignalLifecycle, event_type: EventType
) -> None:
    embed = build_embed(rendered(score, lifecycle, event_type))
    names = [field["name"] for field in embed["fields"]]

    assert embed["title"]
    assert {"Score", "Confidence", "Intraday", "Long term", "Source"} <= set(names)
    assert names.index("Score") < names.index("Intraday") < names.index("Source"), (
        "fields must read identity -> verdict -> horizons -> freshness"
    )


def test_the_embed_uses_human_names_not_payload_keys() -> None:
    embed = build_embed(
        rendered(87.1, SignalLifecycle.STRONG, EventType.MARKET_SIGNAL_STRENGTHENED)
    )
    names = {field["name"] for field in embed["fields"]}

    assert "Short term" in names
    assert "short_term" not in names
    assert "net_edge_bps" not in names


def test_confidence_renders_as_a_percentage_not_a_fraction() -> None:
    """0.82 is 82%, not a score of 0.8 -- a confusion this codebase already made
    once in its own analysis."""
    embed = build_embed(
        rendered(76.4, SignalLifecycle.QUALIFIED, EventType.MARKET_SIGNAL_QUALIFIED)
    )
    value = next(f["value"] for f in embed["fields"] if f["name"] == "Confidence")

    assert value == "82%"


def test_an_embed_never_shows_a_fabricated_field() -> None:
    embed = build_embed(
        rendered(87.1, SignalLifecycle.STRONG, EventType.MARKET_SIGNAL_STRENGTHENED)
    )

    for field in embed["fields"]:
        assert field["value"].strip(), f"empty field {field['name']}"
        assert not any(word in field["name"].lower() for word in FORBIDDEN_FIELDS)


def test_plaintext_still_carries_the_message() -> None:
    """The fallback is not degraded: a client with no embeds still sees it all."""
    message = rendered(87.1, SignalLifecycle.STRONG, EventType.MARKET_SIGNAL_STRENGTHENED)
    payload = build_payload(message, max_characters=2000, use_embeds=False)

    assert "embeds" not in payload
    assert "NVDA" in payload["content"]
    assert "Score" in payload["content"] or "87.1" in payload["content"]


def test_no_credential_reaches_the_rendered_message() -> None:
    message = rendered(87.1, SignalLifecycle.STRONG, EventType.MARKET_SIGNAL_STRENGTHENED)
    blob = (message.rendered(4000) + str(message.fields) + str(build_embed(message))).lower()

    for forbidden in ("discord.com", "webhook", "tok-", "api_key", "secret"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# Transitions emit exactly once, and survive a restart
# ---------------------------------------------------------------------------
@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


async def test_a_repeated_qualified_state_is_announced_once(
    factory: async_sessionmaker,
) -> None:
    """**The anti-spam property, through the real notify_signal path.**"""
    backend = Capturing()
    service = NotificationService(settings(), backends=[backend], session_factory=factory)
    payload = live_payload(76.4, SignalLifecycle.QUALIFIED)

    first = await service.notify_signal(
        symbol="NVDA", timeframe="1h", horizon="5d", score=76.4, payload=payload
    )
    second = await service.notify_signal(
        symbol="NVDA", timeframe="1h", horizon="5d", score=77.1, payload=payload
    )

    assert first is True
    assert second is False, "a still-qualified signal was announced twice"
    assert len(backend.messages) == 1


async def test_a_strengthening_signal_announces_again(
    factory: async_sessionmaker,
) -> None:
    backend = Capturing()
    service = NotificationService(settings(), backends=[backend], session_factory=factory)

    await service.notify_signal(
        symbol="NVDA",
        timeframe="1h",
        horizon="5d",
        score=76.4,
        payload=live_payload(76.4, SignalLifecycle.QUALIFIED),
    )
    upgraded = await service.notify_signal(
        symbol="NVDA",
        timeframe="1h",
        horizon="5d",
        score=87.1,
        payload=live_payload(87.1, SignalLifecycle.STRONG),
    )

    assert upgraded is True
    assert len(backend.messages) == 2


async def test_a_restart_does_not_replay_a_transition(
    factory: async_sessionmaker,
) -> None:
    """**Restart persistence.**

    State lives in the database, not in the service, so a fresh process with a
    fresh backend still knows the signal was already announced.
    """
    first_run = Capturing()
    service = NotificationService(settings(), backends=[first_run], session_factory=factory)
    payload = live_payload(76.4, SignalLifecycle.QUALIFIED)
    await service.notify_signal(
        symbol="NVDA", timeframe="1h", horizon="5d", score=76.4, payload=payload
    )

    # A completely new service and backend: the process restarted.
    after_restart = Capturing()
    revived = NotificationService(settings(), backends=[after_restart], session_factory=factory)
    again = await revived.notify_signal(
        symbol="NVDA", timeframe="1h", horizon="5d", score=76.6, payload=payload
    )

    assert again is False
    assert after_restart.messages == []


async def test_a_below_threshold_score_produces_no_buy_style_message(
    factory: async_sessionmaker,
) -> None:
    """DISCOVERED is tracked, not announced."""
    backend = Capturing()
    service = NotificationService(settings(), backends=[backend], session_factory=factory)

    sent = await service.notify_signal(
        symbol="NVDA",
        timeframe="1h",
        horizon="5d",
        score=68.0,
        payload=live_payload(68.0, SignalLifecycle.DISCOVERED),
    )

    assert sent is False
    assert backend.messages == []


async def test_a_qualified_signal_reaches_the_market_category(
    factory: async_sessionmaker,
) -> None:
    backend = Capturing()
    service = NotificationService(settings(), backends=[backend], session_factory=factory)

    await service.notify_signal(
        symbol="NVDA",
        timeframe="1h",
        horizon="5d",
        score=76.4,
        payload=live_payload(76.4, SignalLifecycle.QUALIFIED),
    )

    assert backend.messages[0].category is EventCategory.MARKET
    assert backend.messages[0].fields["Intraday"] == "BULLISH"
