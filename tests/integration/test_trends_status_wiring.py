"""Phase 5.8.2: #market-trends and #status running as scheduled jobs.

Every test here is offline. No test constructs a real webhook URL that resolves,
no test performs HTTP, and the two "preview" tests assert that fact directly by
handing the services a client that fails on any request.

What is being verified is the *wiring*, not the policy -- the detection rules and
the cooldown arithmetic are covered by ``tests/unit/test_trends_and_status.py``.
Here the questions are: does the scheduled entry point read persisted state, does
it route to the dedicated webhook, does it stay silent when it should, and can
its failure reach the scanner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import DiscordSettings, Environment, ScannerSettings, Settings
from app.core.events import EventType
from app.db.models import NotificationState, SignalEvaluation
from app.instruments.repository import InstrumentRepository
from app.notifications.backends.discord import DiscordWebhookNotifier
from app.notifications.dashboard import (
    DASHBOARD_KEY,
    DASHBOARD_SCOPE,
    DEGRADED,
    ONLINE,
    DashboardState,
)
from app.notifications.feeds import TRENDS_ROUTING_KEY
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.repository import NotificationRepository
from app.notifications.service import NotificationService
from app.notifications.status_service import StatusService
from app.notifications.trends_service import TrendsService
from app.ops.launchd import scheduled_jobs
from app.scanner.repository import SCOPE_SCAN, SCOPE_SYNC, ScanRunRepository

pytestmark = pytest.mark.integration

# A Friday inside the regular US session, and the weekend that follows it.
REGULAR = datetime(2024, 6, 7, 14, 0, tzinfo=UTC)
PREMARKET = datetime(2024, 6, 7, 11, 0, tzinfo=UTC)
AFTER_HOURS = datetime(2024, 6, 7, 21, 0, tzinfo=UTC)
WEEKEND = datetime(2024, 6, 8, 15, 30, tzinfo=UTC)

HOOK = "https://discord.com/api/webhooks/{}/tok-{}"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------
class CapturingBackend:
    """Accepts everything and remembers it. Never touches the network."""

    name = "capturing"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(backend=self.name, delivered=True, attempts=1)


class BrokenBackend:
    """Fails the way a real outage does, and records that it was tried."""

    name = "broken"

    def __init__(self, *, raises: bool = False) -> None:
        self.attempts = 0
        self._raises = raises

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.attempts += 1
        if self._raises:
            msg = "discord is on fire"
            raise RuntimeError(msg)
        return DeliveryResult(backend=self.name, delivered=False, error="HTTP 500", attempts=3)


class FakeDiscord:
    """A Discord webhook endpoint, in memory.

    Models the two behaviours the dashboard depends on: ``?wait=true`` returns
    the created message, and a PATCH to a message that no longer exists is
    rejected with 404.
    """

    def __init__(self, *, patch_status: int = 200) -> None:
        self.posts: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []
        self.patch_status = patch_status
        self._next_id = 1000

    async def handle(self, request: httpx.Request) -> httpx.Response:
        payload = dict(request.url.params)
        del payload
        if request.method == "PATCH":
            self.patches.append({"url": str(request.url)})
            return httpx.Response(self.patch_status, json={"id": str(request.url).split("/")[-1]})
        self._next_id += 1
        self.posts.append({"url": str(request.url)})
        return httpx.Response(200, json={"id": str(self._next_id)})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def offline_client() -> httpx.AsyncClient:
    """A client that fails loudly if anything tries to use it.

    Used by the preview tests: "sends nothing" is asserted by making a send
    impossible, not by trusting that no code path reaches out.
    """

    async def refuse(request: httpx.Request) -> httpx.Response:
        msg = f"a preview attempted HTTP {request.method} {request.url.host}"
        raise AssertionError(msg)

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def make_settings(*, trends: bool = True, status: bool = True) -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url="sqlite+aiosqlite:///:memory:",
        market_data_provider="mock",
        log_level="WARNING",
        scanner=ScannerSettings(),
        discord=DiscordSettings(
            enabled=True,
            market_webhook=SecretStr(HOOK.format(1, "market")),
            system_webhook=SecretStr(HOOK.format(2, "sys")),
            trends_webhook=SecretStr(HOOK.format(3, "trends")) if trends else SecretStr(""),
            status_webhook=SecretStr(HOOK.format(4, "status")) if status else SecretStr(""),
        ),
    )


@pytest.fixture
def factory(engine: object) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


async def store_evaluation(
    session: AsyncSession,
    *,
    symbol: str,
    at: datetime,
    relative_volume: float = 1.0,
    volatility: float = 0.2,
    state: str = "RANGING",
) -> None:
    """One persisted evaluation, exactly as a scan cycle would leave it."""
    instrument = await InstrumentRepository(session).get_by_symbol(symbol)
    assert instrument is not None
    session.add(
        SignalEvaluation(
            instrument_id=instrument.id,
            evaluated_at=at,
            score=50.0,
            confidence=0.5,
            classification="NEUTRAL",
            direction=0,
            qualified=False,
            aligned=False,
            timeframe_states={},
            trend_metrics={},
            momentum_metrics={},
            volume_metrics={"relative_volume": relative_volume},
            volatility_metrics={"volatility": volatility},
            structure_metrics={"state": state},
            liquidity_metrics={},
            data_quality="OK",
            session_phase="REGULAR",
            feature_set_version="v1",
            signal_model_version="v1",
            scanner_policy_version="v1",
        )
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Trends: the scheduled path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_regular_session_event_is_published_from_stored_data(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The whole point: persisted scan output becomes a Discord message.

    No provider is constructed anywhere in this test, which is the assertion that
    matters -- the trends job must never buy a second copy of data the scanner
    already paid for.
    """
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)

    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    run = await service.publish(now=REGULAR)

    assert run.published
    assert run.messages_sent == 1
    assert backend.messages[0].event_type is EventType.MARKET_TRENDS
    assert backend.messages[0].routing_key == TRENDS_ROUTING_KEY


@pytest.mark.asyncio
async def test_the_same_event_does_not_publish_twice(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A persisting condition is not news.

    The scanner runs four times an hour; without this the channel would repeat
    the same volume spike every fifteen minutes until it faded.
    """
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    first = await service.publish(now=REGULAR)
    second = await service.publish(now=REGULAR + timedelta(minutes=15))

    assert first.messages_sent == 1
    assert second.messages_sent == 0
    assert second.events_suppressed >= 1
    assert len(backend.messages) == 1


@pytest.mark.asyncio
async def test_the_cooldown_expires_and_the_event_may_speak_again(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    await service.publish(now=REGULAR)
    # Four and a half hours later: past the cooldown and still inside the same
    # regular session, so the session gate is not what is being measured. The
    # evaluation is re-stored because a six-hour-old one would be too stale.
    later = REGULAR + timedelta(hours=4, minutes=30)
    await store_evaluation(seeded_session, symbol="NVDA", at=later, relative_volume=3.1)
    again = await service.publish(now=later)

    assert again.messages_sent == 1
    assert len(backend.messages) == 2


@pytest.mark.parametrize(
    ("moment", "label"),
    [(PREMARKET, "pre-market"), (AFTER_HOURS, "after-hours"), (WEEKEND, "weekend")],
)
@pytest.mark.asyncio
async def test_trends_are_silent_outside_the_regular_session(
    seeded_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    moment: datetime,
    label: str,
) -> None:
    """Extended hours are suppressed for a data reason, not a caution one.

    IEX prints thinly outside the session; a "volume spike" computed from a
    handful of trades measures the feed rather than the market.
    """
    await store_evaluation(seeded_session, symbol="NVDA", at=moment, relative_volume=9.9)
    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    run = await service.publish(now=moment)

    assert run.skipped_reason is not None, label
    assert backend.messages == []


@pytest.mark.asyncio
async def test_nothing_notable_sends_nothing(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """**No "no trends found" message.** Silence is the healthy state.

    MSFT is deliberate: the fixture loads bars for NVDA and AAPL only, so this
    instrument has quiet metrics *and* no price history to move. Using NVDA would
    have measured the mock provider's random walk rather than the policy.
    """
    await store_evaluation(
        seeded_session, symbol="MSFT", at=REGULAR, relative_volume=0.9, volatility=0.1
    )
    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    run = await service.publish(now=REGULAR)

    assert run.messages_sent == 0
    assert backend.messages == []


@pytest.mark.asyncio
async def test_stale_scan_output_is_not_announced(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An observation is a claim about *now*.

    If the scanner died this morning, "NVDA is up 4%" may have stopped being true
    hours ago, and repeating it would put tradabot's name on a stale fact.
    """
    await store_evaluation(
        seeded_session, symbol="NVDA", at=REGULAR - timedelta(hours=6), relative_volume=3.1
    )
    backend = CapturingBackend()
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    run = await service.publish(now=REGULAR)

    assert run.skipped_reason is not None
    assert backend.messages == []


@pytest.mark.asyncio
async def test_a_missing_trends_webhook_is_safe(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """No webhook means silence -- **not** a fallback into #market-signals.

    `market-trends` is deliberately not a feed key, so the backend returns no
    destination rather than routing descriptive text into a signals channel where
    it would read as advice.
    """
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    settings = make_settings(trends=False)
    notifier = DiscordWebhookNotifier(settings.discord, client=offline_client())

    assert notifier.webhook_for(_market_category(), TRENDS_ROUTING_KEY) is None

    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[notifier]),
    )
    run = await service.publish(now=REGULAR)

    assert run.messages_sent == 0
    assert run.skipped_reason is None  # it evaluated fine; there was just nowhere to send


@pytest.mark.asyncio
async def test_a_trends_delivery_failure_does_not_raise(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Discord must never be able to break a scheduled job.

    The scanner runs in a **separate process** from this one, so a failure here
    cannot reach it at all -- but the job itself must still exit cleanly rather
    than leaving launchd to retry a crash.
    """
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    backend = BrokenBackend(raises=True)
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )

    run = await service.publish(now=REGULAR)

    assert backend.attempts == 1
    assert run.messages_sent == 0
    assert not run.published


@pytest.mark.asyncio
async def test_a_failed_trend_message_does_not_start_the_cooldown(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Otherwise one dropped message silences a real observation for four hours."""
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    settings = make_settings()

    broken = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[BrokenBackend()]),
    )
    await broken.publish(now=REGULAR)

    backend = CapturingBackend()
    working = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[backend]),
    )
    retry = await working.publish(now=REGULAR + timedelta(minutes=15))

    assert retry.messages_sent == 1


@pytest.mark.asyncio
async def test_a_preview_sends_no_http_request(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """``evaluate`` is the read-only half, and the preview uses only that."""
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(
            settings, backends=[DiscordWebhookNotifier(settings.discord, client=offline_client())]
        ),
    )

    run = await service.evaluate(now=REGULAR)

    assert run.events_detected >= 1
    assert run.messages_sent == 0


@pytest.mark.asyncio
async def test_trend_state_persists_no_secret(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Nothing written to `notification_state` may resemble a credential."""
    await store_evaluation(seeded_session, symbol="NVDA", at=REGULAR, relative_volume=3.1)
    settings = make_settings()
    service = TrendsService(
        factory,
        settings=settings,
        notifications=NotificationService(settings, backends=[CapturingBackend()]),
    )
    await service.publish(now=REGULAR)

    async with factory() as session:
        rows = (await session.execute(_all_state())).scalars().all()

    assert rows
    for row in rows:
        assert "discord.com" not in row.key
        assert "discord.com" not in (row.phase or "")
        assert "tok-" not in row.key
        assert "tok-" not in (row.phase or "")


# ---------------------------------------------------------------------------
# Status: the persistent dashboard
# ---------------------------------------------------------------------------
async def record_runs(session: AsyncSession, *, now: datetime) -> None:
    """A recent sync and scan, so the dashboard has something honest to show."""
    runs = ScanRunRepository(session)
    for scope in (SCOPE_SYNC, SCOPE_SCAN):
        run = await runs.acquire_lease(scope=scope, lease_seconds=60, now=now)
        assert run is not None
        await runs.complete(run, metrics={"symbols_evaluated": 52})
    await session.commit()


@pytest.mark.asyncio
async def test_the_first_publication_creates_the_message(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord()
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )

    run = await service.publish(now=REGULAR)

    assert run.published
    assert run.created
    assert len(discord.posts) == 1
    assert "wait=true" in discord.posts[0]["url"]
    assert discord.patches == []


@pytest.mark.asyncio
async def test_the_second_publication_edits_the_same_message(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """**One dashboard, not a log.** This is the behaviour #status exists for."""
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord()
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )

    first = await service.publish(now=REGULAR)
    second = await service.publish(now=REGULAR + timedelta(minutes=20), force=True)

    assert len(discord.posts) == 1, "a second message was created instead of an edit"
    assert len(discord.patches) == 1
    assert first.message_id is not None
    assert second.message_id == first.message_id
    assert not second.created


@pytest.mark.asyncio
async def test_a_deleted_message_is_recreated(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A 404 on edit means the message is gone. Post a new one and store its id."""
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord(patch_status=404)
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )

    first = await service.publish(now=REGULAR)
    second = await service.publish(now=REGULAR + timedelta(minutes=20), force=True)

    assert len(discord.patches) == 1, "it should have tried the edit first"
    assert len(discord.posts) == 2, "and then recreated"
    assert second.created
    assert second.message_id != first.message_id

    async with factory() as session:
        stored = await NotificationRepository(session).dashboard_state()
    assert stored.message_id == second.message_id


@pytest.mark.asyncio
async def test_a_rotated_webhook_recreates_rather_than_failing(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A rotated webhook rejects the old message id the same way a deletion does."""
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord(patch_status=401)
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )
    async with factory() as session:
        await NotificationRepository(session).save_dashboard_state(
            DashboardState(message_id="9999", published_at=REGULAR - timedelta(hours=1))
        )
        await session.commit()

    run = await service.publish(now=REGULAR)

    assert run.published
    assert run.created
    assert len(discord.posts) == 1


@pytest.mark.asyncio
async def test_an_unchanged_dashboard_stays_quiet_until_the_heartbeat(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Otherwise the "edit, don't spam" design buys nothing.

    This is the test that would have caught the relative-age bug: rendering
    `Last sync: 3m ago` into the fingerprint made every tick look like a change.
    """
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord()
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )

    await service.publish(now=REGULAR)
    quiet = await service.publish(now=REGULAR + timedelta(minutes=5))
    beat = await service.publish(now=REGULAR + timedelta(minutes=16))

    assert not quiet.published
    assert quiet.reason == "unchanged"
    assert beat.published
    assert beat.reason == "heartbeat"
    assert len(discord.patches) == 1


@pytest.mark.asyncio
async def test_a_missing_status_webhook_is_safe(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """No destination means no message, and no scheduler failure."""
    await record_runs(seeded_session, now=REGULAR)
    settings = make_settings(status=False)
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=offline_client()),
    )

    run = await service.publish(now=REGULAR)

    assert not run.published
    assert run.error == "not delivered"


@pytest.mark.asyncio
async def test_a_status_delivery_failure_does_not_stop_operations(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Publishing returns a result; it never raises and never advances state.

    Leaving the stored state untouched is what makes the next tick retry instead
    of believing it already published.
    """
    await record_runs(seeded_session, now=REGULAR)

    async def explode(request: httpx.Request) -> httpx.Response:
        msg = "connection reset"
        raise httpx.ConnectError(msg)

    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(
            settings.discord, client=httpx.AsyncClient(transport=httpx.MockTransport(explode))
        ),
    )

    run = await service.publish(now=REGULAR)

    assert not run.published
    async with factory() as session:
        stored = await NotificationRepository(session).dashboard_state()
    assert stored.message_id is None


@pytest.mark.asyncio
async def test_a_status_preview_sends_no_http_request(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    await record_runs(seeded_session, now=REGULAR)
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=offline_client()),
    )

    fields = await service.render(now=REGULAR)

    assert fields["Server"] == ONLINE
    assert "Checked" in fields


@pytest.mark.asyncio
async def test_the_dashboard_reports_the_full_picture(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Part G: every section can actually be populated from live state."""
    await record_runs(seeded_session, now=REGULAR)
    settings = make_settings()
    service = StatusService(factory, settings=settings, notifier=None)

    fields = await service.render(now=REGULAR)

    for expected in ("Server", "Environment", "Session", "Provider", "Symbols"):
        assert expected in fields
    assert "Last sync" in fields
    assert "Last scan" in fields
    assert "Database" in fields
    assert "Scheduler" in fields
    assert "Discord" in fields
    # Candles and evaluations are read, not fabricated: the seeded database has
    # bars but no stored evaluations, so one is a number and the other is N/A.
    assert fields["Candles"] != "N/A"
    assert fields["Evaluations"] == "N/A"


@pytest.mark.asyncio
async def test_no_webhook_url_reaches_the_persisted_dashboard_state(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """**The security property.** The message id is stored; the credential is not."""
    await record_runs(seeded_session, now=REGULAR)
    discord = FakeDiscord()
    settings = make_settings()
    service = StatusService(
        factory,
        settings=settings,
        notifier=DiscordWebhookNotifier(settings.discord, client=discord.client()),
    )
    await service.publish(now=REGULAR)

    async with factory() as session:
        rows = (await session.execute(_all_state())).scalars().all()

    stored = {row.key: row.phase for row in rows}
    assert DASHBOARD_KEY in stored
    assert stored[DASHBOARD_KEY].isdigit()
    for row in rows:
        assert "https://" not in (row.phase or "")
        assert "tok-" not in (row.phase or "")
    assert all(row.scope != DASHBOARD_SCOPE or "webhook" not in row.key for row in rows)


# ---------------------------------------------------------------------------
# Health semantics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_closed_market_does_not_make_market_data_look_broken(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """**The Friday-close problem.**

    A gap that would read as DEGRADED mid-session is ordinary at the weekend, so
    the freshness window widens rather than the check disappearing.
    """
    saturday = WEEKEND
    await record_runs(seeded_session, now=saturday)
    settings = make_settings()
    service = StatusService(factory, settings=settings, notifier=None)

    # An hour after the last run. Mid-session that is past both windows and
    # would read DEGRADED; at the weekend it is inside the widened ones, because
    # a laptop that dozed for an hour on a Saturday is not an incident.
    fields = await service.render(now=saturday + timedelta(minutes=60))

    assert fields["Server"] == ONLINE


@pytest.mark.asyncio
async def test_a_stale_scan_during_the_session_is_degraded(
    seeded_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The same tolerance does **not** apply while the market is open.

    Thirty minutes puts the five-minute sync well past its window while the
    fifteen-minute scan is still inside its own, which is exactly the partial
    failure DEGRADED exists to name.
    """
    await record_runs(seeded_session, now=REGULAR)
    settings = make_settings()
    service = StatusService(factory, settings=settings, notifier=None)

    fields = await service.render(now=REGULAR + timedelta(minutes=30))

    assert fields["Server"] == DEGRADED


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def test_the_scheduler_gained_exactly_two_jobs() -> None:
    jobs = {job.name: job for job in scheduled_jobs()}

    assert set(jobs) == {"sync", "scan", "overview", "summary", "trends", "status"}
    assert jobs["sync"].interval_seconds == 5 * 60
    assert jobs["scan"].interval_seconds == 15 * 60
    assert jobs["trends"].interval_seconds == 15 * 60
    assert jobs["status"].interval_seconds == 15 * 60


def test_the_new_jobs_invoke_real_cli_commands() -> None:
    """A plist naming a command that does not exist fails silently in launchd."""
    from app.cli import _build_parser

    parser = _build_parser()
    for job in scheduled_jobs():
        if job.name in {"trends", "status"}:
            assert parser.parse_args(list(job.args)) is not None


def test_no_plist_contains_a_secret(tmp_path: Any) -> None:
    """Credentials stay in `.env`; ~/Library/LaunchAgents is world-readable."""
    from pathlib import Path

    from app.ops.launchd import render_plist

    for job in scheduled_jobs():
        rendered = render_plist(
            job, project_root=tmp_path, python_path=Path("/usr/bin/python3"), log_dir=tmp_path
        ).decode()
        assert "webhook" not in rendered.lower()
        assert "api_key" not in rendered.lower()
        assert "EnvironmentVariables" not in rendered


def _market_category() -> Any:
    from app.core.events import EventCategory

    return EventCategory.MARKET


def _all_state() -> Any:
    from sqlalchemy import select

    return select(NotificationState)
