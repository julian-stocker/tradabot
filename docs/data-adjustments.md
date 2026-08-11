# Corporate actions and price adjustment

## The problem, concretely

NVIDIA split 10-for-1 on 10 June 2024. On raw prices, that bar shows a **−90%
return**. Every feature in the system reads it as a market crash:

| Feature (window spanning the split) | RAW | SPLIT_ADJUSTED |
|---|---|---|
| 20-bar return | **−88.2%** | +18.3% |
| Annualised volatility | **824%** | 64% |
| ATR % of price | **44.5%** | 6.4% |
| Signal classification | NEUTRAL (−5.0) | **BULLISH (+47.6)** |

*(measured on tradabot's own mock NVDA series, `as_of=2024-07-01`)*

Nothing about the company changed on that date. One share became ten, each worth a
tenth. A system that scores the raw series is not making a bad prediction — it is
answering a question nobody asked.

---

## The two series

`PriceSeriesAdjustment` is a **required, explicit choice** at the data-loading
boundary. There is no default buried inside an indicator.

### `RAW`

Exactly what the provider delivered. The only series that is a factual record of
what traded, and the **only one stored in the database**.

Use it for: order simulation, execution analysis, anything that asks "what price
would I have paid".

### `SPLIT_ADJUSTED` — the default for features

Prices rescaled so the series is continuous across splits; volumes rescaled
inversely. Dividends are **not** applied.

Use it for: every feature, chart and signal. It is the default because the
alternative silently corrupts momentum, volatility, ATR and every moving average
simultaneously.

### `TOTAL_RETURN` — declared, not implemented

Split-adjusted *and* dividend-reinvested. `adjust_candles` raises
`NotImplementedError`.

It is a separate enum member rather than a flag on `SPLIT_ADJUSTED` for a reason:
**a dividend-adjusted price is not a price anyone ever paid.** Mixing the two
produces a series that is neither, and the confusion is invisible until someone
tries to reconcile a backtest against real fills.

Implementing it requires deciding *where* the dividend is reinvested — ex-date
close? payment-date open? — and a wrong choice biases every long-horizon return
in a direction that is hard to detect. That decision belongs with real dividend
data, not with a synthetic provider.

---

## How adjustment works

### The rule

For a bar at time *t*, the cumulative factors are the product over every split
whose `effective_at` is **strictly after** *t*:

```
price_factor(t)  = Π  1 / ratio(s)     for all splits s where s.effective_at > t
volume_factor(t) = Π  ratio(s)
```

`ratio` is shares-after ÷ shares-before: 2 for a 2-for-1, `0.1` for a 1-for-10
reverse split. **Reverse splits need no special case** — the same arithmetic
handles them.

Bars at or after the most recent split are untouched. Working backwards from the
present means an adjusted series **always ends at today's real traded price**,
which is the only value a reader can check against a broker screen.

### Boundary convention

Half-open, matching candle windows everywhere else in the system: a bar stamped
exactly at `effective_at` is **already post-split** and is not adjusted.

### Computed on read, never stored

Adjusted prices are derived at query time. They are never written back.

Two reasons:

1. **A new split retroactively changes every earlier adjusted price.** Storing
   them means rewriting an instrument's entire history on every new action — a
   large, error-prone write that can destroy the raw record if it goes wrong.
2. **The database stays a factual record.** Raw candles are what the provider
   said. That claim survives any bug in the adjustment layer.

The cost is a cumulative product over a handful of actions per query, computed in
a single reverse pass — `O(bars + actions)`.

### Decimal, then float

Adjustment runs on `Decimal` prices *before* `candles_to_frame` crosses the float
boundary. Scaling is therefore exact, and repeated splits (NVDA has two) do not
accumulate binary error.

---

## Look-ahead: the honest account

Retrospective adjustment uses knowledge of a **future** split to rescale **past**
prices. It is not point-in-time honest in the strict sense, and this section
exists so that is written down rather than glossed.

**Why it is nonetheless safe for feature calculation:**

> Multiplying an entire prefix of the series by a positive constant leaves every
> return, ratio and moving-average *relationship* within that prefix unchanged.

So adjustment cannot manufacture a tradable edge. It removes an artefact without
altering any relative quantity. Both halves of this are tested:

- `test_scaling_preserves_every_return` — the prefix's returns are identical
  before and after adjustment.
- `test_a_later_split_rescales_prices_but_not_returns` — learning about a future
  split moves `sma_20` (an absolute price level) and moves **no** return.

**What is genuinely affected:** absolute price levels. A strategy with a rule like
"buy below €50" behaves differently on an adjusted series, and such a rule is
suspect for exactly this reason.

### Strict point-in-time mode

Callers that need to reconstruct what was knowable on a date pass `known_as_of`:

```python
actions = await repository.list_for_instrument(
    instrument_id=id, symbol="NVDA", known_as_of=datetime(2021, 3, 1, tzinfo=UTC),
)
```

This excludes actions not yet effective. The API exposes it on
`GET /instruments/{symbol}/corporate-actions?known_as_of=…`, and `FeatureService`
applies it automatically whenever `as_of` is supplied.

**Known approximation:** it filters on *effective* time as a proxy for *known*
time. In reality a split is announced weeks before it takes effect, so this is
conservative — it hides an action slightly longer than the market did. Modelling
announcement dates requires provider data that carries them, and is deferred to
phase 3.

---

## The data model

One table, `corporate_actions`, discriminated by `action_type`, with nullable
type-specific columns guarded by CHECK constraints.

**Why one table:** recording a spin-off, merger or symbol change needs no
migration — only the adjustment rule that interprets it. A table per type would
also multiply joins for a query as ordinary as "every action for this instrument,
in order", which the adjustment layer issues on every read.

| Type | Status | `effective_at` means |
|---|---|---|
| `SPLIT` | adjusted | first instant the new shares trade |
| `CASH_DIVIDEND` | recorded, not adjusted | the **ex-dividend** instant |
| `STOCK_DIVIDEND` | recorded only | — |
| `SPIN_OFF` | recorded only | — |
| `MERGER` | recorded only | — |
| `SYMBOL_CHANGE` | recorded only | — |

### Split ratios are a share pair, not a number

`from_shares` / `to_shares`, not a single `ratio` float. A 3-for-2 split is
`(2, 3)`; storing `1.5` loses that it was a 3:2, and a 1-for-3 reverse split
becomes `0.333…`, which is not exactly representable and compounds badly across
multiple actions.

### Dividends are not price adjustments

A `CASH_DIVIDEND` row records the amount, currency, ex-date and payment date. It
does **not** affect `SPLIT_ADJUSTED` prices. Conflating the two is the mistake
that produces a "price" series nobody can reconcile against a trade confirmation.

### Idempotent ingestion

Natural key `(instrument_id, action_type, effective_at)`. Re-ingesting a
provider's full action history is safe and updates in place.

---

## Ordering guarantee

`IngestionService.sync_all` fetches corporate actions **before** candles for each
symbol. An adjusted series computed from a partial action set is
continuous-looking and wrong, with no later signal that anything was missed.

If a provider returns no actions at all, ingestion **logs a warning**:

```
no corporate actions ingested; adjusted series will equal raw series
```

That is not paranoia. "This provider supplies no action data" and "this universe
had no actions" are indistinguishable to a caller, and the first case silently
turns `SPLIT_ADJUSTED` into `RAW`.

---

## What is not handled

1. **Announcement dates** — see the point-in-time approximation above.
2. **Spin-offs, mergers, stock dividends** — recorded, not adjusted. Each needs
   its own rule (a spin-off adjusts price by the value distributed, which
   requires knowing that value).
3. **Symbol changes** — recorded. Stitching an instrument's history across a
   ticker change needs identity resolution, most sanely via ISIN.
4. **Corporate actions on open positions** — a split on a held position changes
   the share count. Phase 3, when positions exist.
5. **Adjustment of stored quotes** — quotes are not yet stored historically
   (phase 5).
