# Operations

Running tradabot continuously on a local machine. **Nothing here is installed
automatically** — a later deployment phase turns this into a service. These are
the exact commands.

---

> Scheduling on macOS is covered in detail in
> [macos-launchd.md](macos-launchd.md). This page is the command reference.

## First run

```bash
make migrate                    # schema through 0008
make portfolios-seed            # the 3 personal portfolios + local owner
tradabot seed-profiles          # the 9 simulation portfolios
tradabot watchlist seed         # the initial universe
tradabot scanner sync           # backfill market data (slowest step)
tradabot scanner run-once       # one cycle
tradabot scanner status
```

With `TRADABOT_MARKET_DATA_PROVIDER=alpaca`, symbols must also be in
`TRADABOT_MARKET_DATA__WATCHLIST` — that list *is* Alpaca's universe here (see
[providers/alpaca.md](providers/alpaca.md#the-instrument-universe)). `watchlist
seed` names anything it could not find rather than silently shortening the list.

## Cadence

| Every | Command | Why |
|---|---|---|
| 5 min | `tradabot scanner sync` | Keep bars current |
| 15 min | `tradabot scanner run-once` | Full evaluation |
| 60 min | `tradabot scanner overview` | Ranked top candidates |
| After close | `tradabot scanner daily-summary` | Portfolio report |

These are **declared, not enforced**. `TRADABOT_SCANNER__*_INTERVAL_MINUTES`
documents intent and is reported by `scanner status`; nothing in the code sleeps
or loops. Overlapping invocations are safe — the database lease makes a second
one return immediately.

## macOS scheduling

```bash
make ops-check       # validate before scheduling anything
make ops-install     # write templates (starts nothing)
make ops-start       # load them — this starts the schedule
make ops-status
make ops-stop
make ops-uninstall
```

Full detail, plist contents and sleep/wake behaviour:
[macos-launchd.md](macos-launchd.md).

### Hand-written alternatives

The templates below are what `ops-install` generates. Kept for reference.

### launchd (preferred on macOS)

`~/Library/LaunchAgents/com.tradabot.scan.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tradabot.scan</string>
  <key>WorkingDirectory</key><string>/Users/YOU/Documents/tradabot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/Documents/tradabot/.venv/bin/python</string>
    <string>-m</string><string>app.cli</string>
    <string>scanner</string><string>run-once</string>
  </array>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>/tmp/tradabot-scan.log</string>
  <key>StandardErrorPath</key><string>/tmp/tradabot-scan.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tradabot.scan.plist
launchctl unload ~/Library/LaunchAgents/com.tradabot.scan.plist
```

launchd runs a missed job when the machine wakes; cron silently skips it. On a
laptop that closes at night, that difference matters.

### cron

```cron
TRADABOT=/Users/YOU/Documents/tradabot
*/5  * * * 1-5  cd $TRADABOT && .venv/bin/python -m app.cli scanner sync
*/15 * * * 1-5  cd $TRADABOT && .venv/bin/python -m app.cli scanner run-once
0    * * * 1-5  cd $TRADABOT && .venv/bin/python -m app.cli scanner overview
30   21 * * 1-5 cd $TRADABOT && .venv/bin/python -m app.cli scanner daily-summary
```

`1-5` is weekdays; the scanner also checks the exchange calendar, so a holiday
costs one cheap no-op. 21:30 UTC is shortly after the US close in summer
(20:00 UTC) — adjust for winter, or run later and let the calendar decide.

Cron does not read your shell profile. `cd` into the project so `.env` is found.

## Inspecting

```bash
tradabot scanner status            # config, session, last run, counts
tradabot scanner candidates        # ranked, current
tradabot notifications status      # delivery outcomes
curl localhost:8000/api/v1/scanner/status
curl localhost:8000/api/v1/signals/active
```

## What healthy looks like

- `scanner status` shows a recent `last_success` and no `last_error`.
- **Zero qualified signals is normal.** With the default 75/85 and a baseline
  engine that needs a volume step change as well as a trend, expect days with
  nothing. That is the design.
- `hit_rate` well below 10%. Routinely high means the threshold is not
  selective and the hits are just the market.
- `evaluations kept` growing steadily — the dataset accumulating regardless of
  what was announced.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `scan skipped; lease held` | A cycle is running, or one crashed within the last 15 min. The lease expires by itself. |
| Every evaluation `STALE` | Sync is not running, or the market has been shut. Check `market-data status`. |
| `symbols_failed` high | Provider errors — check `scanner status` for the redacted last error. |
| Nothing on Discord | Usually correct. Confirm with `notifications status` and `scanner candidates`. |
| Nothing qualifies, ever | Expected. See [signal-lifecycle.md](signal-lifecycle.md#thresholds) before touching thresholds. |

## SQLite under a schedule

Overlapping jobs (sync every 5 min, scan every 15) are configured for:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | WAL | A reader and a writer proceed at once; the default journal takes an exclusive lock and one fails |
| `busy_timeout` | 5000 ms | A contended write waits instead of raising immediately |
| `synchronous` | NORMAL | Durable against a process crash — the failure that happens — without fsyncing every commit |
| `foreign_keys` | ON | |

A power cut could lose the last transaction. Acceptable for observations that
will be re-fetched, and stated rather than assumed.

This makes SQLite **safe for this schedule**, not good for many concurrent
writers.

## Database

SQLite is fine for 50 symbols. Move to PostgreSQL/TimescaleDB when the universe
passes ~200 symbols, the API serves concurrent reads under load, retention passes
about two years, or you want the `candles` hypertable. Migration is
`make migrate` against the new URL; nothing in the application changes.

**Back up the SQLite file before any upgrade.** It holds the ML dataset, and
those observations cannot be recreated — the market has moved on.

## Security

Never printed by any command or endpoint: Alpaca keys, Discord webhook URLs.
`scanner status` and `/health/*` report *whether* something is configured, never
what with. See [discord.md](discord.md#security).

---

## Research jobs (phase 5)

Backtesting and labelling are **manual, not scheduled**, and they are safe to run
while the LaunchAgents are going.

```bash
make backtest FROM=2026-07-24 TO=2026-08-11   # replay + per-portfolio execution
make outcomes                                  # label; matures pending rows
make research-calibration HORIZON=1d
make research-export HORIZON=1d
```

### Why they cannot disturb the scheduler

- Backtest observations are written to `signal_evaluations` with a
  `backtest_run_id`. Every production read filters on `backtest_run_id IS NULL`,
  so the candidate list, the daily summary and `ops status` never see them.
- Nothing in the research path touches `tracked_signals`, `scan_runs`,
  portfolios or notifications. No Discord message is ever sent.
- No scan lease is taken, so the 15-minute scan is never blocked.
- Writes commit per 8-symbol chunk (replay) and per 250-evaluation chunk
  (labelling), so no single transaction holds the SQLite write lock for long.
  With WAL and `busy_timeout=5000` a concurrent scan waits milliseconds.

Asserted in `tests/integration/test_backtest_research.py`: a full replay leaves
the tracked-signal, scan-run, position and notification counts unchanged.

### Cost

A 52-symbol replay over 13 sessions on the hourly timeframe takes roughly five
minutes and produces ~3,700 observations. Labelling ~4,000 evaluations across
seven horizons takes under two minutes. Both report their runtime.

### Re-running

Both are idempotent. A second `outcomes generate` updates rows in place rather
than duplicating them, and completes anything that was pending. A second
`backtest run` with the same configuration creates a new run row sharing the same
`run_key` -- which is how reproducibility is checked, not an error.
