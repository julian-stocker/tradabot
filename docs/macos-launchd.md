# Running on macOS with launchd

How to make tradabot run on a schedule locally. **Nothing installs itself** —
every step below is a command you type.

---

## Why launchd rather than cron

On a laptop the difference is decisive: **launchd runs a job that was missed
while the machine slept; cron silently skips it.** A scanner that goes quiet from
Friday evening to Monday morning and never mentions it is worse than one that
catches up noisily.

launchd also survives logout/login cleanly and does not need your shell profile.

## The four jobs

| Job | Command | Interval |
|---|---|---|
| `com.tradabot.sync` | `scanner sync` | 5 min |
| `com.tradabot.scan` | `scanner run-once` | 15 min |
| `com.tradabot.overview` | `scanner overview` | 60 min |
| `com.tradabot.summary` | `ops daily-summary-if-due` | 60 min |

The summary runs **hourly and decides for itself** whether the session has
closed. Pinning it to a wall-clock time would bake in a timezone and slip by an
hour twice a year with US daylight saving — a bug that shows up as a missing
report rather than an error. It is idempotent per trading session, so an hourly
trigger produces one report a day.

## Install

```bash
make ops-check        # validate first — do not schedule a broken install
make ops-install      # writes ~/Library/LaunchAgents/com.tradabot.*.plist
make ops-start        # launchctl load -w — THIS starts the schedule
make ops-status       # what has run, and where the portfolios stand
```

`ops-install` writes files and **starts nothing**. Files are inert until
`launchctl load` runs, and starting a schedule on your machine should be a
deliberate keystroke.

## Stop and remove

```bash
make ops-stop         # launchctl unload -w — stops the schedule, keeps the files
make ops-uninstall    # prints the removal commands
```

Reversible by design, and `ops-uninstall` prints rather than deletes, so you see
exactly what would go.

## What is in a plist

```
Label                com.tradabot.scan
WorkingDirectory     /Users/you/Documents/tradabot     ← so `.env` is found
ProgramArguments     .venv/bin/python -m app.cli scanner run-once
StartInterval        900
RunAtLoad            false                             ← loading does not fire a scan
StandardOutPath      <project>/logs/scan.log
```

**No `EnvironmentVariables` key, and no secret of any kind.** Credentials stay in
`.env`, read from the working directory exactly as a manual run does.
`~/Library/LaunchAgents` is world-readable by default and backed up by Time
Machine; it is not a place for credentials. A test asserts no plist contains one.

`WorkingDirectory` matters more than it looks: without it a relative SQLite path
resolves wherever launchd started, and the scheduled jobs quietly build a
**second database** that diverges from the one your manual commands use.

## Sleep and wake

**A sleeping MacBook is not 24/7 monitoring.** Be clear-eyed about this:

- While asleep, nothing runs. No scans, no notifications.
- On wake, launchd fires the missed jobs. Incremental sync catches up on bars,
  the next scan proceeds normally, and open positions and active signals survive
  because all state is in the database.
- **Missed opportunities cannot be reconstructed as real-time notifications.**
  The data arrives; the moment does not. A signal that qualified at 15:30 while
  the lid was shut is visible in the evaluation history, and no message about it
  is sent after the fact — announcing a two-hour-old entry as if it were current
  would be worse than silence.

For genuine 24/7 you need an always-on host: a Raspberry Pi, a mini PC, a NAS or
a small VPS. **Not implemented, and out of scope for this phase.**

## Logs

`<project>/logs/{sync,scan,overview,summary}.{log,err}`, git-ignored.

launchd does not rotate. Logs are truncated past 5 MB, because an unbounded log
on a laptop is a slow disk leak. No credential reaches them: provider and Discord
errors pass through `app/core/redaction.py`, which masks any URL containing
`/webhooks/`.

To check by hand:

```bash
tail -f logs/scan.log
launchctl list | grep tradabot
```

## Overlapping runs

Sync every 5 minutes and scan every 15 will collide. Two protections:

- **A database lease** stops two scans from running the same cycle; the second
  exits immediately (`scan skipped; lease held`). Leases expire, so a killed
  process does not lock the scanner out.
- **SQLite WAL mode** with a 5-second busy timeout lets a reader and a writer
  proceed at once. Without it the default journal takes an exclusive lock and one
  of the two fails with "database is locked".

## Troubleshooting

| Symptom | Check |
|---|---|
| Nothing ever runs | `launchctl list \| grep tradabot`; did you run `make ops-start`? |
| Runs but fails immediately | `logs/*.err` — usually a wrong `WorkingDirectory` or venv path |
| Two databases | A plist with the wrong `WorkingDirectory`; re-run `make ops-install` |
| `scan skipped; lease held` | Normal if a scan is running; self-clears within the lease window |
| No Discord messages | Usually correct — see `make ops-status` and `tradabot scanner candidates` |
