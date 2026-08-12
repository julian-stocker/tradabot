"""The daily-summary builder and the notification CLI commands."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import main
from app.notifications.summary import MIN_TRADES_FOR_RATE, build_daily_summary
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration


async def test_the_default_report_covers_only_the_user_facing_portfolios(
    session: AsyncSession,
) -> None:
    """**The nine legacy research profiles stay out of Discord.**

    They remain enabled and keep trading -- they are simply not in the report.
    Twelve portfolio blocks on a phone is a wall nobody reads, and only three of
    them are the user's.
    """
    from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles

    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await profiles.upsert_many(build_personal_profiles())
    await session.flush()
    enabled = await profiles.list_profiles(enabled_only=True)

    payload = await build_daily_summary(session)

    reported = {p["profile"] for p in payload["portfolios"]}
    assert reported == set(PORTFOLIO_KEYS)
    assert len(enabled) > len(reported), "the legacy profiles must still be enabled"


async def test_research_can_still_see_every_enabled_profile(session: AsyncSession) -> None:
    """Hidden from the report, not deleted: passing None restores the full list."""
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    enabled = await profiles.list_profiles(enabled_only=True)

    payload = await build_daily_summary(session, profile_keys=None)

    assert {p["profile"] for p in payload["portfolios"]} == {p.name for p in enabled}


async def test_an_untraded_portfolio_omits_its_win_rate(session: AsyncSession) -> None:
    """A win rate over zero trades is not a number worth putting on a channel."""
    await SimulationProfileRepository(session).upsert_many(build_default_profiles())
    await session.flush()

    payload = await build_daily_summary(session)

    for portfolio in payload["portfolios"]:
        assert "win_rate" not in portfolio, f"needs {MIN_TRADES_FOR_RATE} trades first"


async def test_an_unvalued_portfolio_omits_its_drawdown(session: AsyncSession) -> None:
    """Drawdown needs an equity curve; one point makes it a definitional zero."""
    await SimulationProfileRepository(session).upsert_many(build_default_profiles())
    await session.flush()

    payload = await build_daily_summary(session)

    for portfolio in payload["portfolios"]:
        assert "max_drawdown" not in portfolio


async def test_a_summary_reports_equity_for_every_portfolio(session: AsyncSession) -> None:
    await SimulationProfileRepository(session).upsert_many(build_default_profiles())
    await session.flush()

    payload = await build_daily_summary(session)

    for portfolio in payload["portfolios"]:
        assert portfolio["equity"] > 0, "a seeded portfolio starts with its capital"


async def test_an_empty_database_produces_an_empty_summary(session: AsyncSession) -> None:
    payload = await build_daily_summary(session)

    assert payload["portfolios"] == []
    assert payload["exits"] == 0


# ---------------------------------------------------------------------------
# CLI (Parts L and O)
# ---------------------------------------------------------------------------
def test_the_test_command_reports_when_nothing_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """Disabled notifications exit non-zero with guidance, not a stack trace."""
    monkeypatch.setenv("TRADABOT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("TRADABOT_NOTIFICATIONS__CONSOLE", "false")
    monkeypatch.setenv("TRADABOT_DISCORD__ENABLED", "false")
    monkeypatch.setenv("TRADABOT_LOG_LEVEL", "CRITICAL")
    from app.core.config import get_settings

    get_settings.cache_clear()

    exit_code = main(["notifications", "test"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "disabled" in output.lower()
    assert "TRADABOT_DISCORD__ENABLED" in output


def test_the_test_command_sends_through_the_console_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """A local test run needs no Discord server and no credentials."""
    database = f"sqlite+aiosqlite:///{tmp_path}/cli-console.db"
    monkeypatch.setenv("TRADABOT_DATABASE_URL", database)
    monkeypatch.setenv("TRADABOT_NOTIFICATIONS__CONSOLE", "true")
    monkeypatch.setenv("TRADABOT_LOG_LEVEL", "CRITICAL")
    from app.core.config import get_settings

    get_settings.cache_clear()

    assert main(["create-tables"]) == 0
    exit_code = main(["notifications", "test", "--category", "market"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "console" in output
    assert "market" in output


def test_the_cli_never_prints_a_webhook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """`notifications status` reports channel names, never their URLs."""
    hook = "https://discord.com/api/webhooks/999/cli-secret-token"
    database = f"sqlite+aiosqlite:///{tmp_path}/cli-status.db"
    monkeypatch.setenv("TRADABOT_DATABASE_URL", database)
    monkeypatch.setenv("TRADABOT_DISCORD__ENABLED", "true")
    monkeypatch.setenv("TRADABOT_DISCORD__MARKET_WEBHOOK", hook)
    monkeypatch.setenv("TRADABOT_LOG_LEVEL", "CRITICAL")
    from app.core.config import get_settings

    get_settings.cache_clear()

    assert main(["create-tables"]) == 0
    assert main(["notifications", "status"]) == 0

    output = capsys.readouterr().out
    assert hook not in output
    assert "cli-secret-token" not in output
    assert "market" in output, "the channel is named"


def test_the_daily_summary_command_runs_without_notifications(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """Part L: a plain callable, invokable by cron, that works with delivery off."""
    database = f"sqlite+aiosqlite:///{tmp_path}/cli-summary.db"
    monkeypatch.setenv("TRADABOT_DATABASE_URL", database)
    monkeypatch.setenv("TRADABOT_NOTIFICATIONS__CONSOLE", "false")
    monkeypatch.setenv("TRADABOT_DISCORD__ENABLED", "false")
    monkeypatch.setenv("TRADABOT_LOG_LEVEL", "CRITICAL")
    from app.core.config import get_settings

    get_settings.cache_clear()

    assert main(["create-tables"]) == 0
    assert main(["seed-profiles"]) == 0
    capsys.readouterr()

    exit_code = main(["notifications", "daily-summary"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "portfolios summarised" in output
    assert "disabled" in output, "it says plainly that nothing was delivered"
