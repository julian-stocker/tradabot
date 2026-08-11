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
`app/market_data/registry.py`. Nothing else changes.

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

### 13. UTC everywhere, enforced at the type level

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
4. **`max_daily_loss` is stored but not enforced.** It needs a session boundary,
   which arrives with the exchange calendar. Every other risk limit is enforced.
   Paper trading is long-only and market-orders-only; both are refused rather than
   approximated.
5. **`symbol` is globally unique.** Real multi-venue listings (VOD on LSE vs. Xetra)
   need a `(symbol, exchange)` key. Mechanical migration later; guessing a venue today
   would not be.
6. **No exchange calendar.** `Timeframe.duration` is nominal wall-clock time; the mock
   provider approximates a US session and ignores DST and holidays.
7. **Regime is instrument-local.** A real regime signal needs market-wide inputs (index
   trend, breadth, a volatility index). Named honestly rather than overstated.
8. **Historical signals use the configured default spread.** Today's spread was not
   knowable in 2023; using it would be look-ahead in the cost model — the place people
   rarely think to check. Storing historical quotes is phase 5.
9. **`confidence` is not a probability.** It measures agreement between components and
   feature availability. A signal can be confidently wrong. Calibration is phase 8.

---

## Security

tradabot is a **local-first, single-user tool** and ships with **no authentication**.
`POST /api/v1/admin/sync` is unauthenticated and will happily ingest on request.

**Do not expose this service to a network you do not control.** Bind it to localhost,
or put it behind a reverse proxy that handles authentication, before it leaves your
machine. Adding auth is a phase 9 concern, alongside the web dashboard.

Secrets come from the environment only. `docker-compose.yml` uses `${VAR:?err}` so
compose fails loudly rather than starting with a blank password.
