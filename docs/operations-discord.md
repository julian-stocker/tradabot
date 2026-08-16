# Operating the Discord product

Everything needed to run, diagnose and recover this system after a reboot,
without reference to any conversation.

## What runs, and where it goes

| Discord channel | content | job | cadence |
|---|---|---|---|
| `market-trends` | Weekly Market Intelligence | `weekly-newsletter` | checks every 6h, publishes once a week |
| `market-signals` | material market/company events | `monitor-market` | every 30 min |
| | company fundamentals and filings | `monitor-companies` | daily |
| `paper-1k` / `paper-3k` / `paper-10k` | one account each | `monitor-portfolio` | every 4h |
| `status` | operational dashboard | `status` | every 15 min |
| `system` | transport failures and recovery | (on failure) | — |

**Silence is correct.** Roughly one in four sessions produces no message at all.
A quiet `market-signals` means nothing material changed, not that something broke.

## First installation

```
make ops-install                                  # write launchd templates
launchctl load -w ~/Library/LaunchAgents/com.tradabot.monitor-market.plist
# ...repeat for each job, or: make ops-start
launchctl list | grep tradabot                    # verify
```

Nothing starts by itself. `ops install` only writes templates.

Loading an already-loaded job fails visibly (`Load failed: 5`) rather than
registering it twice — that error is the safe outcome, not a problem.

## Running anything by hand

```
tradabot publish events                     # market-signals
tradabot publish events --companies         # plus fundamentals and filings
tradabot publish portfolio                  # only when something changed
tradabot publish portfolio --always         # unconditional, all three accounts
tradabot publish weekly                     # the newsletter
tradabot publish weekly --if-due            # only if this week is unsent
tradabot ops status-publish                 # refresh the status dashboard
tradabot heartbeat                          # ping the external watchdog
```

Add `--dry-run --render` to any `publish` command to see exactly what would be
sent without sending it. **Always do this first after changing anything.**

## Silencing publishing

Three levers, from softest to hardest:

1. `--dry-run` on a manual run — renders, sends nothing.
2. `DISCORD_ENABLED=false` in `.env` — every publish becomes a no-op; monitoring,
   analysis and paper accounting continue untouched.
3. `launchctl unload ~/Library/LaunchAgents/com.tradabot.monitor-market.plist`
   (and the other presentation jobs) — stops the scheduled runs.

Removing a webhook from `.env` also silences exactly that channel and nothing
else. It never reroutes.

## State, and what to delete when

| directory | holds | safe to delete? |
|---|---|---|
| `data/monitor_state/` | the previous observation per subject | yes — costs one quiet run while the baseline is relearned |
| `data/monitor_events/` | append-only journal of reported events | yes — the weekly digest loses history |
| `data/monitor_delivery/` | what has been delivered | **careful** — deleting it makes the next run treat each channel as new and baseline it silently, so recent alerts are never sent |
| `data/sec_facts.parquet` | production fundamentals | rebuildable: `tradabot fundamentals sync` |
| `data/sec_cache/` | per-symbol SEC payloads | yes — the next sync refetches |

## Diagnosing: is it data or transport?

These are different faults with different fixes, and the distinction is visible.

**Data not synced** — the tool says so and exits 2 (or 1 for `status`):

```
$ tradabot fundamentals status
FACT STORE DATA_NOT_SYNCED
```

Fix: `tradabot fundamentals sync` (about 20 minutes for the full universe).

**Transport failure** — analysis succeeded, delivery did not. The publish command
reports failures, `#status` shows `Discord delivery: DEGRADED` with a pending
count, and `#system` receives a notice:

```
$ tradabot publish portfolio --always
3 message(s): 0 delivered, 3 failed, 0 unconfigured, 0 already delivered
```

Fix: check the webhook still exists in Discord; replace it in `.env`; re-run.

**Nothing to publish** is neither of those — it is the normal quiet case:

```
$ tradabot publish events
Nothing to publish.
```

## Replacing a Discord webhook

1. Create a new webhook in the Discord channel.
2. Replace the value in `.env` (`DISCORD_MARKET_WEBHOOK`, etc.). The variable
   names are canonical; see `app/core/webhooks.py`.
3. `tradabot publish smoke-test --dry-run` to confirm routing.
4. `tradabot publish smoke-test` to send one labelled `TRADABOT TEST` message per
   destination.

A missing webhook silences that channel only. **Paper slots never fall back** —
`PAPER_1K` will not post into `paper-3k` under any circumstance.

## Portfolio coverage

By default a portfolio message says:

```
PORTFOLIO COVERAGE: ALPACA ACCOUNT ONLY — this account's positions; not a view of total holdings
```

To state something more specific, set one variable per account in `.env`:

```
TRADABOT_COVERAGE_PAPER_3K=US_ONLY_VIEW        # "PARTIAL — US-listed holdings only"
TRADABOT_COVERAGE_PAPER_1K=FULL_PORTFOLIO      # only if it genuinely is
TRADABOT_COVERAGE_PAPER_10K=PARTIAL_PORTFOLIO
```

Anything else is treated as free text and rendered as `PARTIAL — <your text>`,
e.g. `TRADABOT_COVERAGE_PAPER_3K="IBKR holds the rest"`.

This changes wording only. It never invents a position or estimates a weight for
holdings the account cannot see.

## Failure recovery

A failed delivery is recorded as failed, not forgotten. The next scheduled run
may retry it. Once more than ten failures accumulate, one summary goes to
`#system` and the backlog is dropped — a transport outage lasting a day will
never discharge a day of alerts when it clears.

Retries inside a single delivery are bounded by the notifier's policy and honour
Discord's `Retry-After`.

## The external watchdog

**`#status` cannot tell you the server is off.** Every field on that dashboard is
produced by this machine; a stopped process, a crashed interpreter and a closed
laptop all look identical from the inside — nothing new appears — which is also
what a quiet market looks like.

So the host emits a heartbeat every 5 minutes (`com.tradabot.heartbeat`), and
something off-host judges its absence. Grace period 15 minutes, i.e. three missed
beats: one is a network blip, three is a pattern.

**To finish this, an operator must provision two things:**

1. **A heartbeat endpoint** — any dead-man's-switch service (healthchecks.io,
   Better Stack, Cronitor). Put its ping URL in `.env` as
   `TRADABOT_HEARTBEAT_URL`, and its read/status URL in the GitHub repository
   secret `HEARTBEAT_STATUS_URL`.
2. **`DISCORD_STATUS_WEBHOOK` as a GitHub repository secret**, so
   `.github/workflows/watchdog.yml` can post to `#status` without this machine
   being involved.

Until both exist the workflow is inert by design and `#status` says
`Server heartbeat: NOT CONFIGURED`. It does not pretend to know.

States: `UP` · `LATE` (past the interval, inside grace — reported, never alerted)
· `DOWN` (alerts once) · `RECOVERED` (alerts) · `UNKNOWN` (never seen a beat).

## After a reboot

launchd restarts loaded agents automatically, and — unlike cron — runs jobs that
were missed while the machine slept. Verify with:

```
launchctl list | grep tradabot          # expect 12 jobs, exit=0
tradabot ops check                       # validates the installation
tradabot fundamentals status             # expect READY
tradabot publish events --dry-run        # expect a routing summary, no send
```

If a job shows a non-zero exit code, its log is under the path in the plist
(`~/Library/Logs/` by convention); logs are size-bounded and contain no secrets.

## What this system will never do

It places no orders, cancels nothing, closes nothing, and holds no execution
client on any path reachable from monitoring, publishing, Portfolio Fit or the
Advisor. It emits no buy, sell, target price or expected return, because no
validated predictive evidence in this repository supports one. Structural tests
enforce all of it.
