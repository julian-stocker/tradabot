# Outcome labels

What happened *after* a signal. **This is Y**, and it lives in its own tables
(`signal_outcomes`, `trade_outcomes`) joined to `signal_evaluations` by id.

## Why labels are not columns on the evaluation

Because `SELECT *` is the most common query anyone writes. A future-derived
column sitting beside the features is leakage waiting for the first careless
join, and no naming convention survives contact with a notebook. The join is
deliberate friction: it forces the feature/label boundary to be restated every
time it is crossed.

## Market outcome vs trade outcome

Two different questions, kept in two tables because they routinely disagree.

| | `signal_outcomes` | `trade_outcomes` |
|---|---|---|
| answers | what did the market do? | what would *we* have made? |
| knows about capital | no | yes |
| knows about costs | no | yes |
| rows per evaluation | one per horizon | one per portfolio |
| same for every portfolio | yes | no |

A signal the market vindicated by 40 bps is a loss in a 100 EUR account whose
round trip costs 60. Reporting only the market outcome calls that signal good;
reporting only the trade outcome hides that the *signal* was right and the
*account size* was wrong.

## Horizons

`15m`, `1h`, `4h`, `1d`, `3d`, `5d`, `20d`.

The day-denominated ones mean **trading days**, resolved through the exchange
calendar (`TradingCalendar.add_trading_days`). Adding `timedelta(days=5)` to a
Friday afternoon lands inside a weekend where there is no price at all, and steps
over holidays as if the market had been open. A `D5` horizon from 1 July 2024
resolves to 9 July, skipping Independence Day.

Intraday horizons are wall-clock but session-aware at the edge: a 4-hour horizon
from 18:00 UTC ends after the close, so it rolls to the next session's open and
the row is flagged `rolled_to_next_session`. It never resolves *backwards* -- a
horizon that clamped to the previous close would produce a return whose sign is
meaningless.

## Reference and future prices

| Purpose | Price used |
|---|---|
| signal-time reference | the primary timeframe's close at `T` -- the last value the scoring engine consumed |
| future price | the close of the last bar at or before the resolved target |
| simulated entry | the **next** bar's open after `T` |
| simulated exit | the stop, target, or the bar close at the holding limit |
| mark-to-market | bar close (bid, when a live quote exists) |

The market-outcome reference is deliberately the price the signal *saw*. Reaching
for a quote or a later bar would measure a return the signal never had access to
the start of.

## MFE and MAE

Maximum favourable and adverse excursion, measured over `(reference, target]`
from bar **highs and lows**, not closes -- the excursion is how far the move went
while the window was open, not where it happened to end.

```
MFE (long) = max(high) / reference - 1
MAE (long) = min(low)  / reference - 1
```

Stored as ratios. **Neither is realised P/L**: a trade can end +8% having been
10% underwater first, and both facts are recorded. The reference bar itself is
excluded from the window, so an excursion can never be "achieved" before the
trade could exist.

The `direction` column exists so SHORT can be labelled later without a migration;
production remains long-only.

## Barriers

Against a fixed 2% target and 1% stop -- fixed rather than ATR-derived, because a
*market* label must mean the same thing for every row. Letting the barrier float
with volatility would make `TARGET_FIRST` a different question per observation.
Execution-aware barriers belong to the trade outcome, where the risk profile
properly applies.

| Outcome | Meaning |
|---|---|
| `TARGET_FIRST` | target touched before the stop |
| `STOP_FIRST` | stop touched before the target |
| `NEITHER` | horizon elapsed with neither touched |
| `AMBIGUOUS_SAME_BAR` | one candle spanned both |

`time_to_target` / `time_to_stop` are seconds from the reference instant.

### The ambiguity rule

When a single candle's range covers both levels, OHLC says both happened within
those minutes and nothing about the order. **The label does not guess.** It
records `AMBIGUOUS_SAME_BAR`, stores the offending bar's timestamp, and stops.

Execution has to resolve it to continue, and assumes the **stop** came first
(`CandleAmbiguityPolicy.CONSERVATIVE`). A backtest that is pessimistic by an
unknown amount is survivable; one that is optimistic by an unknown amount is
worthless.

## Pending labels are never zero

Recent observations cannot be labelled at any horizon longer than their own age.

| Status | Meaning |
|---|---|
| `COMPLETE` | enough future data existed; the label is real |
| `PENDING` | the horizon has not elapsed yet |
| `INSUFFICIENT_FUTURE_DATA` | the horizon elapsed but bars are missing |

Writing `0.0` for either absence would be indistinguishable from a flat market.
It drags every mean toward zero, understates variance, and does the most damage
to the newest -- most relevant -- observations. So the outcome columns are
nullable and the status sits beside them.

The labeller does **not** consult bars beyond `now`, even when the database holds
them. Otherwise a horizon that had not elapsed would still be labelled `COMPLETE`
from a backfill, which is the labeller committing its own look-ahead.

## Idempotency

`tradabot outcomes generate` is safe to re-run and safe to schedule.

- Upserts key on `(evaluation_id, horizon, direction, label_policy_version)`, so
  a second run updates in place rather than doubling the weight of every row.
- Rows already `COMPLETE` are skipped unless `--recompute` is passed.
- A `PENDING` row that now has enough future data is **updated** to `COMPLETE`,
  keeping its id so anything joined to it survives maturation.

Verified in `tests/integration/test_backtest_research.py`: a labelling pass at an
early `now` leaves long horizons pending, and a later pass matures them.

## Versioning

Every outcome carries `label_policy_version`; every trade outcome also carries
`cost_model_version`. Rows computed under different versions are **not
comparable** and must never be pooled into one statistic. Changing a resolution
rule, an excursion definition or a barrier level means bumping
`LABEL_POLICY_VERSION` in `app/research/horizons.py`.
