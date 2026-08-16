"""macOS LaunchAgent generation.

Produces plist files for the four scheduled jobs. **Generates only** -- nothing
here loads, starts or installs anything, and no test does either. Installation is
an explicit, reversible operator action (``make ops-install`` /
``make ops-uninstall``).

Why launchd rather than cron
----------------------------
On a laptop, the difference is decisive: launchd runs a job that was missed while
the machine slept, cron silently skips it. A scanner that goes quiet from Friday
evening to Monday morning and never mentions it is worse than one that catches up
noisily.

**No secret appears in a plist.** The jobs read `.env` from the working
directory, exactly as a manual invocation does. Putting credentials in a plist
would place them in `~/Library/LaunchAgents`, a directory that is world-readable
by default, backed up by Time Machine and synced by some tools.
"""

from __future__ import annotations

import plistlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.core.logging import get_logger

logger = get_logger(__name__)

LABEL_PREFIX: Final = "com.tradabot"
LAUNCH_AGENTS_DIR: Final = Path.home() / "Library" / "LaunchAgents"

# Bounded so a scheduled job cannot fill a disk. launchd does not rotate, so the
# ops layer truncates on install and the CLI keeps each line short.
MAX_LOG_BYTES: Final = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One launchd job."""

    name: str
    args: tuple[str, ...]
    interval_seconds: int
    description: str
    long_running: bool = False
    """A daemon rather than a periodic job.

    launchd expresses the two differently: a periodic job gets ``StartInterval``
    and is not run at load, while a daemon is started at login and restarted
    when it exits. Giving a long-running process a ``StartInterval`` would ask
    launchd to start a *second* copy every interval while the first is still
    running."""

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.name}"

    @property
    def plist_name(self) -> str:
        return f"{self.label}.plist"


def scheduled_jobs(
    *,
    scan_minutes: int = 15,
    sync_minutes: int = 5,
    overview_minutes: int = 60,
    summary_minutes: int = 60,
    trends_minutes: int = 15,
    status_minutes: int = 15,
    options_minutes: int = 20,
    market_monitor_minutes: int = 30,
    company_monitor_minutes: int = 60 * 24,
    portfolio_monitor_minutes: int = 60 * 4,
    weekly_newsletter_minutes: int = 60 * 6,
    heartbeat_minutes: int = 5,
) -> tuple[ScheduledJob, ...]:
    """The seven jobs, at the configured cadence.

    The daily summary runs hourly and decides for itself whether the session has
    closed -- see :mod:`app.ops.status`. Pinning it to a wall-clock time would
    bake in a timezone and break twice a year on US daylight-saving transitions,
    which is exactly the kind of bug that shows up as a missing report rather
    than an error.

    Why trends and status are separate jobs
    ---------------------------------------
    Neither fetches market data; both read what sync and scan already persisted.
    They are separate **processes** rather than tails appended to the scan cycle
    because process isolation is the only kind that survives a hang. A ``try``
    block around a Discord call contains an exception; it does not stop a stalled
    HTTP request from holding the scan lease while the next scan is due.

    Status additionally *cannot* live inside the scanner: it must keep publishing
    when the market is closed, which is exactly when a scan has nothing to do.
    """
    return (
        ScheduledJob(
            name="sync",
            args=("scanner", "sync"),
            interval_seconds=sync_minutes * 60,
            description="Incremental market-data synchronisation",
        ),
        ScheduledJob(
            name="scan",
            args=("scanner", "run-once"),
            interval_seconds=scan_minutes * 60,
            description="Full scan cycle",
        ),
        ScheduledJob(
            name="overview",
            args=("scanner", "overview"),
            interval_seconds=overview_minutes * 60,
            description="Ranked market overview",
        ),
        ScheduledJob(
            name="summary",
            args=("ops", "daily-summary-if-due"),
            interval_seconds=summary_minutes * 60,
            description="Daily report, session-aware and sent at most once a day",
        ),
        ScheduledJob(
            name="trends",
            args=("scanner", "trends"),
            interval_seconds=trends_minutes * 60,
            description="Descriptive market activity from stored evaluations (no fetch)",
        ),
        ScheduledJob(
            name="options",
            args=("options", "capture"),
            interval_seconds=options_minutes * 60,
            description=(
                "Capture one point-in-time option surface per regular session. Runs on a "
                "short interval and decides for itself whether the session is open, whether "
                "the capture window is current, and whether today is already done -- so a "
                "retry or a slept machine converges on exactly one snapshot per symbol per "
                "day. Option chains cannot be backfilled, which is why this exists."
            ),
        ),
        ScheduledJob(
            name="status",
            args=("ops", "status-publish"),
            interval_seconds=status_minutes * 60,
            description="Status dashboard heartbeat; edits one message, market open or shut",
        ),
        # ---- presentation only. None of these can reach a broker. ----------
        ScheduledJob(
            name="monitor-market",
            args=("publish", "events"),
            interval_seconds=market_monitor_minutes * 60,
            description=(
                "Detect and publish material market changes. Price-driven, so it "
                "runs through the session; the monitor decides whether anything is "
                "worth sending and most passes send nothing."
            ),
        ),
        ScheduledJob(
            name="monitor-companies",
            args=("publish", "events", "--companies"),
            interval_seconds=company_monitor_minutes * 60,
            description=(
                "Fundamentals, filings and valuation bands. Daily is sufficient: "
                "these change on filing days, and a company pass runs the Advisor "
                "once per watched symbol."
            ),
        ),
        ScheduledJob(
            name="monitor-portfolio",
            args=("publish", "portfolio"),
            interval_seconds=portfolio_monitor_minutes * 60,
            description=(
                "Read-only paper account analysis, each slot to its own channel. "
                "Reads the broker; never writes to it."
            ),
        ),
        ScheduledJob(
            name="heartbeat",
            args=("heartbeat",),
            interval_seconds=heartbeat_minutes * 60,
            description=(
                "Ping the off-host watchdog. The only job whose absence is itself "
                "the signal: nothing here can report that this machine stopped, so "
                "something elsewhere infers it from the silence."
            ),
        ),
        ScheduledJob(
            name="weekly-newsletter",
            args=("publish", "weekly", "--if-due"),
            interval_seconds=weekly_newsletter_minutes * 60,
            description=(
                "Weekly market intelligence. Runs often and decides for itself "
                "whether the week is due, so a slept machine still publishes once "
                "rather than skipping the week entirely."
            ),
        ),
    )


def build_plist(
    job: ScheduledJob, *, project_root: Path, python_path: Path, log_dir: Path
) -> dict[str, object]:
    """The plist contents for one job.

    ``WorkingDirectory`` is the project root so the job finds `.env` and a
    relative SQLite path resolves to the same database a manual run uses --
    otherwise a scheduled job quietly creates a *second* database in whatever
    directory launchd happened to start in, and the two diverge invisibly.
    """
    schedule: dict[str, object] = (
        # Restarted when it exits, with a throttle so a crash loop backs off
        # instead of spinning. ThrottleInterval is launchd's own minimum gap
        # between respawns.
        {"KeepAlive": True, "RunAtLoad": True, "ThrottleInterval": job.interval_seconds}
        if job.long_running
        else {"StartInterval": job.interval_seconds, "RunAtLoad": False}
    )
    return {
        "Label": job.label,
        "WorkingDirectory": str(project_root),
        "ProgramArguments": [str(python_path), "-m", "app.cli", *job.args],
        **schedule,
        "StandardOutPath": str(log_dir / f"{job.name}.log"),
        "StandardErrorPath": str(log_dir / f"{job.name}.err"),
        "ProcessType": "Background",
        # No EnvironmentVariables key: credentials stay in `.env`, read from the
        # working directory. A plist is not a place for secrets.
    }


def render_plist(
    job: ScheduledJob, *, project_root: Path, python_path: Path, log_dir: Path
) -> bytes:
    """One job's plist, as bytes ready to write."""
    return plistlib.dumps(
        build_plist(job, project_root=project_root, python_path=python_path, log_dir=log_dir)
    )


def write_plists(
    jobs: tuple[ScheduledJob, ...],
    *,
    project_root: Path,
    python_path: Path,
    log_dir: Path,
    target_dir: Path,
) -> list[Path]:
    """Write plists to ``target_dir``. Creates nothing else and loads nothing."""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for job in jobs:
        path = target_dir / job.plist_name
        path.write_bytes(
            render_plist(job, project_root=project_root, python_path=python_path, log_dir=log_dir)
        )
        written.append(path)
    logger.info("wrote launchd plists", count=len(written), directory=str(target_dir))
    return written


def install_commands(jobs: tuple[ScheduledJob, ...], *, target_dir: Path) -> list[str]:
    """The exact `launchctl` commands to activate the jobs.

    Printed rather than executed. Loading a LaunchAgent starts running the user's
    machine on a schedule, and that should be a deliberate keystroke, not a side
    effect of a build step.
    """
    return [f"launchctl load -w {target_dir / job.plist_name}" for job in jobs]


def uninstall_commands(jobs: tuple[ScheduledJob, ...], *, target_dir: Path) -> list[str]:
    return [f"launchctl unload -w {target_dir / job.plist_name}" for job in jobs]


def daemon_jobs(*, restart_throttle_seconds: int = 30) -> tuple[ScheduledJob, ...]:
    """Long-running processes, kept apart from the scheduled jobs.

    Separate from :func:`scheduled_jobs` so the twelve periodic jobs remain
    exactly twelve: the status dashboard counts them, and a daemon appearing in
    that list would read as a thirteenth scheduled task.
    """
    return (
        ScheduledJob(
            name="discord-bot",
            args=("discord-bot",),
            interval_seconds=restart_throttle_seconds,
            description=(
                "Interactive Discord bot serving /check. Long-running, restarted "
                "on exit. Independent of the publisher jobs: a bot crash stops no "
                "monitoring, and a publisher failure does not disconnect the bot."
            ),
            long_running=True,
        ),
    )


def launchctl_available() -> bool:
    """Whether `launchctl` exists. False on Linux and in CI."""
    return shutil.which("launchctl") is not None


def truncate_log(path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> bool:
    """Truncate a log that has grown past ``max_bytes``.

    launchd does not rotate, and an unbounded log on a laptop is a slow disk
    leak. Truncation rather than rotation because the old content is scheduled
    job output, and the useful state is in the database.
    """
    if not path.exists() or path.stat().st_size <= max_bytes:
        return False
    path.write_text("")
    logger.info("truncated oversized log", path=str(path))
    return True
