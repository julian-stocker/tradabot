# Simulation timing semantics

> This is the most important document in the paper-trading phase. Every result the
> simulator produces is worthless if the timing rules below are violated, and the
> violation is invisible in the output — it just makes everything look better.

## The rule

**A signal computed from the close of bar *N* may not execute before bar *N+1*.**

```
bar N                         bar N+1
├──────────────────┤          ├──────────────────┤
open   high/low  close        open  ...
                    │           │
                    │           └── earliest possible fill
                    └── signal computed here
```

The close of bar *N* is not knowable while bar *N* is forming. A fill priced from
it uses information that did not exist at decision time — and because the close is
the very number the signal reacted to, the resulting P&L is systematically
flattering.

## How it is enforced

Structurally, not by convention. `PaperTradingEngine.open_from_decision` takes
**both** timestamps and raises before doing anything else:

```python
if execution_timestamp <= signal_bar_timestamp:
    raise LookAheadError(signal_bar_timestamp, execution_timestamp)
```

There is no argument combination that fills at or before the signal bar. It is a
`LookAheadError` — an exception, not a rejection — because it is always a caller
bug, never a market condition.

Tested by `TestNoLookAhead` in `tests/integration/test_paper_lifecycle.py`:
execution *at* the signal bar raises, execution *before* it raises, execution
after it succeeds, and the recorded entry price comes from the execution bar.

## Timestamp conventions

| Field | Meaning |
|---|---|
| `Candle.timestamp` | Bar **open** time (left edge). A 5-minute bar stamped 14:30 covers [14:30, 14:35) and completes at 14:35. |
| `SignalRow.bar_timestamp` | The bar the signal was computed from. |
| `VirtualOrder.requested_at` | When the order was placed. Must be after the signal bar. |
| `VirtualPosition.entry_timestamp` | The fill time — equal to `requested_at` for market orders. |
| `PortfolioSnapshot.timestamp` | The bar whose close drove the valuation. |

Everything is UTC and timezone-aware; `ensure_utc` rejects naive datetimes at the
boundary.

## Prices available at each step

| Step | May use | May **not** use |
|---|---|---|
| Signal | bars up to and including *N* | anything after *N* |
| Sizing | the expected fill (ask + slippage) at *N+1* | the actual fill, which is not yet known |
| Entry fill | quote or open at *N+1* | bar *N*'s close |
| Exit check | the bar being processed | any later bar |
| Mark-to-market | current bar's close, or the bid | tomorrow's price |

Sizing deliberately uses the **expected** fill (the ask), not the mid. Sizing
against the mid systematically overshoots by half a spread plus slippage, and the
error grows with the spread — biggest exactly where liquidity is worst.

## Exit timing

Exits are evaluated **against the bar being processed**, which is legitimate: a
stop resting in the market executes during the bar, not after it. What the
simulator must not do is choose *which* intrabar event happened first when the
data cannot say — see the ambiguity policy in
[paper-trading.md](paper-trading.md#same-bar-ambiguity).

| Exit | Fill price |
|---|---|
| Stop touched intrabar | the stop level |
| Stop gapped through at the open | **the open** (worse than the stop) |
| Target touched intrabar | the target level |
| Target gapped through at the open | **the open** (better than the target) |
| Max holding period | the bar's close |
| Signal reversal | the mark supplied by the caller |

## The clock

Business logic never calls `datetime.now()`. Every timestamp is injected:

- `PaperTradingEngine.open_from_decision(execution_timestamp=…)`
- `PaperTradingEngine.process_bar(bar=BarPrices(timestamp=…))`
- `PaperTradingService.run_signal(now=…)`
- `evaluate_decision(now=…)`

That is what makes the demo byte-identical across runs, and it is why the tests
need no clock mocking. `utc_now()` appears only in CLI entry points and as a
last-resort default.

## Bars, not calendar days

Holding periods are counted in **bars processed**, not elapsed time:

```python
holding_period_expired(bars_held=…, max_holding_bars=…)
```

tradabot has no exchange calendar yet, so "5 trading days" cannot be derived from
timestamps — a weekend or holiday would silently shorten the period. Counting bars
is exact for the data actually seen, and with daily bars `max_holding_bars=5`
*is* five trading days.

The approximation lives in exactly one function. A real calendar replaces that
function and nothing else.

## Known gaps

1. **Intrabar path is unknown.** OHLC gives four numbers per bar; the order in
   which they occurred is lost. Resolved by an explicit, conservative policy
   rather than a guess.
2. **Quotes are point-in-time only.** Historical quotes are not stored, so an
   `as_of` simulation uses the configured default spread rather than the spread
   that actually prevailed. Phase 6.
3. **No intraday latency model.** A market order is assumed to fill at the
   execution bar with no queueing delay. Realistic for daily bars, optimistic for
   minute bars.
