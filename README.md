# tradabot

A local-first platform for **stock-market analysis, signal generation, backtesting and
probabilistic forecasting**.

tradabot continuously ingests market data, computes transparent features, and produces
explainable, statistically testable directional signals — always accounting for the
transaction costs that decide whether an edge survives contact with a broker.

---

## What tradabot is

- A **research and analysis platform** you run on your own machine.
- A system for producing **explainable, rule-based signals** — every signal states the
  evidence for it and the risks against it.
- A framework built so that signals can be **honestly backtested** and, later,
  evaluated with walk-forward machine learning.
- A place where **transaction costs are first-class**, not an afterthought. A bullish
  forecast that is smaller than the round-trip spread plus fees is not an opportunity.

## What tradabot is **not**

- **Not a price predictor.** Nothing here claims to know where a stock will go.
  Outputs are probabilistic and provisional.
- **Not a trading bot.** There is no order execution, no broker integration, and no
  path to placing a real order. That is a deliberate architectural boundary.
- **Not financial advice.** The baseline scoring weights are *heuristics chosen for
  legibility*, with zero statistical validation. They exist to be falsified by the
  backtesting engine in phase 4.
- **Not validated on real data.** Real US equity data now flows through the whole
  pipeline, which proves the *ingestion* is correct. It proves nothing about
  predictive edge: no backtester, no significance testing, no out-of-sample
  discipline exists yet. A profitable-looking simulation today is a statement
  about the plumbing.

---

## Architecture at a glance

A **modular monolith**. One deployable process, strict module boundaries inside it.
Microservices would add distributed-systems failure modes to a problem that does not
yet have them.

```
app/
├── core/              Configuration, logging, UTC time helpers, error hierarchy
├── domain/            Shared vocabulary: Timeframe, Horizon, Quote, Classification
├── db/                SQLAlchemy models, custom Decimal/UTC column types, sessions
├── instruments/       Instrument repository, service, point-in-time universe
├── market_data/       Provider abstraction, mock provider, ingestion, candle repository
├── corporate_actions/ Splits and dividends; the price-adjustment layer
├── features/          Polars feature engine, indicators, registry
├── costs/             Spread, fee and slippage modelling; net-edge calculation
├── signals/           Rule-based scoring components, signal engine, persistence
├── simulation/        Simulation profiles, position sizing, trade decisions
├── paper/             PaperBroker, execution, exits, portfolio accounting, engine
├── broker/            Broker protocol (implemented by app/paper/broker.py)
├── forecasting/       Interfaces for future probabilistic forecasts (no implementation)
├── backtesting/       Data structures and protocols for the future engine (no engine)
├── scanner/           Interfaces for the future market scanner (no implementation)
└── api/               FastAPI routes and wire schemas — thin, no business logic
```

The dependency rule is one-directional:

```
api → {instruments, market_data, corporate_actions, features,
       signals, costs, simulation, paper} → domain → core
                        ↓
                       db
```

`features/` never imports `db/`. `signals/` never imports `api/`. `domain/` imports
nothing but `core/`. See [docs/architecture.md](docs/architecture.md) for the reasoning.

---

## Quick start

### Option A — Docker (recommended)

```bash
cp .env.example .env          # then edit POSTGRES_PASSWORD and TRADABOT_DATABASE_URL
docker compose up --build
```

The API is then on <http://localhost:8000>, docs on <http://localhost:8000/docs>.

Migrations run automatically on container start. To seed synthetic data:

```bash
docker compose exec api python -m app.cli seed --symbols NVDA,AAPL,MSFT --days 400
docker compose exec api python -m app.cli seed-profiles
```

### Option B — local Python

Requires Python 3.12+ and a reachable PostgreSQL (or just use SQLite, below).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

# Point at SQLite for a zero-infrastructure run:
export TRADABOT_DATABASE_URL="sqlite+aiosqlite:///./tradabot.db"

make migrate          # apply migrations
make seed             # ingest synthetic instruments, corporate actions and candles
make seed-profiles    # install the 9 default simulation profiles
make demo-simulation  # run a deterministic paper-trading simulation
make dev              # start the API with autoreload
```

> **SQLite is for convenience and tests only.** TimescaleDB is the supported target;
> SQLite lacks the numeric type, the time-partitioning and the concurrency the real
> workload needs.

---

## Common commands

| Command | What it does |
|---|---|
| `make dev` | Run the API with autoreload on `:8000` |
| `make test` | Run the full test suite |
| `make test-cov` | Tests with a coverage report |
| `make lint` | Ruff lint checks |
| `make format` | Ruff auto-format + import sorting |
| `make typecheck` | mypy in strict mode |
| `make check` | `format-check` + `lint` + `typecheck` + `test` — run before committing |
| `make migrate` | Apply all migrations (`alembic upgrade head`) |
| `make migration m="..."` | Autogenerate a new migration |
| `make downgrade` | Roll back one migration |
| `make seed` | Ingest deterministic synthetic data (instruments, actions, candles) |
| `make seed-profiles` | Install the default simulation-profile catalogue |
| `make demo-simulation` | Run the deterministic multi-profile paper-trading demo |
| `make market-data-status` | Provider configuration and stored-data freshness |
| `make market-data-import s=NVDA from=... to=...` | Import a real historical window |
| `make market-data-sync [s=NVDA]` | Update the watchlist from its newest stored bar |
| `make quote s=NVDA` | Fetch one latest quote |
| `make simulate s=NVDA from=... to=...` | Replay imported real candles through the paper broker |
| `make smoke-real-data` | Opt-in live-provider smoke test (needs credentials) |
| `make notify-test` | Send a labelled TEST notification to every configured channel |
| `make notify-status` | Notification configuration and delivery outcomes |
| `make daily-summary` | Build and send the daily portfolio report |
| `make watchlist-seed` | Seed the initial 52-symbol development universe |
| `make scan` | Run one scan cycle |
| `make scan-sync` | Incrementally sync watchlist market data |
| `make candidates` | Show ranked current candidates |
| `make demo-scanner` | Deterministic offline scanner demonstration |
| `make portfolios-seed` | Install the 3 personal paper portfolios + local owner |
| `make portfolios` | Portfolio equity and open positions |
| `make ops-check` | Validate this installation can run unattended |
| `make ops-install` / `ops-start` | Write launchd templates / start the schedule |
| `make ops-status` / `ops-stop` / `ops-uninstall` | Inspect / stop / remove |
| `make storage-plan FROM=… TO=…` | Project the disk cost of a historical expansion |
| `make history-plan` / `make history FROM=… TO=…` | Report / run a resumable historical backfill |
| `make backtest FROM=… TO=…` | Replay history through the production scanner |
| `make backtest-status` / `backtest-report RUN=id` | Recent runs / one run's full metadata |
| `make outcomes` / `outcomes-status` | Compute outcome labels / label counts |
| `make research-calibration HORIZON=1d` | Outcome quality by score band (measurement only) |
| `make research-features` / `research-export` | Feature-vs-outcome tables / Parquet dataset |
| `make up` / `make down` | Start / stop the Docker stack |
| `make clean` | Remove caches and build artefacts |

---

## Running tests

```bash
make test
```

Tests are **fully deterministic and fully offline**. There are no network calls, no
sleeps, and no wall-clock dependence. Database tests run against SQLite in-memory by
default, so no container is required.

Notable test categories:

- `tests/unit/test_indicators.py` — indicators against hand-computed expected values.
- `tests/unit/test_no_lookahead.py` — a **property test over every registered feature**:
  truncating the candle series immediately after bar *i* must not change any feature
  value at bar *i*. Any feature that peeks into the future fails this automatically,
  including features added years from now.
- `tests/unit/test_adjustments.py` — split/dividend modelling; a 2-for-1 split must
  not appear as a −50% return in the adjusted series.
- `tests/unit/test_split_features.py` — a split must not create return, volatility,
  ATR or moving-average artefacts, and prefix invariance must survive adjustment.
- `tests/unit/test_simulation.py` — profile validation, position sizing, decision
  gates, and the fixed-fee impact at €50 vs €5000.
- `tests/unit/test_paper_exits.py` — stop/target rules, **same-bar ambiguity**
  (a bar touching both must resolve to the stop) and **gap fills** (a stop at 100
  fills at 95 when the market opens there).
- `tests/integration/test_paper_lifecycle.py` — the full lifecycle, plus
  **no-look-ahead** (a fill on the signal bar raises), portfolio isolation,
  idempotent replay, restart recovery and transactional rollback.
- `tests/unit/test_costs.py` — spread arithmetic and round-trip cost accounting.
- `tests/unit/test_signals.py` — component scoring, classification boundaries, weights.
- `tests/integration/` — repository and ingestion behaviour against a real database.
- `tests/api/` — endpoint contracts via `httpx.ASGITransport`.

---

## Database migrations

```bash
make migration m="add dividend adjustments"   # autogenerate from model changes
make migrate                                  # apply
make downgrade                                # roll back one revision
```

Always read generated migrations before applying them. Alembic autogenerate does not
detect column type changes reliably and will happily generate a destructive downgrade.

### TimescaleDB

The `candles` table is converted into a hypertable partitioned on `timestamp` by
migration `0002`. That migration **degrades gracefully**: if the `timescaledb`
extension is unavailable (plain PostgreSQL, SQLite), it logs and skips, leaving a
perfectly functional regular table. Nothing in the application logic depends on
Timescale being present.

---

## API endpoints

All under the `/api/v1` prefix. Interactive docs at `/docs`, schema at
`/openapi.json`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + database connectivity |
| `GET` | `/health/market-data` | Provider configuration and data freshness (credential-free) |
| `GET` | `/health/notifications` | Notification delivery status (never exposes a webhook) |
| `GET` | `/api/v1/scanner/status` | Scanner configuration, session phase and last run |
| `GET` | `/api/v1/scanner/candidates` | Ranked current candidates |
| `GET` | `/api/v1/signals/active` | Active tracked signals |
| `GET` | `/api/v1/signals/{id}` | One tracked signal |
| `GET` | `/api/v1/signals/{id}/evaluations` | A signal's evaluation history |
| `GET` | `/api/v1/instruments` | List instruments (filter by exchange, asset type) |
| `GET` | `/api/v1/instruments/{symbol}` | Instrument detail |
| `GET` | `/api/v1/instruments/{symbol}/candles` | OHLCV in a time window |
| `GET` | `/api/v1/instruments/{symbol}/features` | Computed feature series |
| `GET` | `/api/v1/instruments/{symbol}/features/latest` | Fully warmed-up feature snapshot |
| `GET` | `/api/v1/instruments/{symbol}/signal` | Explainable signal for a horizon |
| `GET` | `/api/v1/instruments/{symbol}/quote` | Latest quote with spread metrics |
| `GET` | `/api/v1/instruments/{symbol}/corporate-actions` | Splits and dividends |
| `GET` | `/api/v1/universe` | Instruments tradable at a point in time |
| `GET` | `/api/v1/simulation/profiles` | Configured virtual portfolios |
| `GET` | `/api/v1/simulation/profiles/{name}` | One portfolio's configuration |
| `GET` | `/api/v1/simulation/overview` | Every virtual portfolio side by side |
| `GET` | `/api/v1/simulation/profiles/{name}/portfolio` | Cash, equity, exposure, drawdown |
| `GET` | `/api/v1/simulation/profiles/{name}/positions` | Virtual positions |
| `GET` | `/api/v1/simulation/profiles/{name}/orders` | Orders, including rejections |
| `GET` | `/api/v1/simulation/profiles/{name}/trades` | Completed round trips |
| `GET` | `/api/v1/simulation/profiles/{name}/performance` | Derived performance summary |
| `POST` | `/api/v1/admin/sync` | Ingest instruments, actions and candles |

The feature and signal endpoints take an `adjustment` parameter
(`RAW` | `SPLIT_ADJUSTED` | `TOTAL_RETURN`), defaulting to `SPLIT_ADJUSTED`.

---

## Current limitations

These are known and deliberate, not oversights:

1. **Real data is ingested but unvalidated as a strategy.** Alpaca provides real
   US equity bars, quotes and corporate actions. That proves *ingestion
   correctness*, not *predictive edge* — see [docs/market-data.md](docs/market-data.md).
   `MockMarketDataProvider` remains and remains mandatory: every test depends on
   it, and it keeps the suite offline and deterministic.
2. **Corporate actions cover splits and dividends only.** Spin-offs, mergers and
   symbol changes can be *recorded* but are not adjusted for. See
   [docs/data-adjustments.md](docs/data-adjustments.md).
3. **Free-tier data is one venue.** The default Alpaca feed is IEX, roughly 2-3%
   of consolidated volume. Volume-based signal components computed on it measure
   IEX, not the market. See [docs/providers/alpaca.md](docs/providers/alpaca.md).
4. **Signal weights are arbitrary.** They are legible guesses, marked as such
   everywhere they appear. Do not read meaning into a score of 42. The phase 5
   benchmark found outcome quality is **not** monotonic in score — and the sample
   above the 75 threshold was 27 observations, far too few to justify a change.
5. **Backtests are not survivorship-bias-free.** Instruments carry no
   `listed_at`/`delisted_at`, so a historical universe resolves to today's
   survivors. See [docs/backtesting.md](docs/backtesting.md).
6. **Every historical transaction cost is modelled, never observed.** No
   historical quotes are stored, so backtested costs are a versioned assumption
   labelled `MODELLED`. See [docs/data-quality.md](docs/data-quality.md).
7. **Provider history is a rolling ~6-year window.** Alpaca serves this
   account back to 2020-07-27 and no further, and that floor advances daily.
   Bars that age out are gone for good, which is why raw candles are treated as
   irreplaceable. See [docs/historical-expansion.md](docs/historical-expansion.md).
5. **No backtester.** Only the data structures and protocols exist. Any claim about
   historical performance is currently unsupported.
6. **No machine learning.** By design — see [docs/ml.md](docs/ml.md).
7. **Single-venue symbols.** `symbol` is globally unique; multi-venue listings need a
   `(symbol, exchange)` key change.
8. **One timeframe evaluated.** Horizons are modelled explicitly throughout, but only
   daily-bar signals are computed today.
9. **Paper trading is long-only, market-orders-only.** Shorts and limit orders are
   refused rather than approximated. No partial fills, no liquidity model.
10. **Historical spreads are assumed, not measured.** Bars carry no quote, so a
    replay applies the configured spread symmetrically. A real spread widens
    exactly when it matters most, and this one does not.
11. **The baseline engine rarely reaches the 75 threshold.** With flat volume,
    momentum and trend saturate near 97-99 and the score still tops out around
    63 — half the weight sits in volume, volatility, regime and spread. Reaching
    75 needs a volume step change as well as a trend, so expect few qualifying
    signals. See [docs/signal-lifecycle.md](docs/signal-lifecycle.md#thresholds).
12. **Notification delivery is at-most-once.** A failed alert is recorded but not
    automatically resent; a failed *signal* alert is retried by the next
    evaluation, a failed system alert is not. No transactional outbox — see
    [docs/notifications.md](docs/notifications.md).
13. **Lifecycle, not index membership.** The universe answers "was this listed?",
    not "was this in the DAX in 2019?".
14. **No backtester.** The paper engine runs forward through bars; it is not a
    historical strategy evaluator with walk-forward validation.

---

## Roadmap

| Phase | Focus | Status |
|---|---|---|
| 1 | Foundation and deterministic market analysis | ✅ complete |
| 2 | Data integrity + simulation domain | ✅ complete |
| 3 | Multi-profile paper-trading engine | ✅ complete |
| 3b | Real market-data provider integration | ✅ complete |
| 3.5 | Discord notification infrastructure | ✅ complete |
| 4 | Continuous scanner + signal lifecycle | ✅ complete |
| 4.1 | Local operations + portfolio-aware Discord | ✅ this release |
| 5 | Backtesting engine | planned |
| 6 | Spread and execution-cost calibration | planned |
| 7 | Machine-learning baseline | planned |
| 8 | Walk-forward ML evaluation | planned |
| 9 | Web dashboard | planned |
| 10 | Alerts and continuous monitoring | planned |

Details, entry criteria and explicit non-goals: [docs/roadmap.md](docs/roadmap.md).

---

## Documentation

- [docs/architecture.md](docs/architecture.md) — module boundaries, data flow, key decisions
- [docs/market-data.md](docs/market-data.md) — real data ingestion, calendars, quotes, scheduling
- [docs/providers/alpaca.md](docs/providers/alpaca.md) — the Alpaca integration, feeds, retries, credentials
- [docs/data-quality.md](docs/data-quality.md) — validation rules, gaps, and why nothing is repaired
- [docs/notifications.md](docs/notifications.md) — event routing, thresholds, and database-vs-Discord
- [docs/discord.md](docs/discord.md) — channels, webhook security, retries, message limits
- [docs/scanner.md](docs/scanner.md) — the continuous scanner, universe, timeframes, locking
- [docs/signal-lifecycle.md](docs/signal-lifecycle.md) — signal identity and state transitions
- [docs/ml-dataset.md](docs/ml-dataset.md) — what is collected for a future model, and why
- [docs/operations.md](docs/operations.md) — running continuously, scheduling, troubleshooting
- [docs/macos-launchd.md](docs/macos-launchd.md) — launchd setup, sleep/wake, logs
- [docs/multi-user-roadmap.md](docs/multi-user-roadmap.md) — the ownership boundary, and what is *not* built
- [docs/provider-connections.md](docs/provider-connections.md) — credentials, references, future OAuth
- [docs/data-adjustments.md](docs/data-adjustments.md) — corporate actions, raw vs adjusted prices
- [docs/simulation-design.md](docs/simulation-design.md) — multi-profile simulation and feedback
- [docs/paper-trading.md](docs/paper-trading.md) — order/position lifecycle, accounting, exits, gaps
- [docs/simulation-timing.md](docs/simulation-timing.md) — execution timing and no-look-ahead rules
- [docs/roadmap.md](docs/roadmap.md) — phased plan
- [docs/storage-planning.md](docs/storage-planning.md) — measured bytes/row, growth projections, SQLite limits
- [docs/historical-expansion.md](docs/historical-expansion.md) — provider depth, chunking, resume, gap classification
- [docs/backtesting.md](docs/backtesting.md) — the replay engine, execution convention, bias constraints
- [docs/outcome-labels.md](docs/outcome-labels.md) — horizons, MFE/MAE, barriers, same-bar ambiguity
- [docs/research-dataset.md](docs/research-dataset.md) — the exported dataset, columns, sampling policy
- [docs/ml-readiness.md](docs/ml-readiness.md) — why a random train/test split is invalid here
- [docs/ml.md](docs/ml.md) — how models will be introduced, and the evaluation rules

---

## Disclaimer

tradabot is a research tool. It does not provide financial advice, and it does not
execute trades. Markets are adversarial, mostly efficient, and expensive to be wrong
in. Treat every number this system produces as a hypothesis awaiting falsification.
