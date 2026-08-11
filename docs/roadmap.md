# Roadmap

Each phase lists **entry criteria** (what must be true before starting) and **exit
criteria** (what must be demonstrably true before it counts as done). Phases are
ordered by dependency, not ambition.

The recurring principle: *no phase may produce a number that a later phase would have
to retract.* Capability that cannot yet be evaluated honestly is deferred rather than
shipped with a disclaimer.

---

## Phase 1 — Foundation and deterministic market analysis ✅

**Status:** complete.

Vertical slice: market data → normalised storage → features → signal → API.

**Delivered**
- Modular monolith, strict module boundaries, strict typing throughout
- Instrument + OHLCV schema with `Decimal` prices and UTC-aware timestamps
- `MarketDataProvider` protocol + deterministic `MockMarketDataProvider`
- Quote model with spread arithmetic; configurable round-trip cost model
- Polars feature engine with a registry and verified warm-up declarations
- Explainable rule-based signal engine with directional/quality separation
- Cost-aware `NetEdge` on every signal
- Explicit `Horizon` modelling across short/medium/long term
- FastAPI endpoints with OpenAPI docs
- 234 deterministic offline tests, including prefix-invariance over every feature
- Docker + TimescaleDB compose stack; Alembic migrations with no model drift

**Explicit non-goals met:** no ML, no backtester, no order execution, no broker API.

---

## Phase 2 — Data integrity and simulation domain ✅

**Status:** complete.

Correct historical data, and the domain model multi-profile paper trading needs.

**Delivered**
- Instrument lifecycle (`listed_at` / `delisted_at`) with a half-open
  `is_tradable_at()` rule, and `UniverseService` for point-in-time queries
- Corporate actions in one extensible table: splits and cash dividends adjusted
  for, four more types recordable without a migration
- Explicit `RAW` / `SPLIT_ADJUSTED` / `TOTAL_RETURN` series, adjustment computed
  on read so raw provider data is never overwritten
- Adjustment applied at the `FeatureService` boundary; no indicator has an
  adjustment opinion
- Persisted signals, so one signal can fan out to many profile decisions
- `BrokerCostProfile` / `RiskProfile` / `SimulationProfile` — normalised so nine
  portfolios share three risk rows
- `TradeDecision` records, including **rejected** signals with the economics that
  caused the rejection
- `Broker` protocol (interfaces only)
- 166 new tests; 400 total

**Explicit non-goals met:** no ML, no paper-trading engine, no order execution, no
real provider, no scanner.

**Proved by measurement:** on a window spanning NVDA's 10-for-1 split, the raw
series scores NEUTRAL on a −88% 20-day return; the adjusted series scores BULLISH
on +18%. And one signal at score 62 produces TRADE for three €5000 portfolios and
SKIP for six smaller ones, purely because a €1.00 fee is 4012 bps of a €5 position.

---

## Phase 3 — Multi-profile paper-trading engine ✅

**Status:** complete.

The full virtual lifecycle: signal → decision → order → fill → position →
monitoring → exit → realised P&L → performance.

**Delivered**
- `PaperBroker` implementing the phase 2 `Broker` protocol, with execution gates
  distinct from decision gates (`OrderRejectionReason` vs `DecisionReason`)
- Per-leg fill pricing: buys from the ask, sells from the bid, slippage always
  adverse — reconciling exactly with the phase 1 round-trip cost model
- Risk-based position sizing capped by position, cash and exposure limits, with
  the binding constraint recorded on every order
- Persistent portfolios, orders, positions, trades and equity snapshots; the
  database is the source of truth and the engine holds no state between calls
- Conservative same-bar ambiguity policy and realistic gap fills, both flagged on
  the position so results resting on a guess are identifiable
- **No-look-ahead enforced structurally**: executing at or before the signal bar
  raises `LookAheadError`
- Idempotent replay via UNIQUE idempotency keys; restart recovery with no special
  case; transactional atomicity across order, cash and position
- Counterfactual `decision_outcomes` for SKIP decisions
- Read-only API and a deterministic `make demo-simulation`
- 123 new tests; 523 total

**Explicit non-goals met:** no live trading, no broker APIs, no ML, no scanner, no
strategy self-modification.

**Demonstrated:** one STRONG_BULLISH signal opens 6 positions across 9 portfolios.
All three €50 portfolios decline it — at the *decision* stage, because a €1.00 fee
is 415 bps of a €50 round trip and destroys the expected edge.

**Deliberate limitations:** long-only, market orders only, no partial fills, no
liquidity model. Each is refused explicitly rather than approximated.
(`max_daily_loss` was stored but unenforced here; phase 3b enforced it.)

---

## Phase 3b — Real market-data provider integration ✅

**Entry:** phase 3 complete. Was "phase 2b"; renumbered because the paper engine
landed first.

The adjustment layer (phase 2) and the execution engine (phase 3) both exist and
are tested against synthetic data. Everything downstream now needs *real* prices:
a backtest, a scanner and an ML baseline built on a random number generator
measure nothing.

**Scope**
- One real provider adapter behind the existing protocol, with its
  corporate-action feed
- Exchange calendars — real sessions, holidays, half-days, DST. Unlocks
  `max_daily_loss` (needs a session boundary) and replaces the bar-counted
  holding period with real trading days
- Backfill of `listed_at` / `delisted_at` from provider reference data
- Rate limiting, retry with backoff, provider-error taxonomy
- Data-quality checks: gap detection, duplicate bars, impossible prices, stale quotes
- Announcement dates on corporate actions, replacing the effective-date proxy

**Delivered:** Alpaca (`alpaca-py`, market data only) behind the existing
protocol; `exchange-calendars` sessions/holidays/half-days; calendar-aware gap
detection; bounded retry with jittered backoff and `Retry-After`; provenance on
every stored bar; split adjustment for *open positions*; session-based
`max_daily_loss` and trading-day holding periods; a credential-free health
endpoint; CLI import/sync/quote/status and a real-data replay command.

Two providers coexist and are switchable by configuration alone. The paper engine
runs on real data with **no change to phase 3 code** — `simulate` composes the
existing engine over stored candles.

**Not delivered, and deferred deliberately:**
- Reconciliation of adjusted prices against an independent source. It needs a
  second data vendor, which is phase 6's problem, not a checkbox this phase can
  honestly tick.
- `listed_at` / `delisted_at` backfill. Alpaca's asset catalogue is behind the
  *trading* API, and requesting a trading credential for a market-data tool asks
  for more access than the job needs.
- Announcement dates on corporate actions. The provider does not supply them, so
  the effective-date proxy stands and remains documented as conservative.

**The distinction this phase turns on:** real data proves **ingestion
correctness**, not **predictive edge**. Nothing here makes a backtest valid; it
makes one possible.

---

## Phase 4.1 — Local operations and portfolio-aware Discord ✅

**Entry:** phase 4.

**Delivered:** three personal paper portfolios (100 / 1000 / 10000 EUR, balanced,
completely isolated) each with its own Discord channel; routing by persistent
portfolio identity rather than message content or capital; a `TradabotUser`
ownership boundary and an `ExternalAccountConnection` record that stores a
credential *reference* and never a secret; macOS launchd generation with an
explicit, reversible install; an `ops check` pre-flight; SQLite WAL and busy
timeout for overlapping scheduled jobs; session-aware, deduplicated daily
summary.

**Not delivered, deliberately:** authentication, per-user watchlists, a Discord
bot, slash commands, Alpaca OAuth, any secret in the database, and any automatic
scheduler installation. `docs/multi-user-roadmap.md` and
`docs/provider-connections.md` describe those as future work and say plainly that
they do not exist.

**The honest limitation:** a sleeping laptop is not 24/7 monitoring. On wake the
data catches up and state survives, but a signal that qualified while the lid was
shut produces no retrospective notification -- announcing a two-hour-old entry as
if it were current would be worse than silence.

---

## Phase 3.5 — Discord notification infrastructure ✅

**Entry:** phase 3b.

The event boundary already exists: `app/core/events.py` defines the event types,
the `EventPublisher` protocol and a null implementation, and ingestion already
emits through it. Nothing delivers anything yet.

**Scope**
- A Discord transport implementing `EventPublisher`, attached at the composition
  root — not called from inside provider code
- Delivery failure is swallowed and logged, never propagated: a Discord outage
  must not fail a market-data sync
- Rate limiting and coalescing, so one bad night does not produce 400 messages
- Webhook URL from the environment, treated as a credential

**Delivered:** a provider-independent notification layer with Discord as its first
backend. `NotificationService` implements the existing `EventPublisher`, so no
domain service changed. Four channels routed by event category; transition-based
deduplication with configurable thresholds and cooldowns; bounded retry with
jitter and `Retry-After`; a persisted attempt audit; a credential-free health
endpoint; CLI `test` / `status` / `daily-summary`; and a console backend that
shares the formatters so the whole path is exercisable offline.

**Reliability, stated plainly:** at-most-once with an audit trail, **not** a
transactional outbox. An outbox needs a relay process, and scheduling is deferred
to the deployment environment by design. A failed *signal* alert is retried by
the next evaluation (its "announced" state is only committed on success); a
failed *system* alert is not. `docs/notifications.md` says exactly this rather
than implying a stronger guarantee.

**The constraint that outranks everything else here:** notification filtering
never affects persistence. A score-60 signal is never announced and is always
stored, and its forward outcome stays measurable. The database is the future ML
dataset, and shaping it by "what was worth posting to a chat channel" would bake
in a selection bias that later looks like signal.

**Not delivered, deliberately:** slash commands, an interactive bot, or any path
by which Discord could influence a decision. Discord is where tradabot writes what
it decided.

---

## Phase 4 — Continuous watchlist scanner and signal lifecycle ✅

**Entry:** phase 3.5.

The next phase, and the one that finally produces a dataset. Continuous
evaluation of **real** market data on a schedule, persisting every candidate it
evaluates and notifying only the high-value transitions the notification policy
already knows how to identify.

**Scope**
- Scheduled evaluation across the configured watchlist (`ScanRequest` /
  `ScanResult` are already defined)
- **Persist every evaluated candidate**, not only the qualifying ones — this is
  the whole point, and the reason it is worth doing before the backtester
- Signal lifecycle tracking: when a candidate qualified, strengthened, lapsed,
  and what happened afterwards
- Feed `MarketOverview` with real top-N candidates; the formatter exists and is
  currently given nothing
- Incremental computation so a repeat scan is not a full recompute

**Exit**
- A watchlist scan completes within an acceptable local time budget
- Every result reports `instruments_scanned` and `hit_rate`
- Discord volume stays legible over a full trading day — which is what the
  phase 3.5 thresholds exist to be tested against

**Delivered:** a persistent watchlist (52 symbols, 9 sectors, seeded from data);
four timeframes with explicit roles and measured agreement; price-structure
metrics; `SignalEvaluation` persistence for *every* candidate; a signal lifecycle
with stable identity; deterministic ranking; a database-backed scan lease;
scanner CLI and read-only API; and a deterministic offline demo.

**Four bugs the implementation surfaced**, each of which would have been silent:
SQLite's bound-parameter ceiling failing intraday backfills; a flat staleness
tolerance marking every *daily* series permanently stale (nothing would ever have
qualified); a demo that advanced the clock rather than the data, showing the 1h
series the same bars every phase; and a test suite that inherited the developer's
`.env` and broke when real webhooks were configured.

**What the engine actually does:** it cannot reach 75 on price trend alone. With
flat volume, momentum and trend saturate near 97-99 and the score tops out around
63 — half the weight sits in components a smooth trend leaves neutral. Reaching
75 needs a volume step change too. Expect few qualifying signals.

**Hazard, restated because it now applies:** scanning many instruments for
"score > 75" is a multiple-comparisons trap. At any plausible false-positive rate
it returns hits regardless of whether the signal predicts anything. `hit_rate` is
reported with every scan for this reason, and every evaluation is stored so
forward performance can be measured in phase 5. **A busy Discord channel is not
evidence of a working strategy** — it is evidence of a threshold.

---

## Phase 5 — Historical backtesting, outcome labels, research dataset ✅

**Delivered.** Migrations 0009-0010.

**Scope**
- `HistoricalReplay` over the phase-1 `DataFeed`/`ExecutionModel` protocols,
  reusing the **production** analyser, feature service and signal engine -- there
  is no separate backtest strategy
- Explicit event-time model: signal at bar close, fill at the next bar's open
- Outcome labels for 15m/1h/4h/1d/3d/5d/20d with MFE/MAE and barrier outcomes,
  in their own tables ([outcome-labels.md](outcome-labels.md))
- Versioned **modelled** historical transaction costs -- no historical quotes
  exist, so no backtested cost is ever `OBSERVED`
- `SpreadQuality`, separating quote sanity from bar staleness
- Deterministic Parquet export with a manifest
  ([research-dataset.md](research-dataset.md))
- Research isolation: backtest observations carry `backtest_run_id`, which every
  production read filters out, so a replay is safe while the scheduler runs

**Found and fixed:** a real look-ahead leak in `CandleRepository.get_latest`,
which filtered on bar *start* and so exposed partially formed bars to the live
scanner. See [backtesting.md](backtesting.md).

**Not delivered** (carried to phase 6)
- `listed_at`/`delisted_at`: the historical universe is still today's survivors,
  so this is **not** a survivorship-bias-free backtest
- Volume-capped fills
- A deliberately look-ahead-biased strategy asserted to score impossibly well
- The phase 1 heuristic weights are **not** revised -- the sample above the 75
  threshold is 27 observations, which cannot justify changing anything

**What the first benchmark showed:** outcome quality is **not** monotonic in
score, and the lowest-scoring band had the highest mean 5-day return over the
window. That is a finding to sit with, not a reason to move a threshold.

---

## Phase 6 — Spread and execution-cost calibration

**Entry:** phase 5, plus observed quote data.

**Scope**
- Historical quote/spread storage, so past spreads are reconstructable instead of
  assumed
- Spread modelling by time of day, volatility regime, and instrument liquidity
- Market-impact estimation for size relative to displayed volume
- Replace `CostSettings` defaults with measured values per broker
- Replace the invented `expected_move_capture_ratio` with a calibrated estimate

**Exit**
- Modelled costs reconcile against a sample of real fills
- Backtests re-run with calibrated costs, and the delta versus assumed costs reported

**Expect this phase to hurt.** Realistic costs are where marginal strategies die. That
is the phase working correctly, not failing.

---

## Phase 7 — Machine-learning baseline

**Entry:** phase 5 at minimum, ideally phase 6. See [ml.md](ml.md).

**Scope**
- Labelled dataset builder with purge gaps and cost-aware thresholds
- Logistic regression predicting `P(return > round-trip cost)`
- Calibration measurement (reliability curves, Brier score)
- Comparison against buy-and-hold and the rule-based signal

**Exit**
- The baseline beats all four control baselines out-of-sample, **or** the negative
  result is documented and ML is deferred

A negative result here is a genuine success. It is much cheaper to learn that the
features contain no exploitable signal than to deploy a model that believes they do.

---

## Phase 8 — Walk-forward ML evaluation

**Entry:** phase 7 showing signal.

**Scope**
- Walk-forward harness with expanding/rolling windows
- Gradient boosting (XGBoost / LightGBM), Optuna inside the walk-forward loop
- Per-fold stability analysis and regime-conditional performance
- Experiment tracking: parameters, seeds, data range, code version

**Exit**
- Performance stable across folds, not concentrated in one regime
- Calibrated probabilities feeding the existing `NetEdge` gate
- Number of configurations tried reported with every result

---

## Phase 9 — Web dashboard

**Entry:** phase 4; more useful after phase 5.

**Scope**
- Next.js frontend against the existing API
- Charts with feature and signal overlays
- Signal explanations surfaced prominently — reasons *and* risks
- Backtest result visualisation
- **Authentication**, before this is reachable from anywhere but localhost

**Exit**
- Every number displayed traceable to its inputs
- Uncertainty and cost shown alongside every signal, not buried in a detail view

---

## Phase 10 — Alerts and continuous monitoring

**Entry:** phase 3.

**Scope**
- Scheduled scans and continuous evaluation
- Alerting on high-conviction, cost-positive setups
- Data-freshness and pipeline-health monitoring
- **Signal-decay tracking** — measuring whether live performance still matches backtest

**Exit**
- Alerts are rare enough to be worth reading
- Automatic flagging when live performance diverges from expectation

The decay tracking matters most. A strategy that worked historically and stopped
working is the normal outcome, and noticing quickly is worth more than any single
improvement in the model.

---

## Permanent non-goals

Not scheduled, and not intended to be:

- **Automatic order execution.** No broker order placement, ever.
- **Unofficial broker APIs.** Including Trade Republic. Cost settings are entered
  manually as `BrokerCostProfile` rows.
- **Strategy self-modification.** The feedback system collects evidence; it never
  rewrites trading rules automatically. See
  [simulation-design.md](simulation-design.md#the-critical-constraint-on-feedback).
- **Guaranteed predictions.** Outputs are probabilistic and provisional.
- **Financial advice.**
- **Multi-tenant SaaS.** Local-first is a design choice, not a stepping stone.
