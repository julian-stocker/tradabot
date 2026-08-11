# Continuous scanner

Evaluates a watchlist on a schedule, records **every** observation, and surfaces
only the few worth interrupting a human for.

> **The product principle this whole phase is subordinate to.**
> The goal is not to generate signals. It is to collect honest observations and
> surface only potentially interesting setups. **Zero qualified signals is a
> valid, common and correct result.** Nothing in tradabot lowers a threshold to
> produce activity, and a busy Discord channel is evidence of a threshold, not of
> an opportunity.

---

## The flow

```
watchlist → incremental sync → multi-timeframe analysis → candidate evaluation
                                                                  ↓
                                                    SignalEvaluation  (X, always)
                                                                  ↓
                                                    TrackedSignal lifecycle
                                                          ↓             ↓
                                                     ranking      paper decisions
                                                          ↓             ↓
                                                     Discord (selected events only)
```

## Commands

```bash
tradabot watchlist seed                 # the initial universe
tradabot watchlist list
tradabot watchlist add NVDA
tradabot watchlist disable NVDA
tradabot watchlist enable NVDA

tradabot scanner sync                   # incremental market data
tradabot scanner run-once               # one full cycle
tradabot scanner run-once --no-paper    # skip paper decisions
tradabot scanner candidates --limit 5   # ranked, current
tradabot scanner overview               # send the ranked overview
tradabot scanner daily-summary          # send the daily report
tradabot scanner status
tradabot scanner demo                   # deterministic, offline
```

API (read-only — nothing here can start a scan):

```
GET /api/v1/scanner/status
GET /api/v1/scanner/candidates?limit=5
GET /api/v1/signals/active
GET /api/v1/signals/{id}
GET /api/v1/signals/{id}/evaluations
```

## The initial universe

**52 liquid US equities across nine sectors** — see
`app/scanner/universe.py` for the full list.

| Sector | Symbols |
|---|---|
| Technology | AAPL, MSFT, GOOGL, ORCL, CRM, ADBE |
| Semiconductors | NVDA, AMD, INTC, AVGO, QCOM, TXN, MU |
| Communication | META, NFLX, DIS, T, VZ |
| Consumer discretionary | AMZN, TSLA, HD, MCD, NKE, SBUX |
| Financials | JPM, BAC, GS, MS, V, MA, BRK.B |
| Healthcare | UNH, JNJ, LLY, PFE, ABBV, MRK |
| Industrials | CAT, BA, HON, GE, UPS, LMT |
| Energy | XOM, CVX, COP, SLB |
| Consumer staples | PG, KO, PEP, WMT, COST |

> **Inclusion is not an investment recommendation.** These are development
> fixtures chosen for liquidity and sector spread. Nothing in tradabot reads this
> list to decide anything.

**Why fifty and not five hundred.** Scanning a large universe for "score > 75"
returns hits every day whether or not the signal predicts anything — the
multiple-comparisons hazard recorded in `app/scanner/models.py` since phase 1.
Fifty keeps the base rate interpretable and a cycle fast on a laptop.

**Why multiple sectors.** A technology-only watchlist is one bet with fifty
tickers on it: when the sector moves, everything qualifies at once and the
scanner looks like it found fifty opportunities when it found one.

The watchlist is a **database table**, not code. Seeding is idempotent, and a
symbol the provider does not serve is **named** rather than silently dropped.

## Timeframes

Four, with explicit and different roles. **Not an average** — averaging four
scores discards the only thing worth computing them for.

| Timeframe | Role | Question |
|---|---|---|
| `1d` | macro | Is this in an uptrend at all? |
| `1h` | primary | Where the setup is identified |
| `15m` | confirmation | Does structure support the 1h read? |
| `5m` | entry | Immediate momentum and volume |

These two situations average alike and mean opposite things:

```
1d UP    1h UP    15m UP    5m UP       → aligned setup
1d DOWN  1h SIDE  15m UP    5m UP       → a bounce in a downtrend
```

The second is the most common way a short-timeframe signal loses money. So the
**macro timeframe cannot be outvoted**: a directional read requires 1d not to
oppose it, and the second case reports direction 0.

**Unknown is not neutral.** A timeframe without enough history is `UNKNOWN` and
is excluded from the agreement denominator — counting it would let an instrument
with less history look more "agreed" than one with more.

## What is measured

Everything except structure is reused from `app/features/registry.py`; no
indicator is reimplemented.

| Group | Metrics |
|---|---|
| Trend | EMA20/50 spread, EMA relationships, trend state |
| Momentum | RSI-14 |
| Volume | `rel_volume_20`, volume confirmation |
| Volatility | ATR-14, `atr_pct_14`, realised volatility |
| **Structure** (new) | higher highs/lows, lower highs/lows, breakout, breakdown, consolidation, support, resistance, distance to high/low, range position |
| Liquidity | spread, spread bps, quote age |

Every structure definition is arithmetic on OHLCV, stated in
`app/scanner/structure.py`. **No subjective chart patterns**: a swing high is a
bar exceeding the `k` bars either side, and only *confirmed* swings are reported
— an unconfirmed one at the last bar would be look-ahead wearing a chart
pattern's costume.

## Data quality and staleness

Staleness tolerance **scales with the timeframe**, and this is not a detail: a
daily bar is by definition up to a day old, so a flat 30-minute limit marks every
daily series permanently stale, drags the whole context down (quality is the
worst of the four), and the scanner silently never qualifies anything.

```
tolerance = max(TRADABOT_SCANNER__MAX_DATA_AGE_MINUTES, 3 × bar interval)
```

Stale or insufficient data can **downgrade** a signal but never **promote** one.
A setup that only looks qualified on stale data is not qualified. The evaluation
is still persisted, with its data-quality state — a stale observation is a real
observation about the feed.

## Market hours

| Phase | New qualifications? |
|---|---|
| `REGULAR` | yes |
| `PRE_MARKET`, `AFTER_HOURS` | no |
| `CLOSED`, `WEEKEND`, `HOLIDAY` | no |

Configurable via `TRADABOT_SCANNER__REQUIRE_REGULAR_SESSION`.

The reason is the data. The default IEX feed carries a small fraction of
consolidated volume, and in extended hours that fraction is smaller and spreads
much wider. Relative volume, spread and breakout confirmation mean different
things at 08:00 than at 15:00, and none of the thresholds were chosen for the
former — a scanner qualifying on pre-market IEX prints would be measuring the
feed's thinness.

**Evaluations are still recorded outside the session.** Weekend and holiday are
reported distinctly from "closed" so a genuine feed failure is not
indistinguishable from Sunday.

## The cycle

`run_scan_cycle(as_of)` does one pass and returns statistics. It does **not**
sleep, loop or schedule itself.

Per **symbol**, not per cycle:

```
calculate → persist → COMMIT → notify
```

- One symbol's failure rolls back only that symbol.
- Discord cannot roll back data: notification happens after the commit.
- A crash loses at most one symbol's work.

## Locking

A database-backed **lease** in `scan_runs`. A second invocation — from cron, a
restarted process, another machine — returns immediately rather than scanning
concurrently.

Leases **expire** (`TRADABOT_SCANNER__LEASE_SECONDS`, default 900). A process
killed mid-cycle leaves a `running` row; without expiry the scanner would be
locked out until someone noticed and cleared it by hand, at exactly the moment
nobody is watching. An expired lease is taken over and the old run marked failed.

## Metrics

Every cycle returns and persists:

```
symbols_total / synced / evaluated / skipped / failed
candidates_discovered, signals_qualified, signals_strong
paper_decisions, positions_opened, positions_closed
duration, hit_rate
```

`hit_rate` is printed with every scan because it is the **base rate**. If it is
routinely high the threshold is not selective and the "hits" are just the market.

## Performance

- Incremental sync only; a full re-download never happens on a schedule.
- **Backfill windows scale with the timeframe** — 20 days of 5-minute bars, 400
  of daily. Pulling 400 days of 5-minute bars is 30,000 rows to compute an answer
  that needs sixty.
- Feature lookback is bounded (warm-up + margin), structure to 60 bars.
- Candle upserts are chunked to stay under SQLite's bound-parameter ceiling.

## SQLite

Phase 4 works on SQLite and needs no PostgreSQL. Expected volume at 50 symbols,
26 scans a day:

| | Rows/day | Rows/year |
|---|---|---|
| `signal_evaluations` | ~1,300 | ~475,000 |
| Candles (4 timeframes) | ~4,000 | ~1.5M |

SQLite handles that. **Move to PostgreSQL/TimescaleDB when** any of these
becomes true: the universe grows past ~200 symbols, the scanner runs
concurrently with API reads under load, retention passes about two years, or you
want the `candles` hypertable's time-partitioning. See
[operations.md](operations.md).

## Related

- [signal-lifecycle.md](signal-lifecycle.md) — identity and state transitions
- [ml-dataset.md](ml-dataset.md) — what is stored and why
- [operations.md](operations.md) — scheduling
- [notifications.md](notifications.md) — what reaches Discord
