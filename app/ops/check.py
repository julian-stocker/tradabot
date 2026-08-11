"""Pre-flight validation and operational status.

``ops check`` answers "is this installation ready to run unattended?" before the
scheduler is installed, rather than after a silent week of nothing happening.

**No check prints a credential.** Every one reports *whether* something is
configured and, when it is not, which variable to set — never a value, a prefix
or a length.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import SimulationProfile
from app.market_data.calendars import get_trading_calendar
from app.notifications.repository import NotificationRepository
from app.ops.launchd import launchctl_available, scheduled_jobs
from app.ownership.service import OwnershipService
from app.paper.repository import PaperTradingRepository
from app.scanner.repository import (
    SCOPE_SCAN,
    SCOPE_SYNC,
    ScanRunRepository,
    WatchlistRepository,
)
from app.scanner.sessions import describe_phase, is_after_close, session_phase
from app.simulation.portfolios import PORTFOLIO_KEYS

logger = get_logger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One validation outcome."""

    name: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status != FAIL

    def render(self) -> str:
        mark = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}[self.status]
        return f"  [{mark}] {self.name:<28} {self.detail}"


@dataclass
class CheckReport:
    """Every check, and whether the installation is ready."""

    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.results.append(CheckResult(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == WARN]

    def render(self) -> str:
        lines = [result.render() for result in self.results]
        lines.append("")
        lines.append(
            f"  {len(self.results)} checks: "
            f"{len(self.results) - len(self.failures) - len(self.warnings)} pass, "
            f"{len(self.warnings)} warn, {len(self.failures)} fail"
        )
        return "\n".join(lines)


async def run_checks(
    session: AsyncSession, settings: Settings, *, project_root: Path, log_dir: Path
) -> CheckReport:
    """Validate configuration, database, routing and scheduler templates."""
    report = CheckReport()

    report.add("settings", OK, f"parsed; environment={settings.env.value}")
    await _check_database(session, report)
    await _check_migrations(session, report)
    _check_market_data(settings, report)
    _check_discord(settings, report)
    await _check_portfolio_routing(session, settings, report)
    await _check_watchlist(session, report)
    _check_directories(project_root, log_dir, report)
    _check_scheduler(settings, report)
    await _check_ownership(session, report)

    logger.info("ops check complete", ok=report.ok, failures=len(report.failures))
    return report


async def _check_database(session: AsyncSession, report: CheckReport) -> None:
    try:
        await session.execute(text("SELECT 1"))
    # A check reports problems; it never becomes one.
    except Exception as exc:
        report.add("database", FAIL, f"unreachable: {type(exc).__name__}")
        return
    report.add("database", OK, "reachable")


async def _check_migrations(session: AsyncSession, report: CheckReport) -> None:
    """Whether the schema is at a known revision.

    Reports the stamped revision rather than comparing against the migration
    directory: `alembic check` does that properly, and duplicating it here would
    give two answers that could disagree.
    """
    try:
        row = (await session.execute(text("SELECT version_num FROM alembic_version"))).first()
    # An unmigrated database is a normal state, not an error.
    except Exception:
        report.add(
            "migrations",
            WARN,
            "no alembic_version table; run `make migrate` (or tables were created directly)",
        )
        return
    revision = row[0] if row else "(none)"
    report.add("migrations", OK, f"schema at revision {revision}")


def _check_market_data(settings: Settings, report: CheckReport) -> None:
    provider = settings.market_data_provider
    if provider == "mock":
        report.add("market data", WARN, "provider is `mock`; no real data will be fetched")
        return
    if not settings.alpaca.is_configured:
        report.add(
            "market data",
            FAIL,
            "alpaca selected but credentials missing; set TRADABOT_ALPACA__API_KEY "
            "and TRADABOT_ALPACA__API_SECRET (or __SECRET_KEY)",
        )
        return
    report.add("market data", OK, f"provider={provider} feed={settings.alpaca.feed}")


def _check_discord(settings: Settings, report: CheckReport) -> None:
    if not settings.discord.enabled:
        report.add("discord", WARN, "disabled; scanner runs but nothing is delivered")
        return
    if not settings.discord.is_configured:
        report.add("discord", FAIL, "enabled but no webhook configured")
        return
    channels = sorted(settings.discord.configured_categories)
    report.add("discord", OK, f"{len(channels)} destinations: {', '.join(channels)}")


async def _check_portfolio_routing(
    session: AsyncSession, settings: Settings, report: CheckReport
) -> None:
    """Every personal portfolio must exist and have somewhere to post.

    Checked per portfolio rather than in aggregate: "routing is broken" sends an
    operator looking through three channels, "paper-1000 has no webhook" does not.
    """
    rows = (
        (
            await session.execute(
                select(SimulationProfile).where(
                    SimulationProfile.notification_channel.in_(PORTFOLIO_KEYS)
                )
            )
        )
        .scalars()
        .all()
    )
    configured = {row.notification_channel for row in rows}

    for key in PORTFOLIO_KEYS:
        if key not in configured:
            report.add(
                f"portfolio {key}",
                FAIL,
                "no simulation profile; run `tradabot portfolios seed`",
            )
            continue
        if not settings.discord.enabled:
            report.add(f"portfolio {key}", WARN, "profile exists; discord disabled")
            continue
        if settings.discord.webhook_for_portfolio(key) is None:
            report.add(
                f"portfolio {key}",
                FAIL,
                f"no destination; set TRADABOT_DISCORD__{key.replace('-', '_').upper()}_WEBHOOK",
            )
            continue
        report.add(f"portfolio {key}", OK, "profile and destination configured")


async def _check_watchlist(session: AsyncSession, report: CheckReport) -> None:
    count = await WatchlistRepository(session).count()
    if count == 0:
        report.add("watchlist", FAIL, "empty; run `tradabot watchlist seed`")
        return
    report.add("watchlist", OK, f"{count} symbols enabled")


def _check_directories(project_root: Path, log_dir: Path, report: CheckReport) -> None:
    if not (project_root / "app").is_dir():
        report.add("project root", FAIL, f"{project_root} does not look like the project")
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".write-probe"
        probe.write_text("")
        probe.unlink()
    except OSError as exc:
        report.add("log directory", FAIL, f"{log_dir} not writable: {exc.strerror}")
        return
    report.add("directories", OK, f"root and log directory writable ({log_dir})")


def _check_scheduler(settings: Settings, report: CheckReport) -> None:
    jobs = scheduled_jobs(
        scan_minutes=settings.scanner.scan_interval_minutes,
        sync_minutes=settings.scanner.market_sync_interval_minutes,
        overview_minutes=settings.scanner.overview_interval_minutes,
    )
    if not launchctl_available():
        report.add(
            "scheduler",
            WARN,
            f"{len(jobs)} templates valid; launchctl not found (not macOS)",
        )
        return
    report.add("scheduler", OK, f"{len(jobs)} job templates valid; launchctl present")


async def _check_ownership(session: AsyncSession, report: CheckReport) -> None:
    owner = await OwnershipService(session).local_owner(create=False)
    if owner is None:
        report.add("ownership", WARN, "no local owner yet; created on first seed")
        return
    profiles = await OwnershipService(session).profiles_for(owner.id)
    report.add("ownership", OK, f"owner #{owner.id} owns {len(profiles)} portfolios")


# ---------------------------------------------------------------------------
# Operational status
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PortfolioStatus:
    key: str
    equity: float
    open_positions: int
    closed_trades: int


@dataclass
class OperationalStatus:
    """What has run, when, and where the portfolios stand."""

    checked_at: datetime
    session_phase: str
    last_sync: datetime | None = None
    last_scan: datetime | None = None
    last_scan_status: str | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    last_notification_success: datetime | None = None
    last_notification_failure: datetime | None = None
    portfolios: list[PortfolioStatus] = field(default_factory=list)
    session_closed: bool = False


async def operational_status(session: AsyncSession, settings: Settings) -> OperationalStatus:
    """Assemble the operational picture. Contains no secret."""
    now = utc_now()
    calendar = get_trading_calendar(settings.market_data.default_exchange)

    runs = ScanRunRepository(session)
    scan = await runs.latest(scope=SCOPE_SCAN)
    sync = await runs.latest(scope=SCOPE_SYNC)
    success = await runs.latest_successful(scope=SCOPE_SCAN)
    last_ok, last_fail = await NotificationRepository(session).last_outcome()

    portfolios: list[PortfolioStatus] = []
    paper = PaperTradingRepository(session)
    rows = (
        (
            await session.execute(
                select(SimulationProfile)
                .where(SimulationProfile.notification_channel.in_(PORTFOLIO_KEYS))
                .order_by(SimulationProfile.initial_capital)
            )
        )
        .scalars()
        .all()
    )

    for row in rows:
        try:
            portfolio = await paper.get_portfolio(row.id)
            equity = float(portfolio.cash)
        # A portfolio with no trades has no persisted row yet.
        except Exception:
            equity = float(row.initial_capital)
        open_positions = await paper.open_positions(row.id)
        trades = await paper.trades(row.id)
        portfolios.append(
            PortfolioStatus(
                key=row.notification_channel or row.name,
                equity=equity,
                open_positions=len(open_positions),
                closed_trades=len(trades),
            )
        )

    return OperationalStatus(
        checked_at=now,
        session_phase=describe_phase(session_phase(calendar, now), now),
        last_sync=sync.started_at if sync else None,
        last_scan=scan.started_at if scan else None,
        last_scan_status=scan.status if scan else None,
        last_success=success.started_at if success else None,
        last_error=scan.error if scan and scan.error else None,
        last_notification_success=last_ok,
        last_notification_failure=last_fail,
        portfolios=portfolios,
        session_closed=is_after_close(calendar, now),
    )
