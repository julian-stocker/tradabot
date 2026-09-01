"""LaunchAgent generation and the operations check.

**No test installs, loads or starts anything.** Templates are written to a
`tmp_path`, never to `~/Library/LaunchAgents`, and `launchctl` is never invoked --
a test suite that scheduled jobs on the machine running it would be a hostile
thing to ship.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from app.ops.launchd import (
    LABEL_PREFIX,
    MAX_LOG_BYTES,
    build_plist,
    install_commands,
    render_plist,
    scheduled_jobs,
    truncate_log,
    uninstall_commands,
    write_plists,
)

FAKE_HOOK = "https://discord.com/api/webhooks/1/secret-token-aaaa"
FAKE_KEY = "PKTESTFAKE1234567890"


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "tradabot"
    (root / "app").mkdir(parents=True)
    return {
        "root": root,
        "python": root / ".venv" / "bin" / "python",
        "logs": tmp_path / "logs",
        "agents": tmp_path / "LaunchAgents",
    }


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_every_job_runs_at_the_documented_cadence() -> None:
    """Phase 5.8.2 added trends and status; phase 10.1 added the option collector;
    phase 12.37 added the four presentation jobs.

    Neither trends nor status fetches market data -- they read what sync and scan
    persisted -- so matching the scan interval costs nothing and keeps
    observations from ageing before they are mentioned. The option collector
    does fetch, which is why it runs on its own cadence and guards itself on
    session, window and whether today is already captured.
    """
    jobs = {job.name: job for job in scheduled_jobs()}

    assert set(jobs) == {
        # trading and data
        "sync",
        "scan",
        "overview",
        "summary",
        "trends",
        "status",
        "options",
        # presentation only, added in phase 12.37; none can reach a broker
        "monitor-market",
        "monitor-companies",
        "monitor-portfolio",
        "weekly-newsletter",
        # phase 12.38: the ping an off-host watchdog judges by its absence
        "heartbeat",
    }
    assert jobs["options"].interval_seconds == 20 * 60
    assert jobs["sync"].interval_seconds == 5 * 60
    assert jobs["scan"].interval_seconds == 15 * 60
    assert jobs["overview"].interval_seconds == 60 * 60
    assert jobs["trends"].interval_seconds == 15 * 60
    assert jobs["status"].interval_seconds == 15 * 60
    # Presentation cadences. The market monitor runs through the session; the
    # company pass is daily because fundamentals move on filing days and each
    # pass runs the Advisor once per watched symbol.
    assert jobs["monitor-market"].interval_seconds == 30 * 60
    assert jobs["monitor-companies"].interval_seconds == 24 * 60 * 60
    assert jobs["monitor-portfolio"].interval_seconds == 4 * 60 * 60
    assert jobs["weekly-newsletter"].interval_seconds == 6 * 60 * 60
    assert jobs["heartbeat"].interval_seconds == 5 * 60


def test_the_presentation_jobs_cannot_mutate_a_broker() -> None:
    """**The gate.** Every job added for reporting is read-only by command."""
    presentation = {
        "monitor-market",
        "monitor-companies",
        "monitor-portfolio",
        "weekly-newsletter",
        "heartbeat",
    }
    for job in scheduled_jobs():
        if job.name in presentation:
            assert job.args[0] in ("publish", "heartbeat")
            assert not ({"submit", "cancel", "close", "exit"} & set(job.args))


def test_the_cadence_follows_configuration() -> None:
    jobs = {job.name: job for job in scheduled_jobs(scan_minutes=30, sync_minutes=10)}

    assert jobs["scan"].interval_seconds == 30 * 60
    assert jobs["sync"].interval_seconds == 10 * 60


def test_the_daily_summary_runs_hourly_and_decides_for_itself() -> None:
    """Pinning it to a wall-clock hour would bake in a timezone and slip by an
    hour twice a year with US daylight saving."""
    summary = next(job for job in scheduled_jobs() if job.name == "summary")

    assert summary.args == ("ops", "daily-summary-if-due")
    assert summary.interval_seconds == 60 * 60


def test_labels_are_namespaced() -> None:
    for job in scheduled_jobs():
        assert job.label.startswith(LABEL_PREFIX)
        assert job.plist_name.endswith(".plist")


# ---------------------------------------------------------------------------
# Plist content
# ---------------------------------------------------------------------------
def test_the_plist_uses_the_project_working_directory(paths: dict[str, Path]) -> None:
    """Otherwise a scheduled job creates a *second* SQLite database wherever
    launchd started, and the two diverge invisibly."""
    job = scheduled_jobs()[0]

    plist = build_plist(
        job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
    )

    assert plist["WorkingDirectory"] == str(paths["root"])


def test_the_plist_uses_the_virtualenv_python(paths: dict[str, Path]) -> None:
    job = scheduled_jobs()[0]

    plist = build_plist(
        job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
    )

    args = plist["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0] == str(paths["python"])
    assert ".venv" in args[0]
    assert args[1:3] == ["-m", "app.cli"]


def test_the_plist_does_not_run_at_load(paths: dict[str, Path]) -> None:
    """Loading an agent should not immediately fire a scan."""
    job = scheduled_jobs()[0]

    plist = build_plist(
        job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
    )

    assert plist["RunAtLoad"] is False


def test_a_plist_contains_no_secret(paths: dict[str, Path]) -> None:
    """`~/Library/LaunchAgents` is world-readable by default and backed up by
    Time Machine. Credentials stay in `.env`."""
    for job in scheduled_jobs():
        rendered = render_plist(
            job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
        ).decode()

        assert "EnvironmentVariables" not in rendered
        assert FAKE_HOOK not in rendered
        assert FAKE_KEY not in rendered
        for forbidden in ("API_KEY", "SECRET", "WEBHOOK", "discord.com", "PASSWORD"):
            assert forbidden not in rendered


def test_a_rendered_plist_parses(paths: dict[str, Path]) -> None:
    job = scheduled_jobs()[0]

    parsed = plistlib.loads(
        render_plist(
            job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
        )
    )

    assert parsed["Label"] == job.label
    assert isinstance(parsed["StartInterval"], int)


def test_logs_are_written_under_the_log_directory(paths: dict[str, Path]) -> None:
    job = scheduled_jobs()[0]

    plist = build_plist(
        job, project_root=paths["root"], python_path=paths["python"], log_dir=paths["logs"]
    )

    assert str(paths["logs"]) in str(plist["StandardOutPath"])
    assert str(paths["logs"]) in str(plist["StandardErrorPath"])


# ---------------------------------------------------------------------------
# Writing and installing
# ---------------------------------------------------------------------------
def test_writing_templates_creates_one_file_per_job(paths: dict[str, Path]) -> None:
    jobs = scheduled_jobs()

    written = write_plists(
        jobs,
        project_root=paths["root"],
        python_path=paths["python"],
        log_dir=paths["logs"],
        target_dir=paths["agents"],
    )

    assert len(written) == len(jobs)
    assert all(path.exists() for path in written)


def test_writing_templates_starts_nothing(paths: dict[str, Path]) -> None:
    """Files on disk are inert until `launchctl load` runs."""
    write_plists(
        scheduled_jobs(),
        project_root=paths["root"],
        python_path=paths["python"],
        log_dir=paths["logs"],
        target_dir=paths["agents"],
    )

    # The only artefacts are the plists themselves.
    assert sorted(p.suffix for p in paths["agents"].iterdir()) == [".plist"] * len(scheduled_jobs())


def test_install_commands_are_printed_not_executed(paths: dict[str, Path]) -> None:
    commands = install_commands(scheduled_jobs(), target_dir=paths["agents"])

    assert len(commands) == len(scheduled_jobs())
    assert all(command.startswith("launchctl load -w ") for command in commands)


def test_uninstall_is_the_exact_inverse(paths: dict[str, Path]) -> None:
    """Installation must be reversible, and visibly so."""
    jobs = scheduled_jobs()

    installs = install_commands(jobs, target_dir=paths["agents"])
    uninstalls = uninstall_commands(jobs, target_dir=paths["agents"])

    assert len(installs) == len(uninstalls)
    for install, uninstall in zip(installs, uninstalls, strict=True):
        assert install.replace("load", "unload") == uninstall


def test_no_test_touches_the_real_launch_agents_directory(paths: dict[str, Path]) -> None:
    """Guard against a future edit defaulting the target back to the real path."""
    from app.ops.launchd import LAUNCH_AGENTS_DIR

    write_plists(
        scheduled_jobs(),
        project_root=paths["root"],
        python_path=paths["python"],
        log_dir=paths["logs"],
        target_dir=paths["agents"],
    )

    assert paths["agents"] != LAUNCH_AGENTS_DIR
    assert not str(paths["agents"]).startswith(str(Path.home() / "Library"))


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
def test_a_small_log_is_left_alone(tmp_path: Path) -> None:
    log = tmp_path / "scan.log"
    log.write_text("a few lines\n")

    assert not truncate_log(log)
    assert log.read_text() == "a few lines\n"


def test_an_oversized_log_is_truncated(tmp_path: Path) -> None:
    """launchd does not rotate; an unbounded log is a slow disk leak."""
    log = tmp_path / "scan.log"
    log.write_text("x" * (MAX_LOG_BYTES + 1))

    assert truncate_log(log)
    assert log.read_text() == ""


def test_truncating_a_missing_log_is_harmless(tmp_path: Path) -> None:
    assert not truncate_log(tmp_path / "absent.log")
