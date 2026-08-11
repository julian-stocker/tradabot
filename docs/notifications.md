# Notifications

How events become messages, what decides which ones are worth sending, and what
this layer is explicitly **not** allowed to influence.

> **The rule that governs everything here.**
>
> **Database:** stores *every* evaluated signal, decision and outcome.
> **Discord:** shows only selected, high-value events.
>
> Notification filtering must never affect what is persisted. A score of 60 is
> never announced and is always stored — its future outcome is a training example
> either way. See [Database vs Discord](#database-vs-discord).

---

## The path

```
market data → features → signals → decisions → PaperBroker → performance
                                       ↓
                                 Event (a fact)
                                       ↓
                              NotificationService          ← policy applied here
                                       ↓
                       ┌───────────────┴───────────────┐
                  ConsoleNotifier            DiscordWebhookNotifier
                                                       ↓
                                          #market-signals  #paper-trades
                                          #performance     #tradabot-system
```

`NotificationService` **is** an `EventPublisher`. That single fact is the whole
integration: anything that already publishes an event gains notifications by
being handed a different publisher at the composition root. No domain service
imports anything from `app/notifications/`, nothing calls `send_discord(...)`,
and turning it all off is constructing a different object.

Business code publishes **facts**, never commands:

```python
await events.publish(Event.paper_trade_closed(symbol="NVDA", payload=...))   # yes
await send_discord("NVDA closed +4%")                                        # no
```

## Configuration

```bash
TRADABOT_NOTIFICATIONS__ENABLED=true        # master switch
TRADABOT_NOTIFICATIONS__CONSOLE=false       # echo to the log, no Discord needed

TRADABOT_DISCORD__ENABLED=false
TRADABOT_DISCORD__MARKET_WEBHOOK=
TRADABOT_DISCORD__TRADES_WEBHOOK=
TRADABOT_DISCORD__PERFORMANCE_WEBHOOK=
TRADABOT_DISCORD__SYSTEM_WEBHOOK=
```

Full list in `.env.example`. Discord specifics — channels, security, retries —
are in [discord.md](discord.md).

With Discord disabled, tradabot runs, tests and demos normally. Missing webhook
configuration never breaks startup; it is only validated when Discord is enabled,
and then reported by **channel name**, never by value.

## Routing

The category is derived from the event type (`EVENT_CATEGORIES` in
`app/core/events.py`), not stored on the event — so an emitter cannot file the
same fact under two categories, adding an event type does not touch routing, and
adding a destination does not touch the emitters.

| Category | Events | Channel |
|---|---|---|
| `market` | `MarketSignalQualified`, `...Strengthened`, `...Invalidated`, `MarketOverview` | #market-signals |
| `paper_trade` | `PaperTradeOpened`, `...Closed`, `...Skipped` | **per portfolio** — see below |
| `performance` | `PortfolioPerformanceSummary`, `DailySimulationSummary` | #performance |
| `system` | sync failures, stale data, provider up/down, lifecycle, critical errors | #tradabot-system |

tradabot posts to those six channels and **never** to any other. #allgemein or
any community channel receives nothing automated, ever.

### Portfolio routing

A paper-trade event carries a **`routing_key`** — `paper-100`, `paper-1000`,
`paper-10000` — which wins over the category default:

```
Event.routing_key  →  NotificationMessage.routing_key  →  webhook lookup
```

The key comes from `simulation_profiles.notification_channel`, **persistent
portfolio identity**. It is never derived from message content: routing on what a
message happens to say breaks the moment the wording changes, and never loudly.

There is no `if capital == 100` anywhere. The settings validator collects any
`TRADABOT_DISCORD__PAPER_<N>_WEBHOOK` generically, so adding a `paper-250` is one
environment variable plus one entry in `app/simulation/portfolios.py` — no change
to notification or trading logic. A test asserts a portfolio that exists nowhere
in the codebase routes correctly.

`TRADABOT_DISCORD__TRADES_WEBHOOK` remains as a **fallback only**, used when a
portfolio has no destination of its own, so an installation mid-migration keeps
delivering rather than going quiet.

#market-signals stays **global**: it carries the signal, not any portfolio's
state.

## Severity

`INFO`, `SIGNAL`, `TRADE`, `WARNING`, `CRITICAL`. Anything not explicitly mapped
is `INFO` — the default has to be the quiet one, or severity stops meaning
anything. `CRITICAL` is reserved for a disconnected provider and an unhandled
failure; an alert level that fires often is an alert level nobody reads.

## Deduplication and cooldown

A scanner re-evaluating a watchlist every fifteen minutes produces a stream of
near-identical scores. Announcing each one makes the channel useless within a day.

So notifications fire on **transitions**, not on levels:

```
score 64  →  silent          below the threshold
score 76  →  QUALIFIED       crossed into range
score 77  →  silent          same phase, +1 is not material
score 86  →  STRENGTHENED    crossed the strong threshold
score 85  →  silent          still strong
score 61  →  INVALIDATED     dropped back out
```

| Setting | Default | Meaning |
|---|---|---|
| `SIGNAL_THRESHOLD` | 75 | Announce at or above |
| `STRONG_SIGNAL_THRESHOLD` | 85 | Announce an upgrade above |
| `SIGNAL_COOLDOWN_MINUTES` | 60 | Minimum gap between repeats |
| `MINIMUM_SCORE_CHANGE` | 5 | Movement needed for a repeat |

**These are engineering defaults for controlling volume.** Nothing in the scoring
model treats 75 or 85 as meaningful, and neither should you.

Cooldown and minimum-change both apply to *repeats within a phase* — either alone
still produces a stream. Neither gates a **phase change**: a signal collapsing
twenty minutes after it qualified is exactly what to interrupt someone for, and
the reader may have acted on the original message.

State lives in the `notification_state` table, so a restart does not re-announce
every open signal. It is keyed on `symbol:timeframe:horizon`, so a daily signal
cannot suppress an intraday one.

## System alerts

Same mechanism, different subject:

```
healthy    → unhealthy    notify
unhealthy  → unhealthy    silent      ← the spam this prevents
unhealthy  → healthy      recovery, with measured downtime
```

A stale feed is stale on every check. Alerting each time buries the moment it
*became* stale, which is the only interesting instant, and trains whoever reads
the channel to mute it.

## Delivery reliability — stated precisely

This is **not** a transactional outbox, and does not claim to be. A real outbox
needs a relay that polls and retries independently of the request that wrote the
row, and the scheduler for that is deferred to the deployment environment.

**Guaranteed:**

- **Trading state is never rolled back by a delivery failure.** `publish()`
  catches everything; a backend that raises is a bug in the backend and still
  does not escape. A paper trade stays persisted when Discord is down.
- **Every attempt is recorded** in `notification_attempts`, so a missing alert is
  visible rather than silent.
- **Notifications are sent after the business operation commits.** Announcing a
  trade that then rolls back is worse than not announcing one that did not.

**Not guaranteed:** at-least-once delivery. If the process dies between the commit
and the send, that notification is gone — message content is deliberately not
stored, since it would duplicate the signal and trade tables and grow without
bound.

**One partial recovery**, stated precisely because it is easy to overstate:
`notify_signal` only commits its "already announced" state when delivery
*succeeded*. A failed signal alert is therefore retried by the next evaluation of
that symbol, for as long as the condition persists. Nothing retries a failed
*system* alert.

That is at-most-once with an audit trail and opportunistic retry.

## Message content

Formatters are pure functions in `app/notifications/formatters.py`: event in,
`NotificationMessage` out. Two rules run through all of them.

**Never fabricate a metric.** A formatter renders what the event carries and
omits what it does not. A historical signal has no live quote, so the quote
section vanishes — rather than rendering a placeholder that reads like a number.
A monitoring channel is precisely where an invented figure gets believed.

**Never report gross as net.** Trade messages itemise fees, spread and slippage
beside the gross figure. The gap between the two is the entire point of
tradabot's cost modelling, and a message showing only gross would flatter every
result on the channel a human actually reads.

Two more, applied for the same reason:

- **Confidence renders as HIGH/MEDIUM/LOW**, never as a number. It measures
  agreement between components, not the probability of being right, and "0.72"
  invites exactly the wrong reading.
- **Metrics with too little behind them are omitted.** A win rate over two trades
  is noise wearing a percentage sign; a portfolio with no equity curve has no
  meaningful drawdown.

### Grouping

One signal evaluated by nine portfolios is nine decisions and **one** thing that
happened. `PaperTradeOpened` carries all of them and renders one message with a
profile/outcome table — the interesting information is the *pattern*, and nine
separate messages would hide it on the days it matters most.

### Truncation

Messages truncate from the end at `MAX_MESSAGE_CHARACTERS` (default 1900, below
Discord's hard 2000). Formatters put the important material first — symbol,
score, decision, result — so a long tail of reasons is what gets lost. Failing
delivery because an explanation had too many reasons would lose the alert
entirely, which is the worst outcome available.

## Commands

```bash
tradabot notifications test                      # labelled TEST to every channel
tradabot notifications test --category market    # one channel only
tradabot notifications status                    # config + delivery outcomes
tradabot notifications daily-summary             # build and send the daily report
```

Makefile: `make notify-test`, `make notify-status`, `make daily-summary`.

`GET /health/notifications` reports the same status over HTTP, and returns 503
when notifications are enabled and the most recent outcome was a failure.
Disabled notifications are a valid configuration, not a degraded state.

## Scheduling

`send_daily_summary` is a plain CLI command with no scheduler attached. Cron, a
systemd timer or a future in-process scheduler all invoke the same command, and
no business logic knows which. Scheduling policy is deployment configuration.

The suggested operating model — overview hourly, daily report after the session
close — is configuration for whoever deploys it, not behaviour compiled in.

## Database vs Discord

The distinction this whole layer is subordinate to.

| | Database | Discord |
|---|---|---|
| Every evaluated signal | ✅ | ❌ |
| Every trade decision | ✅ | ❌ |
| Counterfactual tracking | ✅ | ❌ |
| Every trade outcome | ✅ | ❌ |
| Selected high-value events | ✅ | ✅ |

**Notification filtering must never affect signal persistence, `TradeDecision`
persistence, counterfactual tracking, or outcome storage.** A score-60 signal is
not announced and *is* stored, and its forward outcome remains measurable.

This is a hard requirement, not a preference. The database is the dataset a
future ML phase trains on, and a dataset filtered by "what was interesting enough
to post to a chat channel" would carry a selection bias impossible to correct for
afterwards — one that would look like signal.

## Who emits these events

Phase 4 wired the emitters. The scanner calls `notify_signal` on a lifecycle
transition (qualified / strengthened / invalidated) **after** the symbol's
transaction has committed, and emits a grouped paper-decision event when a
qualified signal reaches the simulation profiles. See
[scanner.md](scanner.md) and [signal-lifecycle.md](signal-lifecycle.md).

## Adding a backend

Implement `NotificationBackend` (a `Protocol`: a `name` and an async `send`),
then add it in `build_backends`. Telegram, email or push are new files. No
trading logic changes, because none of it knows a backend exists.
