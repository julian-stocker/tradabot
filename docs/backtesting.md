# Backtesting: constraints before code

**No backtesting engine exists yet.** `app/backtesting/` contains data structures and
protocols only.

That is deliberate. A backtester that is subtly wrong is *worse than none*, because it
manufactures confidence in a strategy that will lose money. Every constraint below must
be satisfied by the implementation, and each is expressed in the type signatures where
possible rather than left as advice in a document nobody re-reads.

---

## 1. Look-ahead bias

Using information that was not available at decision time. The most common failure,
and the most flattering: it makes almost any strategy look profitable.

**How it enters:**

- Computing an indicator over the full series, then iterating — a centred rolling
  window or a back-filled null quietly imports the future.
- Filling an order at the signal bar's close. That close was not observable while the
  bar was forming.
- Using the current bar's high/low to decide whether a stop was hit *and* at what price.
- Selecting a universe, or normalising features, using full-sample statistics.

**Structural defence — `DataFeed`:**

```python
def history(self, symbol, timeframe, as_of, bars) -> pl.DataFrame: ...
```

There is deliberately **no method returning the full series**. A strategy written
against this interface cannot look ahead, because the interface gives it nothing to
look ahead into. Implementations must exclude any bar whose close is at or after
`as_of`.

**Execution lag is mandatory.** A signal computed on bar *t*'s close fills on bar
*t+1* at the earliest. `ExecutionModel.execute` receives the *next* bar for exactly
this reason.

**Already enforced in phase 1:** `CandleRepository.get_latest(as_of=…)` filters in SQL,
and `tests/unit/test_no_lookahead.py` proves prefix invariance for every feature.

---

## 2. Survivorship bias

Testing only on instruments that still exist today. Systematically excludes the
failures, which is precisely the population that would have hurt.

**How it enters:** "give me the S&P 500 constituents" returns *today's* constituents.
Backtesting them over 2015–2025 tests a portfolio selected with ten years of hindsight.

**Defences:**

- Instrument rows are **never deleted**. Delisted instruments carry `is_active = False`.
- `InstrumentRepository.list_all(active_only=False)` exists and backtests must use it.
  The default `True` is for UI display only.
- `DataFeed.universe(as_of)` must return the universe *as it was* at that time —
  including instruments later delisted, excluding those not yet listed.

**Known gap:** the phase 1 schema records no listing/delisting dates, so a
point-in-time universe cannot yet be reconstructed. Phase 2 must add
`listed_at` / `delisted_at` before any historical study is credible. A backtest run
before then must state this limitation in its output.

---

## 3. Data leakage

Information from the evaluation period influencing the model.

**How it enters:**

- Normalising or standardising features using statistics computed over the full dataset.
- Imputing missing values with a full-sample mean.
- Selecting features, parameters, or thresholds by looking at the whole history and
  then "validating" on part of it.
- Random train/test splits on time-series data. Adjacent bars are highly correlated, so
  a random split puts near-duplicates of test rows into training.

**Rules:**

- Any fitted transform (scaler, imputer, encoder) is fitted on training data **only**
  and applied forward.
- Splits are **chronological**. Walk-forward validation, never `train_test_split(shuffle=True)`.
- A **purge gap** between train and test at least as long as the forecast horizon.
  Without it, a label built from a 5-day forward return overlaps the first 5 days of
  the test window.
- Every hyperparameter search is inside the walk-forward loop, not around it.

See [ml.md](ml.md) for the evaluation protocol.

---

## 4. Unrealistic fills

Assuming executions no real broker would give you.

**How it enters:**

- Filling at the mid price (ignoring the spread entirely).
- Filling unlimited size regardless of the bar's volume.
- Assuming a limit order fills because price *touched* it — touching is not trading.
- Ignoring that spreads widen at the open, the close, and around news, i.e. exactly
  when signals tend to fire.

**Rules for `ExecutionModel`:**

1. Fill on the bar **after** the signal bar.
2. Apply spread and slippage **adversely on both legs** — buy above mid, sell below.
3. **Refuse to fill on zero volume**, and cap size against available volume.
4. Return `None` for an order that cannot fill, so unfilled orders appear in results
   instead of silently becoming free.
5. `Order` and `Fill` are separate types. Collapsing them is the structural mistake
   that lets code assume the intended price *is* the achieved price.

---

## 5. Reporting that cannot mislead

`BacktestMetrics` makes the honest numbers **mandatory fields**, not optional extras:

- **`gross_return` and `net_return` side by side.** Gross-only is how a cost-negative
  strategy looks profitable; net-only hides how much of the edge costs consumed.
- **`benchmark_return` is required.** A 12% return means nothing until you know the
  index returned 25%. `excess_return` is what is actually being claimed.
- **`cost_drag`** — the difference — stated explicitly.
- **`sharpe_ratio` is nullable**, to be left null when there are too few trades. A
  Sharpe from six trades is noise with a decimal point.
- **`trades` and `equity_curve` are retained**, not just the summary. A headline number
  cannot be audited; reproducing a suspicious Sharpe requires the individual fills.

---

## 6. Multiple comparisons

Not a backtesting bias exactly, but the reason most backtested strategies fail live.

Testing 200 parameter combinations and keeping the best one does not find a strategy —
it finds the combination that best fit the noise. At a 5% significance level, 200 tests
yield ~10 "significant" results from pure chance.

**Defences:**

- Report **how many configurations were tried**, always.
- Reserve a genuine holdout period, evaluated **once**, at the end.
- Prefer few parameters. A rule with two parameters that works across many instruments
  is more believable than one with nine tuned to a single symbol.
- Treat out-of-sample degradation as the expected outcome, not a surprise.

`ScanResult` applies the same principle to the scanner: `instruments_scanned` and
`hit_rate` are mandatory so the base rate is impossible to miss.

---

## Implementation status (phase 5)

The engine now exists. `app/backtesting/engine.py` replays historical instants
through **the production analyser, feature service and signal engine** -- there is
no separate backtest strategy, because a second implementation drifts from
production and then every comparison measures the drift.

- [x] `DataFeed` bounded by `as_of` (`app/backtesting/feed.py`), and the bound is
      now **bar close**, not bar start -- see below
- [x] Execution lag of at least one bar, asserted in
      `tests/unit/test_backtest_execution.py`
- [x] Spread + slippage applied adversely on both legs, via the production
      `estimate_round_trip_cost`
- [x] Gross, net and itemised costs reported together
- [x] Purge/embargo metadata exported (`reference_timestamp`,
      `label_end_timestamp`, `session_date`) -- see docs/ml-readiness.md
- [ ] `listed_at` / `delisted_at` on instruments -- **still absent**, see
      *Survivorship* below
- [ ] Volume-capped fills
- [ ] A deliberately look-ahead-biased strategy asserted to score impossibly well

---

## The bar-close rule

Candles are stamped when they **open**. So "bars before `as_of`" and "bars that
had finished by `as_of`" are different sets, and only the second is safe.

Phase 5 found the first one in production. `CandleRepository.get_latest` filtered
`timestamp < as_of`, so at 14:20 the hourly bar stamped 14:00 -- which does not
close until 15:00 -- was visible, close price included. Verified against the live
database:

```
as_of = 2026-08-04 14:20  (the 14:00 H1 bar closes at 15:00)
  bar_start=2026-08-04 13:00  closes=14:00  close=209.99
  bar_start=2026-08-04 14:00  closes=15:00  close=209.57  <-- 40 minutes of future
```

The filter is now `timestamp <= as_of - timeframe.duration`, which is
`bar_start + duration <= as_of` rearranged for the index. Walk-forward replay
passes a bar's own timestamp and the previous bar closes exactly then, so the
visible set there is unchanged; what changed is that the **live scanner** no
longer scores a partially formed bar. Regression tests:
`tests/integration/test_point_in_time.py`.

---

## Execution convention

For a primary-timeframe bar closing at `T`:

| Event | When |
|---|---|
| candle open | `T - duration` |
| candle close | `T` -- the first instant its close price exists |
| signal evaluation | `T`, from bars finished at or before `T` |
| order decision | `T` |
| earliest executable | the **next** bar's open, strictly after `T` |
| fill | that next bar's open |

A 5-minute candle closing at 10:05 produces a signal timed 10:05 which fills at
the 10:05-10:10 bar's open. It never fills at the 10:00 open -- that price had
already passed when the information arrived. Where conventions differ the
pessimistic one is taken: the gap between a signal bar's close and the next open
is a real cost, and a close-fill backtest silently deletes it.

---

## Same-bar ambiguity

When one candle's range spans both the target and the stop, OHLC records that
both were touched but not in which order. The data cannot answer it.

- **Labels** record `AMBIGUOUS_SAME_BAR` and refuse to choose
  (`app/research/labels.py`). Resolving toward the target is how a backtest turns
  its worst trades into its best ones.
- **Execution** must resolve it to continue, and does so under
  `CandleAmbiguityPolicy.CONSERVATIVE`: the stop is assumed to have come first.

Keeping the ambiguity visible as its own outcome is what makes it possible to
measure how many results depend on the guess.

---

## Survivorship

**This backtest is not survivorship-bias-free, and must not be described as
one.** `instruments` carries no `listed_at`/`delisted_at`, so
`HistoricalDataFeed.universe()` returns today's watchlist for every historical
date. The 52 symbols are today's survivors; any name that was delisted or
acquired during the window is simply absent, and the results are biased upward by
an unmeasured amount.

The limitation is recorded on every run's `universe_definition` and stated in the
benchmark report rather than hidden.

---

## What a historical backtest proves

**It proves** that the data pipeline, the feature computation, the signal engine,
the execution model and the cost accounting run end to end over real market data,
chronologically, without reading the future -- and it produces a measured
distribution of outcomes across the score range.

**It does not prove predictive edge.** It is one strategy over one window on one
universe with no significance testing, no multiple-comparison correction and no
out-of-sample discipline. The window available here (see docs/research-dataset.md)
is months, not years. A positive result means the plumbing works and the numbers
are worth looking at again on more data.
