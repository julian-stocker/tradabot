"""The operations check, status, daily-summary gating and test routing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import DiscordSettings, Environment, ScannerSettings, Settings
from app.core.events import Event, EventCategory
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.service import NotificationService
from app.notifications.summary import build_daily_summary, daily_summary_already_sent
from app.ops.check import FAIL, OK, WARN, operational_status, run_checks
from app.ownership.service import ensure_local_ownership
from app.scanner.repository import SCOPE_SCAN, ScanRunRepository
from app.scanner.seed import seed_watchlist
from app.scanner.universe import UniverseEntry
from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration

NOW = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)
HOOK = "https://discord.com/api/webhooks/{}/tok-{}"


class CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(backend=self.name, delivered=True)


def full_discord() -> DiscordSettings:
    return DiscordSettings(
        enabled=True,
        market_webhook=SecretStr(HOOK.format(1, "market")),
        performance_webhook=SecretStr(HOOK.format(2, "perf")),
        system_webhook=SecretStr(HOOK.format(3, "sys")),
        paper_100_webhook=SecretStr(HOOK.format(4, "p100")),
        paper_1000_webhook=SecretStr(HOOK.format(5, "p1000")),
        paper_10000_webhook=SecretStr(HOOK.format(6, "p10000")),
    )


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "env": Environment.TEST,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "log_level": "WARNING",
        "scanner": ScannerSettings(require_regular_session=False),
        "discord": full_discord(),
    }
    return Settings(**(defaults | overrides))  # type: ignore[arg-type]


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


async def seed_everything(session: AsyncSession, settings: Settings, provider: object) -> None:
    await SimulationProfileRepository(session).upsert_many(build_personal_profiles())
    await session.flush()
    await seed_watchlist(
        session,
        provider,
        entries=[UniverseEntry("NVDA", "semiconductors")],  # type: ignore[arg-type]
    )
    await ensure_local_ownership(session, settings)
    await session.flush()


def status_of(report: object, name: str) -> str:
    return next(r.status for r in report.results if r.name == name)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ops check
# ---------------------------------------------------------------------------
async def test_a_fully_configured_installation_passes(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await seed_everything(session, settings, provider)

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert report.ok, [r.render() for r in report.failures]
    for key in PORTFOLIO_KEYS:
        assert status_of(report, f"portfolio {key}") == OK


async def test_a_missing_portfolio_webhook_fails_and_names_it(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    """'routing is broken' sends an operator hunting; naming the channel does not."""
    discord = full_discord().model_copy(
        update={"paper_1000_webhook": SecretStr(""), "portfolio_webhooks": {}}
    )
    settings = make_settings(market_data_provider="mock", discord=discord)
    await seed_everything(session, settings, provider)

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    failures = " ".join(r.detail for r in report.failures)
    assert not report.ok
    assert "PAPER_1000_WEBHOOK" in failures


async def test_a_missing_portfolio_profile_fails(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await seed_watchlist(
        session,
        provider,
        entries=[UniverseEntry("NVDA", "semiconductors")],  # type: ignore[arg-type]
    )

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert not report.ok
    assert status_of(report, "portfolio paper-100") == FAIL


async def test_an_empty_watchlist_fails(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await SimulationProfileRepository(session).upsert_many(build_personal_profiles())
    await session.flush()

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert status_of(report, "watchlist") == FAIL


async def test_alpaca_selected_without_credentials_fails(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="alpaca")
    await seed_everything(session, settings, provider)

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert status_of(report, "market data") == FAIL


async def test_the_mock_provider_warns_rather_than_fails(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await seed_everything(session, settings, provider)

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert status_of(report, "market data") == WARN


async def test_the_check_prints_no_credential(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await seed_everything(session, settings, provider)

    rendered = (
        await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")
    ).render()

    assert "discord.com" not in rendered
    assert "tok-" not in rendered


async def test_the_check_validates_scheduler_templates(
    session: AsyncSession, provider: object, tmp_path: Path
) -> None:
    settings = make_settings(market_data_provider="mock")
    await seed_everything(session, settings, provider)

    report = await run_checks(session, settings, project_root=Path.cwd(), log_dir=tmp_path / "logs")

    assert status_of(report, "scheduler") in {OK, WARN}
    assert any("12 job templates" in r.detail for r in report.results)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
async def test_status_reports_every_portfolio(
    session: AsyncSession, provider: object, settings: Settings
) -> None:
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)

    status = await operational_status(session, configured)

    assert [p.key for p in status.portfolios] == list(PORTFOLIO_KEYS)
    assert [p.equity for p in status.portfolios] == [100.0, 1000.0, 10000.0]


async def test_status_reports_the_last_scan(session: AsyncSession, provider: object) -> None:
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)
    run = await ScanRunRepository(session).acquire_lease(
        scope=SCOPE_SCAN, lease_seconds=900, now=NOW
    )
    assert run is not None
    await ScanRunRepository(session).complete(run, metrics={"symbols_evaluated": 3})

    status = await operational_status(session, configured)

    assert status.last_scan == NOW
    assert status.last_scan_status == "completed"


async def test_status_contains_no_secret(session: AsyncSession, provider: object) -> None:
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)

    status = await operational_status(session, configured)

    assert "discord.com" not in str(status)
    assert "tok-" not in str(status)


# ---------------------------------------------------------------------------
# Daily summary gating
# ---------------------------------------------------------------------------
async def test_the_daily_summary_is_not_repeated_within_a_session(
    session: AsyncSession, provider: object, factory: async_sessionmaker
) -> None:
    """A scheduler firing hourly must not produce hourly reports."""
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)
    await session.commit()

    assert not await daily_summary_already_sent(session, configured)

    service = NotificationService(
        configured, backends=[CapturingBackend()], session_factory=factory
    )
    async with factory() as write:
        payload = await build_daily_summary(write)
    await service.publish(Event.daily_summary(payload))

    async with factory() as check:
        assert await daily_summary_already_sent(check, configured)


async def test_the_daily_summary_covers_all_three_portfolios(
    session: AsyncSession, provider: object
) -> None:
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)

    payload = await build_daily_summary(session)

    names = {p["profile"] for p in payload["portfolios"]}
    assert names == set(PORTFOLIO_KEYS)


# ---------------------------------------------------------------------------
# Test routing
# ---------------------------------------------------------------------------
async def test_the_test_command_covers_all_six_destinations(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """The six real channels, and nothing that has no destination.

    The generic PAPER_TRADE category is skipped when portfolio channels exist:
    paper events route by portfolio, so attempting it would report a failure for
    a channel that is not supposed to receive anything.
    """
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    sent = await service.send_test(routing_keys=PORTFOLIO_KEYS)

    assert set(sent) == {
        "paper-100",
        "paper-1000",
        "paper-10000",
        "market",
        "performance",
        "system",
    }
    assert "paper_trade" not in sent, "no message to a channel with no destination"
    routed = {m.routing_key for m in backend.messages if m.routing_key}
    assert routed == set(PORTFOLIO_KEYS)


async def test_without_portfolio_keys_the_generic_category_is_still_tested(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    """An installation still using the legacy single channel keeps working."""
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    sent = await service.send_test()

    assert set(sent) == {c.value for c in EventCategory}


async def test_test_messages_are_labelled_and_carry_no_secret(
    session: AsyncSession, factory: async_sessionmaker
) -> None:
    backend = CapturingBackend()
    service = NotificationService(make_settings(), backends=[backend], session_factory=factory)

    await service.send_test(routing_keys=PORTFOLIO_KEYS)

    for rendered in (m.rendered(4000) for m in backend.messages):
        assert "TEST" in rendered
        assert "discord.com" not in rendered
        assert "tok-" not in rendered


async def test_testing_routing_creates_no_paper_trade(
    session: AsyncSession, factory: async_sessionmaker, provider: object
) -> None:
    """A fabricated position to prove a webhook works would be a much worse trade
    than a message that says TEST."""
    configured = make_settings(market_data_provider="mock")
    await seed_everything(session, configured, provider)
    await session.commit()

    service = NotificationService(
        configured, backends=[CapturingBackend()], session_factory=factory
    )
    await service.send_test(routing_keys=PORTFOLIO_KEYS)

    async with factory() as check:
        status = await operational_status(check, configured)
    assert all(p.open_positions == 0 for p in status.portfolios)
    assert all(p.closed_trades == 0 for p in status.portfolios)
