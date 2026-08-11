# Architecture

## Shape of the system

tradabot is a **modular monolith**: one deployable process with strict internal
module boundaries.

Microservices were considered and rejected for this phase. The workload is a single
user analysing a few thousand instruments on a local machine. Splitting it across
services would add network partitions, distributed transactions, and deployment
coordination to a problem that currently has none of them — while the module
boundaries that would eventually make extraction possible can be enforced just as
well inside one process.

The boundaries below are chosen so that any one module *could* later become a
service without redesign. That is the real deliverable of phase 1.

---

## Module dependency rule

```
                        ┌──────────┐
                        │   api    │   FastAPI routes + wire schemas
                        └────┬─────┘
                             │
     ┌───────────┬──────────┼──────────┬────────────┐
     ▼           ▼          ▼          ▼            ▼
┌─────────┐ ┌──────────┐ ┌────────┐ ┌───────┐ ┌────────────┐
│instrum. │ │market_dat│ │features│ │signals│ │ simulation │
│+universe│ │+corp_act.│ │        │ │+costs │ │ +paper     │
└────┬────┘ └────┬─────┘ └───┬────┘ └───┬───┘ └─────┬──────┘
     │           │           │          │           │
     └───────────┴───────────┴─────┬────┴───────────┘
                                   ▼
                              ┌──────────┐
                              │  domain  │  enums, Quote, value objects
                              └────┬─────┘
                                   ▼
                              ┌──────────┐
                              │   core   │  config, logging, time, errors
                              └──────────┘
        (db/ is used only by repositories and services, never by
         features/, signals/, costs/, or corporate_actions/adjust.py)
```

Enforced rules:

1. **`core/` imports nothing from `app`.** Configuration, logging, UTC helpers, error types.
2. **`domain/` imports only `core/`.** The shared vocabulary — `Timeframe`, `Horizon`,
   `Quote`, `Classification`. Without this leaf package, `features`, `signals`,
   `costs` and `backtesting` would form an import cycle the moment any two of them
   needed to agree on what a timeframe is.
3. **`features/` and `signals/` never import `db/`.** They are pure functions of a
   Polars frame and a feature snapshot. This is what makes the no-look-ahead
   property test possible: there is no I/O to stub, and no hidden state.
4. **Route handlers contain no business logic.** They call a service and map the
   result onto a wire schema. Every route body is under ~20 lines.
5. **`api/schemas/` is separate from `db/models/`.** The database schema can change
   without breaking API consumers, and no internal column leaks by accident.
6. **`signals/` never reads `trade_decisions`.** The feedback system collects
   evidence; it must not close a loop back into the scoring rules. See
   [simulation-design.md](simulation-design.md#the-critical-constraint-on-feedback).

---

## Key decisions

### 1. Decimal for money, float64 for statistics

Monetary values (`open`, `high`, `low`, `close`, `volume`, `vwap`, all costs) are
`Decimal`, stored as `NUMERIC(18, 6)`. Indicators and scores are `float64`.

The conversion happens in **exactly one function**:
`app/features/frame.py::candles_to_frame`. One place to audit.

Why the split: rounding money is not acceptable — cost accounting subtracts small
numbers from other small numbers, the worst case for relative error. But a 200-period
EMA over `Decimal` is slow and pointless; an indicator is a statistic, not an amount.

Two mechanisms enforce this:

- `app/db/types.py::Money` **raises `TypeError` if handed a `float`**. Not a warning
  — a hard failure at the boundary.
- API responses serialise prices as JSON **strings**. JSON has no decimal type, so a
  number would round-trip through a double and silently undo the storage guarantee.

On SQLite (used for tests), `Money` stores a zero-padded fixed-point string. The
padding makes lexicographic ordering agree with numeric ordering, so `CHECK (high >= low)`
still holds. This equivalence only covers non-negative values, which is all `Money` is used for.

### 2. No look-ahead bias, enforced mechanically

Three independent layers, because convention alone has a poor track record here:

| Layer | Mechanism |
|---|---|
| **Indicators** | Only causal Polars primitives: `shift(k>0)`, `rolling_*` with `center=False`, `ewm_mean(adjust=False)`. No centred windows, no negative shifts, no `reverse()`. |
| **Query** | `CandleRepository.get_latest(as_of=…)` excludes later bars *in SQL*. Filtering after the fact is a step that gets forgotten. |
| **Test** | `tests/unit/test_no_lookahead.py` asserts **prefix invariance** over every registered feature: truncating the series after bar *i* must not change any value at bar *i*. |

The test layer is the important one, because it covers features that do not exist yet.
Anything added to the registry is checked automatically.

There is also a direct falsification test: multiply the final bar's price by 10 and
assert that no earlier feature value moves.

### 3. Directional vs. quality components

The scoring model splits components by `ComponentKind`:

- **DIRECTIONAL** (momentum, volume, trend) — signed opinion about direction.
- **QUALITY** (volatility, regime, spread) — scores in `[-100, 0]`, no direction.

They aggregate differently:

```
directional = Σ(wᵢ · sᵢ)                      over DIRECTIONAL
dampening   = Σ(wⱼ · |sⱼ|)                    over QUALITY
score       = directional · (1 − quality_share · dampening/100)
```

**Why not just sum everything?** Because a quality component has no direction to
contribute. Summing "spread is wide" as a negative number into a directional total
makes an illiquid stock look *bearish* — which is nonsense, since a wide spread
punishes a short exactly as much as a long. Treating quality as a multiplicative
dampener means poor conditions shrink a signal toward neutral, which is the actual
claim, and the code clamps the factor at zero so they can never invert it.

Components that cannot evaluate (features not warmed up) are dropped and the
remaining weights **renormalised within their kind**. A missing feature therefore
lowers `confidence` rather than silently voting neutral.

### 4. Cost as a gate, not a footnote

Every `SignalResult` carries a `NetEdge`:

```
net_edge_bps = expected_move_bps − round_trip_cost_bps
```

`is_actionable` requires **both** a non-neutral classification and a positive net edge.
A bullish signal whose expected move is smaller than the spread plus fees is not an
opportunity, and the type system makes that hard to ignore.

The fixed per-order fee makes cost **size-dependent**: at a €1.00 fee, a €500 position
pays 40 bps in fees alone versus 1 bps for a €20,000 position. Small positions are
routinely uneconomic for reasons that have nothing to do with the signal.

> **The weakest number in the system** is `expected_move_bps`. It is
> `|score|/100 · capture_ratio · ATR% · √horizon_bars` — three unvalidated assumptions
> stacked. It exists so the cost-gating plumbing is exercised end to end, and
> `capture_ratio` is configuration precisely because it is the first thing phase 7
> must calibrate. Treat the resulting net edge as illustrative.

### 5. Provider abstraction via `Protocol`

`MarketDataProvider` is a `typing.Protocol`, not an ABC. Providers are independent
adapters sharing no implementation, so inheritance would buy nothing, and structural
typing makes test doubles trivial. Streaming is a *separate* protocol
(`StreamingMarketDataProvider`) so non-streaming providers do not have to stub a
method they cannot support.

Adding a real provider means writing one adapter and one line in
`app/market_data/registry.py`. Nothing else changes — which phase 3b confirmed:
`app/market_data/providers/alpaca.py` is the only module in the codebase that
knows Alpaca exists, and it was added without touching a single downstream
consumer.

Two constraints on that adapter turned out to matter, and are documented in
[providers/alpaca.md](providers/alpaca.md):

* The SDK is **synchronous**, so every call runs on a worker thread via
  `asyncio.to_thread`. Wrapping a blocking client in `async def` without moving
  it off the loop stalls every other request in the process.
* SDK imports are **lazy**, so the module is importable — and the API starts —
  with no credentials and no SDK installed.

### 6. TimescaleDB, optional by design

Migration `0002` converts `candles` into a hypertable **only if the extension is
available**, otherwise it logs and skips. Nothing in the application depends on
Timescale being present.

This is affordable because the composite primary key
`(instrument_id, timeframe, timestamp)` already answers the core query —
"all 5-minute candles for NVDA between t₀ and t₁" — as a single index range scan.
Timescale adds chunk exclusion and compression, which matter at a data volume phase 1
does not have. Making it mandatory would trade real portability (and fast SQLite
tests) for a benefit that is currently theoretical.

The composite PK also satisfies Timescale's requirement that the partitioning column
appear in every unique index.

### 7. Raw data is never overwritten

Corporate-action adjustment is computed **on read**, never stored. A new split
retroactively changes every earlier adjusted price, so storing them would mean
rewriting an instrument's whole history on each new action — and the raw record,
the only factual one, would be at risk from any bug in that rewrite.

Adjustment happens at the `FeatureService` boundary, on `Decimal` prices, before
the float conversion. Indicators have no adjustment opinion at all, which is what
makes "an RSI on raw prices next to an SMA on adjusted ones" structurally
impossible. See [data-adjustments.md](data-adjustments.md).

### 8. Two notions of "active", deliberately

`Instrument.is_active` is the provider's *current* view — cheap to filter, right
for a UI listing. `Instrument.is_tradable_at(t)` is the authority for *historical*
questions and consults only the lifecycle dates.

They cannot silently diverge: `InstrumentInfo` forces `is_active=False` when
`delisted_at` is in the past. The converse is deliberately not enforced — an
instrument can be suspended without a delisting date, and overwriting that would
discard information the provider gave us.

`UniverseService` has **no method that returns "all instruments"**. Every query
takes a timestamp or a window, so a caller cannot accidentally apply today's
universe to a 2019 backtest.

### 9. Execution gates are separate from decision gates

`DecisionReason` records why a profile *wanted* or declined a trade, computed from
the signal. `OrderRejectionReason` records why the broker *could not place* it,
computed from live portfolio state. They fire at different moments against
different data, and a system with one enum cannot distinguish "we didn't want it"
from "we couldn't afford it". See [paper-trading.md](paper-trading.md).

### 10. The simulator never resolves ambiguity in its own favour

OHLC cannot say whether a stop or a target was hit first within a bar. The default
policy assumes the stop; a gapped stop fills at the open, not the stop price. Both
choices are pessimistic, because both errors are one-directional — getting them
wrong always makes results look better than reality.

### 11. Paper-trading state lives in the database, never in memory

Cash is a stored ledger balance mutated inside the same transaction as the event
that moves it. The engine holds nothing between calls, so restart recovery needs
no special case and idempotency is enforced by UNIQUE constraints rather than by
convention.

### 12. Capital size and risk appetite are separate tables

`risk_profiles` holds fractions of equity; `simulation_profiles` holds capital.
Nine portfolios across three risk appetites store three risk rows, not nine.
See [simulation-design.md](simulation-design.md).

### 13. The exchange calendar is provider-independent

`app/market_data/calendars.py` wraps `exchange-calendars` and sits beside the
providers rather than inside one. Which days a venue trades is a fact about the
venue, not about who sells you the data, and two providers disagreeing about a
holiday would be a bug rather than a configuration choice.

It is also the module that lets three separate things stop guessing: gap
detection (a weekend is not missing data), holding periods (five *trading* days),
and the daily-loss budget (a *session*, not a UTC day). pandas is contained
inside the wrapper -- `session_containing` returns a plain `date`, so the
dependency does not leak into the domain.

### 14. Notifications are a subscriber, never a caller

`NotificationService` implements the existing `EventPublisher` protocol. That is
the entire integration: a domain service that already published events gains
Discord delivery by being handed a different publisher at the composition root.

The alternative -- calling a webhook from inside the paper engine -- would put
network I/O, retry policy and a second set of credentials inside a class whose
job is booking trades, make the engine untestable without stubbing a notifier,
and let a Discord outage roll back a trade. Instead:

* business code publishes **facts** (`PaperTradeClosed`), never commands
  (`send_discord(...)`);
* `publish()` catches everything, so no delivery failure can reach a caller;
* delivery happens **after** the business transaction commits, because announcing
  a trade that then rolls back is worse than not announcing one that did.

The reliability model is at-most-once with an audit trail, not a transactional
outbox, and [notifications.md](notifications.md) says so explicitly rather than
implying a stronger guarantee.

### 15. Notification filtering must not shape the dataset

Thresholds, cooldowns and deduplication control **message volume only**. Every
signal is computed, scored and persisted regardless of whether it is announced.

This is a hard constraint rather than a preference: the database is what a future
ML phase trains on, and a dataset filtered by "what was interesting enough to
post to a chat channel" would carry a selection bias that is impossible to
correct for afterwards -- and that would look, in evaluation, like signal.

### 16. Signal identity is separate from signal observation

`tracked_signals` is a continuing setup's **identity**; `signal_evaluations` is
**what was known at time T**. One signal, many evaluations.

The alternative -- a fresh signal row per scan -- makes "how long has this been
true?" unanswerable and turns a fifteen-minute cadence into a stream of
duplicate discoveries. The existing `signals` table is referenced rather than
duplicated: it already holds a single-timeframe scored snapshot, and phase 4 adds
the multi-timeframe context around it.

Identity is the five-tuple (instrument, direction, primary timeframe, horizon,
setup premise). A change in any of them starts a new signal, because merging two
distinct setups loses information irrecoverably while splitting one is visible.

### 17. The scan cycle owns no clock

`run_scan_cycle(as_of)` does one pass and returns. It does not sleep, loop or
schedule itself; an external scheduler decides the cadence, and the configured
intervals are *declared* rather than enforced. A domain service owning its own
clock would be untestable without waiting and unkillable mid-cycle.

Concurrency is handled by a **database-backed lease** rather than process memory,
so a second cron invocation, a restarted process or a second machine all contend
correctly. Leases expire: a process killed mid-cycle must not lock the scanner
until a human notices.

### 18. Notification routing follows identity, not content

A paper-trade event carries a ``routing_key`` taken from the portfolio's stored
``notification_channel``. Routing never inspects the message.

Deriving a destination from capital (``if capital == 100``) or from message text
would couple delivery to two things that change for unrelated reasons -- a
portfolio's size, and how a formatter happens to word a sentence. Both would fail
silently. Instead the settings layer collects any ``PAPER_<N>_WEBHOOK``
generically, so a new portfolio is configuration rather than code.

### 19. Ownership exists before it is needed

``tradabot_users`` holds one row and nothing authenticates against it. It exists
now because ``simulation_profiles`` is about to accumulate live financial state,
and adding an ownership column to that table later means migrating live records
rather than configuration.

``external_account_connections`` records *that* a connection exists and what for.
It stores a ``credential_reference`` -- a pointer -- and the market-data registry
never reads it. A database row must not be able to change which credentials the
system authenticates with.

### 20. Scheduling is generated, never installed

``app/ops/launchd.py`` writes plists and prints ``launchctl`` commands. Nothing
loads them, and no test does either: a suite that scheduled jobs on the machine
running it would be a hostile thing to ship. The plists contain no credential --
the jobs read ``.env`` from a working directory, because
``~/Library/LaunchAgents`` is world-readable and backed up.

### 21. UTC everywhere, enforced at the type level

`ensure_utc` **rejects naive datetimes** rather than assuming UTC. Guessing is how
off-by-one-session bugs get into backtests. The `UTCDateTime` column type normalises
on write and returns aware datetimes on read, on both PostgreSQL and SQLite.

Bar timestamps are **open times** (left edge). A 5-minute bar stamped 14:30 covers
[14:30, 14:35) and is only complete at 14:35 — which is why providers must return
only closed bars.

Time windows are **half-open** `[start, end)` throughout, so consecutive requests tile
without duplicating a boundary bar.

---

## Heuristics — and what they are worth

Every constant in the scoring model is a **legible guess**. None has statistical
backing. They are collected here so nobody has to grep for them.

| Where | Constant | Basis |
|---|---|---|
| `SignalWeights` | momentum .25, volume .25, trend .20, volatility .10, regime .10, spread .10 | Assigned in the project brief. Plausible, unvalidated. |
| `SignalSettings` | bullish ±20, strong ±55 | Chosen so the classes are non-empty on sample data. |
| `momentum` | 3% / 5-bar, 8% / 20-bar scales | Conventional "notable move" magnitudes. |
| `trend` | 2% EMA spread, 5% SMA distance | Conventional chart readings. |
| `volatility` | calm 25%, severe 80% annualised | Equity-like ranges. |
| `spread` | cheap 10%, prohibitive 100% of expected move | Reasoning about ratios, not measurement. |
| `expected_move_capture_ratio` | 0.25 | Deliberately conservative. **Fully invented.** |
| Feature windows | 14, 20, 50, 60 | Conventional chart periods. |

These are *inputs to* the phase 4 backtest, not conclusions from one. The purpose of
writing them down is to make them falsifiable.

---

## Known simplifications

Each is a deliberate phase 1 trade-off, not an oversight.

1. **Corporate actions cover splits and cash dividends.** Other types can be
   *recorded* without a migration (one discriminated table) but are not adjusted
   for. `TOTAL_RETURN` raises `NotImplementedError` rather than guessing a
   reinvestment convention.
2. **Point-in-time filtering uses effective dates, not announcement dates.** A
   split is known to the market weeks before it takes effect; tradabot's
   `known_as_of` is therefore conservative.
3. **Universe is lifecycle, not index membership.** It answers "was this listed?",
   not "was this in the index?".
4. **Paper trading is long-only and market-orders-only.** Shorts and limit orders
   are refused rather than approximated. (`max_daily_loss` is now enforced, per
   trading session -- phase 3b.)
5. **`symbol` is globally unique.** Real multi-venue listings (VOD on LSE vs. Xetra)
   need a `(symbol, exchange)` key. Mechanical migration later; guessing a venue today
   would not be.
6. **Historical spreads are modelled, not stored.** Bars carry no quote, so
   phase 5 estimates the spread from price, volatility, participation and session
   (`app/research/costs.py`), stamps every figure `CostBasis.MODELLED`, and
   versions the model. It is consistent between scoring and execution, and it is
   still an assumption: a real spread widens exactly when it matters most.
   Storing historical quotes is phase 6.
7. **Regime is instrument-local.** A real regime signal needs market-wide inputs (index
   trend, breadth, a volatility index). Named honestly rather than overstated.
8. **A backtest is not survivorship-bias-free.** `instruments` has no
   `listed_at`/`delisted_at`, so a historical universe resolves to today's
   survivors and results are biased upward by an unmeasured amount. Recorded on
   every run rather than hidden; fixing it is phase 6.
9. **`confidence` is not a probability.** It measures agreement between components and
   feature availability. A signal can be confidently wrong. Calibration is phase 8.
10. **Research observations share the production table.** `signal_evaluations`
   holds both, separated by `backtest_run_id` (null = live). One schema means one
   dataset shape; the isolation is enforced by every production read filtering on
   null, and asserted in `tests/integration/test_backtest_research.py`. Two tables
   would have been safer against a forgotten filter and worse for everything else.

---

## Security

tradabot is a **local-first, single-user tool** and ships with **no authentication**.
`POST /api/v1/admin/sync` is unauthenticated and will happily ingest on request.

**Do not expose this service to a network you do not control.** Bind it to localhost,
or put it behind a reverse proxy that handles authentication, before it leaves your
machine. Adding auth is a phase 9 concern, alongside the web dashboard.

Secrets come from the environment only. `docker-compose.yml` uses `${VAR:?err}` so
compose fails loudly rather than starting with a blank password.

Provider credentials (phase 3b) follow the same rule, with four properties held
deliberately:

* held as `SecretStr`, so they never appear in a repr, a `model_dump()` or a log;
* never interpolated into an exception message;
* never returned by an endpoint -- `/health/market-data` reports *whether* a
  provider is configured, never what with, and carries no key prefix or length
  from which one could be confirmed;
* `.env.example` carries empty placeholders only.

`app/core/redaction.py` masks credential-shaped text at every outward boundary
(log line, event payload, HTTP response, notification audit row). Discord webhook
URLs are included: a webhook is a bearer credential, and HTTP clients routinely
echo the request URL into their exception strings. That is **defence in depth against a
third-party SDK echoing request context into its own error strings** -- not the
primary control, which is not putting secrets in strings at all. It is
pattern-based and therefore incomplete.
