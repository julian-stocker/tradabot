# The paper risk layer

**PAPER_RISK_LAYER: `INTEGRATED`.**

Integrated means wired into the paper engine, tested, and measured against a
historical candidate stream. It does **not** mean `TRADING_STRATEGY_READY`,
`BUY_READY` or `LIVE_TRADING_READY`. Those remain unmet: no phase has found
stable directional information, and a risk layer cannot create any.

## Where it sits

```
candidate -> derive stop (ATR) -> RISK GATE -> canonical sizing -> execution
```

The gate does exactly two things:

1. **Floors the stop distance** at `minimum_noise_distance(1)`, so nothing is
   sized off a stop sitting inside ordinary noise.
2. **Refuses**, for arithmetic reasons only — budget, practical size, cost
   share, staleness, missing estimate.

It never computes a quantity. `app.paper.sizing.size_position` remains the only
thing that does, and `RiskGateDecision` has no `quantity`, `direction` or
`score` field to put an opinion in.

Off by default (`risk_layer_enabled=False`), which is what makes "baseline" a
configuration you can run rather than a claim you assert.

## One cost model

`evaluate_entry` takes `CostSettings`, not a cost figure. It models the round
trip through `app.paper.execution.estimate_round_trip_cost`, which reads the same
fee, spread and slippage settings `price_fill` charges. There is no parameter
through which a caller can assert a cost that differs from the one execution
will apply, and a test asserts the parameter's absence.

## Execution fractionality

| Mode | Behaviour |
|---|---|
| `FRACTIONAL_ALLOWED` | quantity to 8 dp |
| `WHOLE_SHARES_ONLY` | quantity floored to an integer |

Both round **down**, always. Every cap in the sizer is an upper bound derived
from risk, cash or exposure, so rounding up breaches whichever one was binding.
This fixed a real defect: `quantize` defaulted to `ROUND_HALF_EVEN`, which could
round a risk-limited maximum upward.

An unaffordable whole share yields `QUANTITY_TOO_SMALL`, not a fractional
position the venue cannot fill.

## What the replay measured

216 qualified bullish candidates, 36 instruments, 2025-02-11 to 2026-07-28, from
backtest run 1. Run 4 (2020–2024) produced zero qualified evaluations and
contributes nothing. Entries fill at the **next** hourly bar's open.

One split falls inside the window — HON, declared 1-for-2 effective 2026-06-29.
The price series shows no discontinuity there (231.40 → 227.81), confirming it as
the phantom action phase 9B built `contradicted_actions` to catch. The unadjusted
forward path is therefore correct, and applying the declared split would have
been the error.

**No P&L figure below is evidence of strategy quality.** It is the arithmetic
consequence of applying a sizing rule to a candidate stream with no measured
directional edge.

### The headline finding: the ATR multiple is inert

`minimum_noise_distance(1)` is the **full** 1-day band, and every `K_BAND`
constant is at least 3.117. A `2.0 × ATR` structural stop is therefore narrower
than the floor in **all four regimes, always**.

| Regime | Noise floor | `2.0 × ATR` stop | Floor wins |
|---|---|---|---|
| LOW_VOL | 4.312% | 2.000% | yes |
| NORMAL_VOL | 3.727% | 2.000% | yes |
| HIGH_VOL | 3.324% | 2.000% | yes |
| EXTREME_VOL | 3.118% | 2.000% | yes |

The replay confirmed it empirically: **162 of 162 permitted entries were
floor-bound**. A profile running `stop_loss_atr_multiple = 2.0` with the risk
layer enabled is not using its ATR multiple — risk-v1's band sets the stop on
every trade. The setting is not ignored; it is dominated.

This is stated, not fixed. Loosening the floor would mean tuning a frozen,
pre-registered constant against a replay.

### Entries and rejections (1% risk, fractional)

| Portfolio | Baseline entries | Risk-layer entries | Risk-layer rejections |
|---|---|---|---|
| paper-100 | 46 | **0** | 132 cost share, 84 impractical size |
| paper-1000 | 189 | 162 | 54 max open positions |
| paper-10000 | 189 | 162 | 54 max open positions |

For paper-1000 and paper-10000 the gate refused **nothing** at 1% risk: the 54
rejections are the pre-existing concurrency limit, hit more often because the
layer's wider stops hold positions differently. At 0.25% and 0.5% the picture
inverts and the cost-share cap refuses everything at paper-1000.

### The cost-share backstop binds — contrary to its own design note

`MAX_COST_SHARE_OF_RISK = 0.35` was documented as an inert backstop on the basis
of a phase 11.2 measurement of 8.1%. That measurement used a 20 bps round trip
with **no flat per-order fee**.

With the configured €1.00 order fee, the round trip carries €2.00 of fixed cost
whatever the size, so the share is set by the risk *budget*:

| Portfolio, 0.25% risk | Risk budget | Backstop on | Backstop off |
|---|---|---|---|
| paper-100 | €0.25 | 0 entries | 0 entries (refused on size first) |
| paper-1000 | €2.50 | **0 entries**, 216 cost-share rejections | 162 entries |
| paper-10000 | €25.00 | 162 entries | 162 entries |

The cap is working — a trade whose fees consume 90% of what you are willing to
lose has no room left to be right — but it is a live constraint on small
accounts, not a backstop. The docstring has been corrected.

### Risk breach audit (1% risk, fractional)

Classified against the 80% one-day band at entry, which is a coverage figure,
not a bound.

| | Baseline (n=189) | Risk layer (n=162) |
|---|---|---|
| WITHIN_EXPECTED_RISK | 98.4% | 59.9% |
| NORMAL_EXCEEDANCE | 0.0% | 31.5% |
| GAP_EXCEEDANCE | 0.5% | 7.4% |
| EXTREME_EXCEEDANCE | 1.1% | 1.2% |

**The risk layer's higher exceedance rate is definitional, not a model failure.**
Because the floor equals the band, a risk-layer stop sits *at* the 1-day band by
construction, so any stop-out is a band-touching event and lands just outside it
once costs and slippage are added. The baseline's 2 × ATR stop sits well inside
the band, so its stop-outs are inside it too.

The number that carries information is `EXTREME_EXCEEDANCE`, which is unchanged
at ~1.1–1.2% across both. Losses beyond twice the band are rare and the layer
neither creates nor prevents them.

### Gap-through

| Configuration | Stop exits | Gapped | Rate |
|---|---|---|---|
| paper-1000 baseline | 98 | 13 | 13.3% |
| paper-1000 risk layer | 65 | 14 | 21.5% |

Gap-through is real at every stop distance, consistent with phase 11.1. A wider
stop is hit less often but a larger share of the hits are gaps, because the
remaining hits are increasingly driven by overnight moves rather than intraday
drift. `stop_excess_loss` is persisted per stop exit — measured against the stop
*level*, not the fill, so ordinary spread and slippage are not misreported as
risk-model breaches.

### paper-100: fractional shares do not rescue it

| Risk budget | Fractional entries | Whole-share entries |
|---|---|---|
| 0.25% | 0 | 0 |
| 0.50% | 0 | 0 |
| 1.00% | 0 | 0 |
| 2.00% | 0 | 0 |

**Identical, because both gate tests are on notional.** `MIN_PRACTICAL_NOTIONAL`
and `MAX_COST_SHARE_OF_RISK` are evaluated before sizing ever rounds anything, so
share granularity cannot change the outcome.

The baseline shows why this is the right answer rather than an inconvenience:
paper-100 with the layer off takes 46 trades at 1% risk and **loses essentially
its entire capital, €92.96 of it in transaction costs alone**. Fractional shares
let a €100 account trade; they do not let it survive a €1.00 flat fee. The
constraint is structural, and no higher-turnover variant was invented to hide it.

## Persisted metadata

Migration `0013`, thirteen nullable columns on `virtual_positions`. Nullable
throughout: a position opened with the layer off carries NULL, which is the
truthful record — a zero would claim the layer ran and had nothing to say.

`risk_structural_distance`, `risk_noise_floor`, `risk_distance`,
`risk_floor_bound`, `risk_regime`, `risk_band_1d`, `risk_estimated_cost`,
`risk_model_version`, `execution_fractionality`, `risk_flag`,
`risk_flag_updated_at`, `stop_excess_loss`.

`execution_fractionality` is written even with the layer disabled, because the
mode changed the quantity either way and a mixed table with no mode column
cannot be read.

## Rolling flags

`PaperTradingEngine.refresh_risk_flags` recomputes a descriptive flag on every
open position: `RISK_STABLE`, `RISK_INCREASED`, `RISK_DECREASED`, `RISK_EXTREME`,
`RISK_DATA_STALE`.

**It cannot close anything.** No member means "exit", there is no exit path in
the method, and tests assert both. A position whose risk doubled is flagged and
left open, because deciding what to do about that belongs to a
position-management phase that does not exist.

Comparison is against the **persisted** `risk_band_1d`, not a re-derived one, so
the flag cannot silently change when historical bars are re-adjusted for a split.

## Architecture note: reserved capital (not implemented)

The replay surfaces a structural problem it cannot solve: at paper-100, the flat
per-order fee dominates any risk budget the account can support. One way out is
to stop treating the whole balance as tradable.

The shape, recorded for a future phase and **deliberately not built**:

| Concept | Meaning |
|---|---|
| `ACTIVE_CAPITAL` | The portion positions are sized against. Risk fractions apply to this, not the balance. |
| `LOCKED_RESERVE` | Held back and never sized against. Raises the effective risk-per-trade on the active portion without raising it on the account. |
| `RESERVE_PAYABLE` | Obligations already incurred against the reserve — accrued fees, settlement in flight — so the reserve is not double-counted as free. |

Why it is not implemented now: it changes what `equity` means to
`size_position`, and equity is the denominator of every cap in the system. That
is a change to the sizing contract, not an addition to it, and it needs its own
phase with its own tests. Adding a third capital concept to make a €100 account
trade would also be solving the wrong problem — the replay's finding is that
paper-100 is structurally unviable at a €1.00 flat fee, and a partition of the
same €100 does not change the fee.

## Discord

**Not enabled.** No test messages were sent. Risk metadata is available in the
database and via the existing `tradabot risk` CLI; adding a channel section is a
product decision, not a research outcome.

## Production safety

Unchanged and verified: WATCH/BUY/SELL disabled, no orders placed, signal-v1 and
the 75/85 thresholds untouched, seven scheduler jobs healthy, `.env` unmodified,
no paid data plan enabled, no ML.
