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

## Implementation checklist (phase 4)

- [ ] `DataFeed` bounded by `as_of`, verified by a prefix-invariance test like the
      feature one
- [ ] Execution lag of at least one bar, asserted in tests
- [ ] Spread + slippage applied adversely on both legs
- [ ] Volume-capped fills; no fill on zero volume
- [ ] `listed_at` / `delisted_at` on instruments, and a point-in-time universe
- [ ] Purge gap between train and test windows
- [ ] Gross, net, and benchmark returns reported together
- [ ] Configurations-tried count in every result
- [ ] A deliberately look-ahead-biased strategy in the test suite, asserted to score
      *impossibly* well — proving the harness detects it
