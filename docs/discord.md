# Discord

The first notification backend. Confined to
`app/notifications/backends/discord.py`, the only module that knows Discord's
protocol — its payload shape, its 2000-character limit, its rate-limit headers.

> **Output only.** Discord is a monitoring surface. No trading logic depends on
> it, nothing reads from it, and there is no bot, no slash command and no
> interactive control. A Discord outage never fails a market-data sync and never
> rolls back a paper trade.

For routing, thresholds and the reliability model, see
[notifications.md](notifications.md).

---

## Channels

Four channels, one webhook each, because Discord scopes a webhook to a channel.

| Channel | Receives | Volume |
|---|---|---|
| `#market-signals` | Qualified / strengthened / invalidated signals, market overviews | Moderate; governed by thresholds and cooldown |
| `#paper-trades` | Grouped entry decisions, individual closes | One message per signal acted on |
| `#performance` | Daily report, portfolio summaries | Once a day |
| `#tradabot-system` | Sync failures, stale data, provider up/down, lifecycle, critical errors | Rare by design — transitions only |

Separate channels rather than one firehose because they are read at different
times and for different reasons: a system alert needs attention now, a daily
report does not, and mixing them means neither gets read properly.

## Setup

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, one
   per channel. Copy each URL.
2. Put them in your **`.env`**, which is git-ignored:

```bash
TRADABOT_DISCORD__ENABLED=true
TRADABOT_DISCORD__MARKET_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__TRADES_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__PERFORMANCE_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__SYSTEM_WEBHOOK=https://discord.com/api/webhooks/...
```

3. Verify:

```bash
tradabot notifications test        # or: make notify-test
```

Each configured channel receives a message labelled `🧪 TRADABOT TEST` naming the
channel and the environment. Nothing secret is included.

Configuring only some channels is fine. An event whose category has no webhook is
recorded as `skipped` — not a failure, because nothing was attempted.

## Security

**A webhook URL is a bearer credential.** Anyone holding one can post to that
channel as tradabot. Treated exactly like an API key:

- held as `SecretStr` — absent from reprs, `model_dump()` and log output;
- never logged, never returned by an endpoint, never written to disk;
- `.gitignore` excludes `.env`; `.env.example` carries empty placeholders only;
- `app/core/redaction.py` masks **any** URL containing `/webhooks/`, wherever it
  appears.

That last one is not theoretical. HTTP clients routinely put the request URL into
their exception strings, and that string travels into a log line, an audit row
and an HTTP response. The whole URL is masked rather than just the token, because
the id identifies the channel and no error message needs any part of it.

Redaction is applied at three boundaries, deliberately overlapping: inside the
Discord backend, at `NotificationService` where *any* backend's error text
enters, and at the health endpoint. Defence in depth — the primary control is
still not putting secrets into strings at all, and pattern-based redaction is
necessarily incomplete.

`GET /health/notifications` reports channel **names** and counts. No URL, no
token, no fragment of one.

Messages also set `allowed_mentions: {"parse": []}`, so a monitoring channel can
never `@everyone` a room even if a symbol name or a formatter ever produced one.

## Delivery

| | |
|---|---|
| Retried | 429, 500, 502, 503, 504 |
| Not retried | 401, 403, 404 (revoked webhook), other 4xx |
| Backoff | Exponential with full jitter, capped |
| `Retry-After` | Honoured, from header or JSON body |
| Attempts | `max_retries + 1`, default 4 |
| Timeout | 10s per attempt |

Discord's 429 is authoritative: it knows when it will accept traffic again, and
ignoring the header is how a client earns a longer ban. Without jitter, several
notifications rate-limited at once would retry in lockstep and rate-limit each
other again.

A 404 means the webhook was deleted. Retrying it only delays the operator
learning that, so it fails immediately and lands in the audit table.

**Delivery never raises.** Every failure becomes a `DeliveryResult` with
`delivered=False`. This is the property the whole design rests on — an exception
escaping this module is how a Discord outage becomes a lost trade.

| Setting | Default |
|---|---|
| `TRADABOT_DISCORD__REQUEST_TIMEOUT_SECONDS` | 10.0 |
| `TRADABOT_DISCORD__MAX_RETRIES` | 3 |
| `TRADABOT_DISCORD__BACKOFF_BASE_SECONDS` | 0.5 |
| `TRADABOT_DISCORD__BACKOFF_MAX_SECONDS` | 15.0 |
| `TRADABOT_DISCORD__USERNAME` | tradabot |

## Message limits

Discord rejects content over 2000 characters. Messages are truncated to
`TRADABOT_NOTIFICATIONS__MAX_MESSAGE_CHARACTERS` (1900 by default; the margin
absorbs the wrapper) with a visible marker.

Truncation drops from the end. Formatters put symbol, score, decision and result
first, so what is lost is a long tail of explanation — and losing that is much
better than failing delivery, which loses the alert entirely.

## Auditing

Every attempt is recorded in `notification_attempts`: event type, category, key,
backend, status, attempt count, status code, redacted error, and timestamps.

**Message content is not stored** — it would duplicate the signal and trade
tables and grow without bound. Consequently a failed delivery cannot be replayed
from the audit row; see the reliability section in
[notifications.md](notifications.md#delivery-reliability--stated-precisely) for
what is and is not recoverable.

```bash
tradabot notifications status      # counts, last success, last failure
curl localhost:8000/health/notifications
```

## Testing

Every test uses `httpx.MockTransport`. **No test opens a socket to Discord**, and
the suite runs with no webhook configured. The strongest assertions are negative:
that a webhook URL appears in no error, no log line, no audit row and no HTTP
response.

## Not implemented, deliberately

Slash commands, an interactive bot, reading messages, reacting to them, or any
path by which Discord could influence trading. Discord is where tradabot *writes*
what it decided; it is never where a decision comes from.

---

## Channel semantics (phase 5.6)

Each destination answers a different question. Mixing them is what made the
market channel unreadable.

| Channel | Trigger | Expected frequency | Empty means |
|---|---|---|---|
| **market-signals** | opportunity *transitions* only: QUALIFIED, STRONG, WEAKENED, INVALIDATED | rare — 385 qualified in 116,844 observations | **healthy silence** |
| **paper-100 / -1000 / -10000** | that portfolio's simulated open/close/stop/target | only when a signal qualifies *and* that portfolio can fund it | **healthy silence** |
| **performance** | daily summary | once per trading day | missing integration |
| **system** | provider down/recovered, scheduler failure, database problem | rare | **healthy silence** |

## Closed-market silence is a feature

`market-signals` used to emit "No qualified opportunities." every hour, including
overnight and at weekends. Zero candidates is the *normal* state — roughly 997 of
every 1,000 scans have nothing to say — so announcing it hourly is the purest
form of noise, and it trains the reader to ignore the one channel that should
never be ignored.

`evaluate_overview()` now suppresses on three ordered rules:

1. **market closed** (weekend, holiday, out of hours) — nothing has changed since
   the close and nothing can until the open;
2. **extended hours** — the scanner refuses to qualify setups on pre/post-market
   IEX prints, so an overview then can only ever say zero;
3. **zero candidates** — not news.

Closed-market status belongs in the daily summary, which is a report, not an
alert.

```
$ python -m app.cli scanner overview
overview suppressed: pre_market cannot qualify signals
(silence here is healthy -- see docs/discord.md)
```

None of this suppresses a *transition*: a signal newly qualifying still notifies.

## Embeds

Messages render as Discord embeds with a severity-coloured spine and a field
grid, with the **plaintext body always sent alongside** — a client that renders
no embed still shows everything. Disable with `TRADABOT_DISCORD__USE_EMBEDS=false`;
presentation degrades, information does not.

**No field is ever fabricated.** tradabot has no price targets, no support and
resistance levels and no probability estimates, so an embed never shows them. A
value that is absent is omitted, because a field labelled "Target" that came from
nowhere is worse than no field — the reader cannot tell the difference.

## Manual lifecycle demo

```bash
make notify-demo
# or: python -m app.cli notifications demo-lifecycle
```

Sends 13 clearly-marked messages — WATCH, QUALIFIED, STRONG, WEAKENED, a
simulated open and close for each portfolio, provider failure and recovery, and a
daily summary — to every destination.

Every message is prefixed `🧪 TEST` and uses the fake ticker `DEMOX`, never a real
symbol: a synthetic STRONG opportunity that reads like a real one is a message
somebody acts on, and using NVDA would leave fabricated NVDA alerts in the
channel history indistinguishable from real ones.

It **writes nothing** — no evaluation, no tracked signal, no position, no trade,
no research row — and is never scheduled or invoked by a test.

## Future channels (prepared, not created)

Phase 5.7 may add `watch-opportunities`, `buy-opportunities` and
`sell-exit-signals`. The routing layer already carries a `routing_key` per event,
so adding them is configuration rather than surgery. Semantics are fixed now to
avoid conflation later:

| Feed | Meaning |
|---|---|
| WATCH | developing setup, **below** threshold — not a trade signal |
| BUY | qualified bullish setup under the production policy |
| SELL/EXIT | **three distinct things**: exiting a bullish thesis · taking profit at a target · an independent bearish setup |

Those three must never be merged: "close the position" and "go short" are
opposite instructions that happen to share a direction of trade.

No webhook is required yet and `.env` is untouched.

---

## The live opportunity message

Since phase 5.6 the **live scanner path** emits this — not a demo, not a mock.
`_notification_payload` builds it from the evaluation that was just persisted, so
the message and the stored row cannot disagree.

```
🚀 STRONG BULLISH — STRENGTHENED

  Company        NVIDIA Corporation Common Stock
  State          STRONG
  Direction      BULLISH
  Score          87.1 / 100
  Confidence     82%
  Price          182.45
  Bid            182.40      Ask   182.50

  Intraday       BULLISH
  Short term     BULLISH
  Medium term    BULLISH
  Long term      NOT AVAILABLE

  Trend          UP
  Momentum       positive
  Volume         surging (2.4x)
  Structure      BREAKOUT
  Volatility     normal (31%)
  Liquidity      normal (5.5 bps)

  Data           2026-08-12 15:30 UTC
  Freshness      0 min old (OK)
  Source         alpaca / iex
```

Ordered identity → verdict → horizons → components → freshness, so the first
screenful answers "what, how strong, over what period".

**`Long term: NOT AVAILABLE` is shown deliberately.** Omitting it would let a
reader assume the horizon was merely neutral. See
[signal-intelligence.md](signal-intelligence.md).

### What can never appear

No support, resistance, price target, entry zone, expected price or long-term
forecast. tradabot computes none of them, so no field can claim them —
`_notification_payload` has no key for any of them, and a test asserts it stays
that way.

`Confidence` renders as a percentage because it is stored as a 0–1 fraction — a
scale this codebase's own analysis misread once.

## Future feeds: ready, and inert

`app/notifications/feeds.py` defines the vocabulary so the split is configuration
rather than surgery:

| Feed key | Lifecycle | Status |
|---|---|---|
| `buy-opportunities` | QUALIFIED / STRONG, bullish | ready |
| `sell-exit-signals` | WEAKENED / INVALIDATED | ready |
| `watch-opportunities` | — | **NOT_IMPLEMENTED** |

**Feed keys fall back to `market-signals` when no webhook is configured**, so
nothing changes until you create one. Portfolio keys deliberately do *not* fall
back: merging one portfolio's trades into a shared channel would misattribute
them, and a wrong number is worse than a missing one.

### Why WATCH is not implemented

The obvious definition — "score just below 75" — is exactly what the research
argues against. **The 70–75 band is the worst in the dataset**: 49.0% positive at
1d and 47.9% at 5d, both *below* the 51.6% baseline. A WATCH channel built on it
would promote the weakest evidence available.

The alternatives were measured too, and none discriminates: every component sits
within about ±1.5pp of the base rate, and volume and breakout confirmation add
least. So the routing is ready and the policy returns `NOT_IMPLEMENTED` rather
than inventing weak behaviour. The 75 threshold is untouched.

### SELL/EXIT is not "sell"

Three things get conflated under that word and only two are supported:

| | Supported |
|---|---|
| exit a long thesis (setup broke) | yes |
| take profit / rule-based exit | yes |
| open a short (independent bearish) | **no — production is long-only** |

The first two concern a position you hold; the third is a new position in the
opposite direction. `HEADLINES[SELL_EXIT]` reads "EXIT SIGNAL — long thesis
weakened", and a test asserts the word "short" never appears in it.

## #market-trends and #status, running (phase 5.8.2)

Phase 5.8.1 built both and wired neither. This phase makes them run, as **two
new launchd jobs** rather than tails appended to the scan cycle:

| Job | Command | Interval | Reads |
|-----|---------|----------|-------|
| `com.tradabot.trends` | `scanner trends` | 15 min | `signal_evaluations` + stored daily candles |
| `com.tradabot.status` | `ops status-publish` | 15 min | `operational_status` + `alembic_version` |

**Neither calls a market-data provider.** Both read what the sync and scan jobs
already persisted, which is the same reasoning behind `scanner overview`: a
summary is a view of what the last scan found, and refetching would answer a
different question at the cost of API quota.

They are separate *processes* on purpose. A `try/except` around a Discord call
contains an exception but not a hang, and a stalled HTTP request inside the scan
cycle would hold the scan lease past the next due scan. Nothing the trends job
does can reach the scanner, research persistence or paper trading.

### Session policy for trends

| Session | Behaviour | Why |
|---------|-----------|-----|
| REGULAR | active | |
| PRE_MARKET / AFTER_HOURS | silent | IEX prints too thin — a "volume spike" off a handful of trades measures the feed, not the market (phase 4 saw 883–1118 bps spreads) |
| CLOSED / WEEKEND / HOLIDAY | silent | nothing new happened |

An event fires once, stays quiet for a 4-hour cooldown while it persists, and
speaks again only if the move extends by ≥2pp. **There is no "nothing notable"
message.** Zero events sends nothing, and that is the normal state.

Observations older than two hours are not announced at all: if the scanner has
been down since morning, "NVDA is up 4%" may have stopped being true.

### What ONLINE / DEGRADED / OFFLINE actually mean

- **ONLINE** — sync and scan both ran inside their freshness windows.
- **DEGRADED** — one is late, an error is recorded, or notification delivery is
  currently failing.
- **OFFLINE** — nothing has ever run, or everything is far past its window.

**The limitation, stated plainly: a dead process cannot post 🔴 OFFLINE.** If the
whole installation stops, #status does not turn red — it stops updating. The
`Checked` timestamp is therefore the real liveness signal, which is why the
heartbeat republishes every 15 minutes even when nothing changed, and why that
note is printed in the message itself.

While the market is closed the freshness windows widen 4× rather than switching
off. A laptop that dozes for an hour on a Saturday is ordinary; a scheduler dead
for six hours still shows through.

### The dashboard is edited, not reposted

First publication POSTs with `?wait=true` and stores the returned message id in
`notification_state` (`scope='dashboard'`). Every later publication PATCHes that
message. **The webhook URL is never persisted** — the id alone grants nothing.
If the edit is rejected (message deleted, webhook rotated) the next run posts a
fresh message and stores the new id, so the worst case is one extra message.

Republication happens on a real content change or on the 15-minute heartbeat.
Time-derived fields (`Checked`, `Last sync`, `Last scan`, `Last delivery`) are
excluded from the change fingerprint — they move every tick without anything
having changed, and including them would republish constantly and defeat the
whole design. The stable field beside each of them still catches real changes.

### Commands

```bash
make trends-preview    # what #market-trends would say. Sends nothing.
make status-preview    # renders the dashboard locally. Sends nothing.

make trends-test       # SENDS A REAL MESSAGE, clearly marked TEST, constructed symbols
make status-test       # SENDS A REAL MESSAGE: forces a refresh of the existing dashboard
```

Previews use the same code path the scheduler runs (`evaluate` / `render`, the
read-only halves), so they cannot reassure you about a message they did not
produce. `trends-test` writes no state, so a test cannot silence a real
observation by starting its cooldown.

Missing `TRADABOT_DISCORD__TRENDS_WEBHOOK` or `__STATUS_WEBHOOK` means silence,
never a fallback: neither is a feed key, so an unconfigured destination cannot
spill trend text or a self-editing dashboard into #market-signals.
