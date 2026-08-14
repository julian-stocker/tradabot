# Canonical price semantics

**The one-sentence policy:** candles are **stored raw and never rewritten**;
every price-derived calculation reads them **split-adjusted**, applied on read,
using only corporate actions knowable at the moment being reconstructed.

This document is the answer to "what price series does tradabot use for return,
ATR, volatility, trend and research calculations across corporate actions?".
There is exactly one answer, and it is enforced by tests rather than convention.

## Why adjust on read rather than store adjusted

A new split retroactively changes every earlier adjusted price. Storing adjusted
candles would mean rewriting an instrument's entire history on every new action —
a large, error-prone write that also destroys the raw record if it goes wrong.
Recomputation is a cumulative product over a handful of actions.

Alpaca is asked for `Adjustment.RAW` **deliberately**. Letting the provider
pre-adjust would produce a series that silently disagrees with our adjustment
layer, with no way to tell which had been applied.

## The rule

For a bar at time *t*, the cumulative price factor is the product of `1/ratio`
over every split whose `effective_at` is **strictly after** *t*:

```
price_factor(t)  = Π 1/ratio(s)   for splits s with s.effective_at > t
volume_factor(t) = Π   ratio(s)   (shares multiply as prices divide)
```

Bars at or after the last split are untouched, so an adjusted series always ends
on today's real traded price — a value a reader can check against a broker
screen.

Split ratios are stored as an explicit `from_shares`/`to_shares` pair, never a
float. A 3-for-2 is `2 → 3`; storing 1.5 loses that it was a 3:2, and a 1-for-3
becomes 0.3333… which is not exactly representable and compounds badly.

**Dividends are not applied.** Correcting for them means choosing a reinvestment
price and timing, and a wrong choice silently biases every return.
`TOTAL_RETURN` raises `NotImplementedError` rather than approximating — see
[data-adjustments.md](data-adjustments.md).

## Is back-adjustment causal?

Rescaling past prices when a *future* split occurs looks like leakage, and the
question deserves an answer rather than a reassurance.

It is not leakage here, because every feature built on these prices is
**scale-invariant**: returns, percentage distances from moving averages, ATR as a
percentage of price, volume relative to its own rolling mean. Multiplying a whole
trailing window by a constant leaves all of them unchanged. The factor only
varies *across* a split boundary, and there it removes a discontinuity that was
never a price move.

A feature keyed on an absolute price level would break this. There is none, and
`test_a_split_after_the_window_does_not_change_the_window` proves the property
directly rather than asserting it.

For point-in-time reconstruction, `known_as_of` restricts actions to those
already effective — a backtest of March 2021 must not adjust using a July split.

## Who reads what

| Consumer | Path | Adjusted |
|---|---|---|
| Scanner / signal-v1 | `FeatureService` | ✅ default `SPLIT_ADJUSTED` |
| Backtesting engine | `FeatureService` | ✅ |
| Paper replay | `FeatureService` | ✅ |
| API | `FeatureService` | ✅ |
| Research feature frames | `app.research.adjustments` | ✅ (phase 9A) |
| volatility-v1 | `AdjustedCandleReader` | ✅ (phase 9B) |
| #market-trends movers | `AdjustedCandleReader` | ✅ (phase 9B) |

`app/market_data/adjusted.py` exists so the last two share one implementation.
Two independent patches would have meant two places to forget, and the next
consumer that wants "just the recent bars" would restart the cycle.
`test_adjusted_reader.py` fails if either module re-imports `CandleRepository`.

## Two implementations, one rule

`app/corporate_actions/adjust.py` is the authority, in `Decimal` over domain
objects. `app/research/adjustments.py` applies the same rule in polars over
float64, because the research frames are ~692,000 rows and per-row `Decimal` is
far too slow.

They are pinned to each other by a randomised property test, not by comment. If
the rule changes in one place, the test fails rather than the two silently
diverging.

## Validating actions against prices

A stored action is not evidence its event happened, and a missing action is not
evidence nothing happened. Only the prices can arbitrate.

**Corroboration.** A declared ratio must explain the observed jump better than
"no split" does, and land within a factor of 1.35. The test is scale-free: a
percentage tolerance would have to be loose enough for TSLA's 5-for-1 on a
12%-move day, which is loose enough to admit a phantom.

**"Cannot check" is not "is wrong".** If the bars bracketing a split are more
than 10 days apart, the ratio between them measures price drift, not a share
count. Those are **indeterminate and applied** — the provider is the authority on
whether an action happened, and missing local data is no evidence of provider
error. Phase 9A got this wrong and dropped NVDA's genuine 10-for-1 across a
557-day hole, leaving every earlier daily price ten times too high.

**The scan.** `tradabot market-data verify-adjustments` classifies every notable
discontinuity:

| Kind | Meaning | Fails the check |
|---|---|---|
| `EXPLAINED` | A stored split accounts for it | no |
| `UNEXPLAINED` | Split-shaped, no stored action | **yes** |
| `CONTRADICTED` | Stored action the prices deny | **yes** |
| `MARKET_GAP` | Large but not split-shaped | no |

It walks bars *and* actions. The bar walk finds a split with no action; only the
action walk finds an action with no split, because a phantom action moves no
price and is invisible to the first.

### The blind spot, named

Classification uses a magnitude boundary of 1.70. In this database the largest
genuine single-bar move is Netflix's 2022-04-20 crash at 1.543 and the smallest
real split is SMH's 2-for-1 at 1.958, so the boundary sits in the measured gap.

**A 3-for-2 split lands at 1.50, inside the range where real crashes live.** No
rule reading prices alone can separate a stock that fell 33% from one that split
3-for-2 — the series are identical. Such bars are reported as `MARKET_GAP` and
left alone, because suppressing a real move is the worse failure. The primary
defence against a missing split is *fetching corporate actions for every
instrument*; the scan is a backstop for large events, not a substitute for
coverage.

## Coverage is the thing that actually fails

Both real defects found so far were coverage, not logic:

1. **The window.** Alpaca's corporate-actions endpoint defaults to roughly the
   current month. An unbounded call returned one action across 62 instruments and
   reported success. The query now spans the full stored candle history.
2. **Late registration.** QQQ and SMH were registered after the one-shot sync, so
   nothing ever fetched their actions, and SMH carried an unadjusted 2-for-1 for
   an entire phase. Benchmark registration now fetches actions for anything it
   creates.

Counting stored actions cannot detect either — ADBE, AMD, BA and BRK.B
legitimately have zero. That is why the check is price-based.

```bash
tradabot market-data corporate-actions          # all instruments, full history
tradabot market-data verify-adjustments         # exits non-zero on a real finding
```

Re-run `corporate-actions` after adding instruments, and `verify-adjustments`
as a periodic gate.
