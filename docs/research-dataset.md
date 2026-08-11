# The research dataset

One row per `(evaluation, horizon)`: features known at `T`, context, and what the
market did afterwards. Exported as Parquet with a manifest.

```bash
make research-export                      # 1d horizon, regular session, Parquet
python -m app.cli research export --horizon 5d --format csv --out exports
```

## Column groups

Declared, never inferred. `app/research/export.py` holds all three lists and
`assert_no_leakage()` fails the export if they ever overlap.

| Group | Contents |
|---|---|
| **identity** | `evaluation_id`, `symbol`, `sector`, `reference_timestamp` |
| **X (features)** | `score`, `confidence`, `agreement`, `relative_volume`, `rsi`, `atr_pct`, `volatility`, `ema_spread_pct`, `expected_move_bps`, `cost_bps`, `net_edge_bps`, `spread_bps` |
| **Y (labels)** | `raw_return`, `mfe`, `mae`, `barrier_outcome`, `time_to_target_seconds`, `time_to_stop_seconds`, `future_price`, `future_timestamp`, `label_status` |
| **context** | session, quality, versions, and the split metadata below |

Every feature comes from a column that `_build_evaluation` filled from
information available at the evaluation instant. No label column is reachable
from the feature group, and a test asserts no feature name even *contains* a
future-sounding word.

## Why Parquet

It preserves dtypes and nulls. CSV cannot: a null label round-trips as an empty
string and then as `0.0`, silently converting "we do not know yet" into "the
market did nothing" -- precisely the corruption `LabelStatus` exists to prevent.
CSV is offered for inspection only. **Never use Excel as the canonical dataset.**

Polars writes Parquet natively, so this needed no new dependency.

## Determinism

Rows are ordered by `(reference_timestamp, evaluation_id)` and columns by the
declared group order, so two exports of the same database are identical. Asserted
in `tests/integration/test_backtest_research.py`.

## The manifest

Written beside every export as `<stem>.manifest.json`:

dataset version · created_at · horizon · row count · symbols · date range ·
feature columns · label columns · context columns · **excluded rows with
reasons** · feature/signal/scanner/cost/label versions · the filters applied.

No credentials, ever.

Exclusions are counted rather than silently dropped, so a dataset that lost 80%
of its rows to `PENDING` labels says so on its face.

## What is excluded by default

| Reason | Why |
|---|---|
| `PENDING` / `INSUFFICIENT_FUTURE_DATA` | unlabelled; would otherwise be read as zero |
| non-`REGULAR` session | the IEX feed's extended-hours prints measure the feed more than the market |
| suspicious spread | > 100 bps during regular hours is a feed artefact |

`AMBIGUOUS_SAME_BAR` rows are **retained** and counted separately: the row is
real, only its barrier ordering is unknowable, and any study of barrier outcomes
needs to see how many results rest on that.

## Sampling policy

**Nothing is deduplicated.** Raw observations are preserved and filtering is made
explicit, because silently collapsing rows would destroy the record of how the
scanner actually behaved.

But these rows are **not independent**, and treating them as if they were is the
easiest way to manufacture significance:

- consecutive evaluations of the same instrument minutes apart are near-copies;
- a setup that persists across a session produces many rows with the same premise;
- overlapping label windows share future bars outright -- two observations an hour
  apart with a `5d` horizon share almost all of their outcome window.

So every row carries the handles needed to recognise correlation later:

| Column | Identifies |
|---|---|
| `symbol` | same instrument |
| `session_date` | same trading session |
| `tracked_signal_id` | same continuing setup, across scans |
| `reference_timestamp` | consecutive evaluations |
| `label_end_timestamp` | overlapping outcome windows |
| `backtest_run_id` | which replay produced it (null = live scanner) |

Any effective-sample-size or clustering correction belongs downstream; phase 5's
job is to make it *possible*.

## The full score distribution

`qualified` is a column, not a filter. Observations are recorded across the whole
score range -- negative, near-threshold and strong alike -- because a dataset of
winners teaches survivorship and a model trained only on rows above 75 has never
seen the boundary it is supposed to learn.

`tradabot research score-calibration` reports the bands. It is **measurement**:
it does not license moving the 75/85 thresholds, and tuning them on this data and
then quoting it as validation would be circular.

## Data currently available

The limiting factor is intraday history, not code.

| Timeframe | Sessions | Range |
|---|---|---|
| D1 | 276 | 2025-07-08 → 2026-08-11 |
| H1 | 124 | 2026-02-12 → 2026-08-11 |
| M15 | 31 | 2026-06-29 → 2026-08-11 |
| M5 | 15 | 2026-07-22 → 2026-08-11 |

A **production-faithful** replay needs all four timeframes and is therefore
limited to roughly the last 15 sessions. Over the longer H1 window the 5m and 15m
contexts degrade to `INSUFFICIENT`, which is a *different* strategy from the live
one -- honest, but not the same thing, and any run over that window must say so.

Deeper M5/M15 history is the single highest-value data acquisition for phase 6.
