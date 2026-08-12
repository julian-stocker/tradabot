# Storage planning

Every number here was **measured against the live database** with `dbstat`, not
derived from row widths. Theoretical estimates miss B-tree overhead, page slack
and index duplication, and they assume the provider returns a bar for every
calendar slot. It does not.

```bash
make storage-plan FROM=2020-07-27 TO=2026-08-11
python -m app.cli research storage-plan --from 2024-01-01 --to 2026-08-11 --cadence 26
```

## Measured cost per row

| Entity | Rows measured | Heap | Indexes | **Bytes/row** |
|---|---|---|---|---|
| Candle | 162,378 | 33.3 MB | 14.5 MB | **295** |
| SignalEvaluation | 4,324 | 17.8 MB | 0.5 MB | **4,218** |
| SignalOutcome | 29,414 | 5.7 MB | 2.1 MB | **263** |
| TradeOutcome | 192 | 70 KB | 33 KB | **533** |

### The number that drives everything

**A `SignalEvaluation` costs fourteen candles.** It stores four timeframes of
assessment plus five metric blobs as JSON — `timeframe_states` alone carries a
nested structure-metrics object per timeframe. That was a deliberate phase-4
choice so a future model could inspect the full context; this is the price.

The consequence is counter-intuitive and it shapes the whole plan: **scanning the
market data costs more than storing it.** A decade of five-minute bars for 52
symbols is a few gigabytes; scanning that decade every fifteen minutes and
keeping the result is over ten. That is why raw expansion and research
materialisation are separate stages.

Candle index overhead is ~30% of total size, which is not waste: the composite
primary key `(instrument_id, timeframe, timestamp)` *is* the range-scan index
every backtest query uses.

## Measured bars per session

Per symbol, as Alpaca actually delivers — including extended hours, and excluding
slots where nothing traded:

| Timeframe | Bars/symbol/session |
|---|---|
| 5m | 73.89 |
| 15m | 26.90 |
| 1h | 7.29 |
| 1d | 1.00 |

A regular session has 78 five-minute slots, but IEX prints no bar for a slot with
no trades, so using 78 would overstate M5 volume. Five-minute bars are ~68% of
all candle rows.

## Projections — 52 symbols, all four timeframes

| From | Sessions | Candle rows | Raw | Evaluations¹ | Outcomes | Research | Total |
|---|---|---|---|---|---|---|---|
| 2025-01-01 | 402 | 2.28 M | 0.63 GB | 125 k | 878 k | 0.71 GB | 1.34 GB |
| 2024-01-01 | 654 | 3.71 M | 1.02 GB | 204 k | 1.43 M | 1.15 GB | 2.17 GB |
| 2023-01-01 | 904 | 5.13 M | 1.41 GB | 282 k | 1.97 M | 1.59 GB | 3.00 GB |
| 2022-01-01 | 1,155 | 6.55 M | 1.80 GB | 360 k | 2.52 M | 2.04 GB | 3.84 GB |
| 2020-01-01 | 1,660 | 9.42 M | 2.59 GB | 518 k | 3.63 M | 2.93 GB | 5.51 GB |
| 2018-01-01 | 2,163 | 12.3 M | 3.37 GB | 675 k | 4.72 M | 3.81 GB | 7.18 GB |
| 2016-01-01 | 2,666 | 15.1 M | 4.15 GB | 832 k | 5.82 M | 4.70 GB | 8.85 GB |

¹ at hourly cadence (6/session). At the **production 15-minute cadence** the
research side more than quadruples:

| Cadence | From 2020 | Evaluations | Outcomes | Research | Total |
|---|---|---|---|---|---|
| hourly (6/session) | 2020 | 518 k | 3.63 M | 2.93 GB | 5.51 GB |
| 15-minute (26/session) | 2020 | 2.24 M | 15.7 M | 12.68 GB | 15.27 GB |

LOW/EXPECTED/HIGH bounds are ×0.85 / ×1.0 / ×1.30. The upper bound is further
from centre than the lower on purpose: overshooting a budget is recoverable,
running out of disk mid-write is not.

## The disk gate

Three guards, all of which must pass:

1. the projected data,
2. **×2 working headroom** — WAL grows before checkpointing, `VACUUM` rewrites
   the file alongside the original, an export materialises a second copy,
3. **20 GB still free afterwards.**

`SAFE` / `WARNING` (fits but leaves under 40 GB) / `UNSAFE` (refused). The 20 GB
floor is not a technical limit — it is someone's laptop, which needs room for
swap, snapshots and updates. A tool that fills a personal machine has caused a
bigger problem than the one it solved.

Current machine: **314.3 GB free of 465.6 GB** after the expansion. Every range in
the table above is `SAFE`.

## Is SQLite still the right choice?

**Yes, for this stage.** Measured evidence:

| Factor | Measurement | Verdict |
|---|---|---|
| Database size | 71 MB → **823 MB** after expansion (2.86 M candles) | Fine; SQLite handles hundreds of GB |
| Analytical query | `AVG(raw_return)` over 3,744 labelled rows: **0.27 s** | Fine |
| Concurrent writes | One writer at a time (WAL); production writes every 5 min for seconds | Workable with `busy_timeout=5000` |
| Backfill throughput | 86,592 bars inserted in ~35 s | Fine |
| Backup | Single file copy | Simpler than PostgreSQL |
| Deployment | No server process | Suits a laptop or a Raspberry Pi |

### When to move to PostgreSQL/TimescaleDB

Migrate when **any** of these becomes true — not before, because the migration
costs real effort and buys nothing until then:

1. **Research materialisation at 15-minute cadence over multiple years.** 15.7 M
   outcome rows and 2.2 M evaluations with JSON blobs is where SQLite's
   single-writer model and lack of parallel query start to hurt.
2. **A second concurrent writer.** Today the scheduler is the only one, and the
   backfill politely takes short locks. Add a web UI that writes, or a second
   scanner, and WAL's one-writer rule becomes the bottleneck.
3. **Analytical queries exceeding a few seconds.** TimescaleDB's hypertables,
   compression and parallel aggregation are built for exactly the
   `candles`-shaped workload.
4. **A server or multi-machine deployment.** SQLite over a network filesystem is
   a corruption risk, not a configuration option.

Until then SQLite is the *better* choice: one file, no daemon, trivial backup,
and `docker compose` already carries a PostgreSQL definition for when it changes.

## Raw vs derived

| Class | Tables | Regenerable? |
|---|---|---|
| **RAW / SOURCE** | `instruments`, `candles` (+ provider provenance), `corporate_actions` | **No** |
| **DERIVED** | `signal_evaluations` (backtest), `signal_outcomes`, `trade_outcomes`, `backtest_runs`, exports | Yes — from raw + versioned code |

Derived data can be rebuilt from immutable raw data plus the recorded
`feature_set_version` / `signal_model_version` / `scanner_policy_version` /
`cost_model_version` / `label_policy_version`. That is what makes future cleanup
possible without destroying anything irreplaceable.

**Raw data is irreplaceable for a reason specific to this provider.** The account's
history is a *rolling* ~6-year window (see docs/historical-expansion.md), so bars
that age out cannot be re-fetched at any price. A candle downloaded today is the
only copy that will ever exist locally.

No automatic deletion is implemented. This section exists so that when it is, it
deletes the right side.

## Parquet archival

Measured on a real 3,120-row export:

| Format | Bytes/row | vs SQLite |
|---|---|---|
| SQLite (evaluation + outcome) | 4,481 | 1.0x |
| CSV (exported columns) | 448 | 10x smaller |
| **Parquet (exported columns)** | **88** | **51x smaller** |

Two honest caveats. The 51x figure is **not like-for-like**: the export carries a
subset of the evaluation's columns, and SQLite additionally stores five JSON
metric blobs which are most of its 4.2 KB. The like-for-like columnar compression
figure is Parquet against CSV of the same columns — **5x**.

Both matter, and they answer different questions: 51x sizes an archive, 5x
describes the format. Dictionary encoding on the repeated strings (symbol,
sector, session, status, five version fields) does most of the work.

The plausible future architecture — **not** implemented, and not needed yet:

- SQLite/PostgreSQL keeps operational state: instruments, watchlist, portfolios,
  scan runs, recent evaluations.
- Large immutable research datasets are archived as Parquet, partitioned by year,
  and read directly by analysis tools.

That split only pays once research materialisation runs to millions of rows.
Today the whole database is 71 MB and moving it would be premature.

## Storing every timeframe vs deriving from 5m

Resampling stored 5-minute bars reproduces Alpaca's own bars **exactly** —
416/416 fifteen-minute and 115/115 hourly, OHLC *and* volume (`app.market_data.gaps.resample`).
So deriving is technically sound and would remove ~31% of candle rows.

**Recommendation: keep storing all four.** The saving is not worth what it costs:

| | Store each | Canonical 5m + derive |
|---|---|---|
| Storage | ~31% more rows | baseline |
| Provider fidelity | exactly what the provider said | our arithmetic in the lineage |
| Reproducibility | direct | depends on retaining 5m forever |
| Backtest query cost | one row per H1 bar | **12 rows** per H1 bar, aggregated per query |
| Corporate actions | adjusted on read, once | adjusted on read, after resampling |
| ML | native series available | must resample before every experiment |

The backtest's primary timeframe is H1, so deriving it would multiply the I/O of
the single hottest query path by twelve — to save gigabytes on a disk with 315 GB
free. Storage is not the binding constraint; provider depth is.

Revisit if the disk gate ever returns `WARNING`. The resampler is tested and
ready, so this is a lever that can be pulled later, not a door that is closed.
