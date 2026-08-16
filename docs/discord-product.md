# The Discord product

Three things arrive in Discord, each in its own channel, each read-only.

| channel | what it carries | cadence |
|---|---|---|
| `market-trends` | Weekly Market Intelligence | weekly |
| `market-signals` | material market and company alerts | through the session |
| `paper-1k` / `paper-3k` / `paper-10k` | one account's Portfolio Analyst | every four hours |
| `status` | the existing dashboard, plus publisher health | every 15 minutes |
| `system` | transport health and recovery notices | on failure only |

## Running it

```
tradabot publish events --dry-run --render      # see what would be sent
tradabot publish events --companies             # include fundamentals and filings
tradabot publish portfolio                      # per-account, to its own channel
tradabot publish weekly --if-due                # the newsletter, once a week
```

`--dry-run` renders and routes without sending. Nothing is sent unless
`DISCORD_ENABLED=true` and a webhook is configured for that destination.

## Where the rules live

**Not here.** Materiality, deduplication, cooldowns, ranking and weekly
aggregation belong to `app.monitoring`, which is the single owner. The publishing
layer formats and delivers what already survived those rules. A test asserts this
package does not re-declare a threshold, a cooldown or a reporting floor.

The practical consequence: change a threshold in `app/monitoring/materiality.py`
and every channel follows. There is no second set of rules to drift.

## Routing

Destinations come from `app/core/webhooks.py`, the canonical resolver:

| variable | channel |
|---|---|
| `DISCORD_TRENDS_WEBHOOK` | market-trends |
| `DISCORD_MARKET_WEBHOOK` | market-signals |
| `DISCORD_PAPER_1K_WEBHOOK` | paper-1k |
| `DISCORD_PAPER_3K_WEBHOOK` | paper-3k |
| `DISCORD_PAPER_10K_WEBHOOK` | paper-10k |
| `DISCORD_STATUS_WEBHOOK` | status |

The retired `PAPER_100/1000/10000` simulation variables are not used.

**Paper channels never fall back.** An unconfigured slot produces no message for
that slot — it does not borrow the next one along. Three accounts run the same
strategy at three capital tiers, so one slot's output in another's channel would
be a true message producing a false conclusion.

**Portfolio events never reach market-signals.** "NVDA moved to 18% of equity" is
meaningless without knowing whose equity. A test asserts the market and portfolio
event kinds do not overlap, so a new kind cannot become routable to both.

## Silence, bursts and first runs

**Quiet days send nothing.** Not "nothing happened today" — nothing. Measured
over the last 30 sessions, 23% of sessions were completely silent and the median
session produced one message.

**Bursts become one ranked digest.** Above five events for a destination, a single
digest goes out with the top rows and a count of what was omitted. Earnings
season produced 53 genuine changes in one session in Phase 12.36; fifty-three
posts is how a channel gets muted. Over 30 sessions, 171 monitor events became
41 messages.

**The first publishing run is silent.** The monitor reports level-based findings —
unusual volume, unusual volatility, a large sector week — on its very first pass,
because those are conditions rather than transitions. The publisher records them
as seen and sends nothing, so the channel opens quiet instead of with a burst of
history.

## Delivery

**Idempotent.** Each event has a stable identity: its kind, its monitoring
deduplication key, and the *session it describes* — not the moment it was
noticed, which would change on every rerun. A restart re-derives the same id and
sends nothing.

**Failure-isolated.** `publish_events` never raises. A Discord outage cannot fail
a market-data sync, an Advisor calculation, a Portfolio Fit report or paper
accounting. Retries are bounded by the notifier's existing policy, and a webhook
URL never appears in a log, a report, an exception or an artifact.

**Recovery is bounded.** A failed delivery is recorded as failed, not left unseen —
left unseen it would become eligible again and a day-long outage would discharge
a day of alerts the moment Discord returned. Above ten accumulated failures, one
notice goes to the system channel and the backlog is dropped.

State lives in `data/monitor_delivery/`, beside the monitoring baseline. Nothing
about whether a message was delivered belongs in a table that records decisions
or orders.

## Vocabulary

Allowed: watch, monitor, review, risk, concentration, fundamental change,
valuation change, unusual activity.

Never: buy, sell, rotate, replace, target price, expected return, probability of
profit. A structural test asserts none of it appears in the package. The weekly
letter's watch list means **an observable change occurred**, never that a name is
expected to outperform — Phase 12.25 established that no company-quality or
valuation relationship in this data survives out-of-sample validation.

## Partial coverage

An account that represents only part of someone's holdings can say so:

```
TRADABOT_COVERAGE_PAPER_3K="PARTIAL — US holdings only"
```

Read from configuration rather than inferred, because whether an account is
someone's whole portfolio is a fact about their intent and no amount of position
data reveals it.

## What status still cannot tell you

The dashboard now reports monitor health, delivery health, pending failed
deliveries and the last market event. Every one of those is produced by the
machine being monitored.

**A machine that is off produces nothing, and an absent dashboard update is
indistinguishable from a quiet market.** True offline detection needs an external
watchdog — an off-host dead-man's switch that alerts on the *absence* of a
heartbeat. That is not built, and nothing in this repository can substitute for it.
