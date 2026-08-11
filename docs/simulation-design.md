# Multi-profile simulation design

## The pipeline

```
market data
    ↓  (raw candles + corporate actions, stored)
price adjustment                    ← docs/data-adjustments.md
    ↓  (SPLIT_ADJUSTED series)
features                            ← no adjustment opinion of their own
    ↓
signal                              ← one signal, persisted once
    ↓
    ├──────────┬──────────┬──────────┐   fan-out: evaluated independently
    ▼          ▼          ▼          ▼
 50 EUR     500 EUR    500 EUR    5000 EUR      simulation profiles
conservative balanced  aggressive  balanced
    ↓          ↓          ↓          ↓
 trade decision (TRADE | SKIP + reason + economics at this size)   ← phase 2
    ↓
 PaperBroker → virtual position → exit → realised performance      ← phase 3 ✅
    ↓
 feedback analytics                                                 ← phase 5+
```

**Phases 2 and 3 implement the pipeline down to realised performance.** The
mechanics of that lower half are documented in
[paper-trading.md](paper-trading.md) and its timing rules in
[simulation-timing.md](simulation-timing.md). Only the analytics layer remains
designed-but-unbuilt.

---

## Why one signal fans out

The same signal is a different proposition for different portfolios, and the
difference is not a matter of taste — it is arithmetic. Measured output from
`evaluate_decision` for one `STRONG_BULLISH` signal at score 62, expected move
120 bps, €1.00 per-order fee, against the phase 2 default profiles (position caps
have since changed, so exact figures differ; the pattern does not):

| Profile | Position | Round-trip cost | Net edge | Verdict |
|---|---:|---:|---:|---|
| 50eur-conservative | €5.00 | 4012 bps | −3892 | SKIP |
| 50eur-balanced | €10.00 | 2012 bps | −1892 | SKIP |
| 50eur-aggressive | €17.50 | 1155 bps | −1035 | SKIP |
| 500eur-conservative | €50.00 | 412 bps | −292 | SKIP |
| 500eur-balanced | €100.00 | 212 bps | −92 | SKIP |
| 500eur-aggressive | €175.00 | 126 bps | −6 | SKIP |
| 5000eur-conservative | €500.00 | 52 bps | +68 | **TRADE** |
| 5000eur-balanced | €1000.00 | 32 bps | +88 | **TRADE** |
| 5000eur-aggressive | €1750.00 | 23 bps | +97 | **TRADE** |

One €1.00 fee is 4012 bps of a €5 position and 23 bps of a €1750 one. The signal
is identical in all nine rows. A single-portfolio system cannot express this, and
a system that models costs as a percentage cannot see it at all.

The €50 portfolio is in the default catalogue precisely because it is the case
that *should* mostly decline — a control for whether the cost gate works.

---

## Capital size is not a risk profile

Three tables, because these are independent dimensions:

```
broker_cost_profiles     what execution costs        (shared across portfolios)
risk_profiles            how much to risk, when      (shared across capital sizes)
simulation_profiles      a portfolio: capital + ↑ + ↑
```

Nine portfolios × three risk appetites store **three risk rows**, not nine.
Collapsing this into one table is the obvious first design and produces the
classic update anomaly: correcting "conservative `risk_per_trade`" would require
nine consistent updates. Verified by
`test_editing_a_risk_profile_moves_every_portfolio`.

**Every risk limit is a fraction of equity, never an absolute amount.** That is
what makes a risk profile reusable: 2% risk-per-trade is €1 on a €50 account and
€100 on a €5000 one, from one row. An absolute limit (`max_position_eur = 250`)
would mean "aggressive" at one size and "unusable" at another, and would force a
duplicate row per size.

| | conservative | balanced | aggressive |
|---|---|---|---|
| risk per trade | 0.5% | 1% | 2% |
| max position | 20% | 30% | 40% |
| min signal score | 75 | 65 | 55 |
| min confidence | 0.60 | 0.45 | 0.30 |
| max open positions | 3 | 5 | 8 |
| stop distance | 2.0 ATR | 2.0 ATR | 2.5 ATR |
| target | 2.0 R | 2.5 R | 3.0 R |
| max holding | 10 bars | 15 bars | 20 bars |

These are **engineering defaults, not validated trading recommendations.** No value
has been backtested, optimised, or measured against any outcome. The ATR and R
multiples are the least justified of all: a 2-ATR stop is a convention, not a
finding. Nothing in the engine reads these constants; they are seed data
(`tradabot seed-profiles`).

---

## The decision gates

`evaluate_decision` is a pure function — no database, no portfolio state. Gates
run cheapest-first, and **which gate fires is the recorded outcome**:

| Order | Gate | Reason code | Depends on capital? |
|---|---|---|---|
| 1 | profile enabled | `PROFILE_DISABLED` | no |
| 2 | classification not NEUTRAL | `CLASSIFICATION_NEUTRAL` | no |
| 3 | score ≥ threshold | `SCORE_BELOW_THRESHOLD` | no |
| 4 | confidence ≥ threshold | `CONFIDENCE_BELOW_THRESHOLD` | no |
| 5 | shorts permitted | `SHORT_NOT_PERMITTED` | no |
| 6 | position sizes above zero | `INSUFFICIENT_CAPITAL` | **yes** |
| 7 | above broker minimum | `POSITION_BELOW_MIN_NOTIONAL` | **yes** |
| 8 | net edge positive **at this size** | `NEGATIVE_NET_EDGE` | **yes** |

Gates 1–5 reject identically for every portfolio. Gates 6–8 are where capital
enters. Recording the distinction is what lets a later analysis ask "how often
does the fee gate bite on the €50 portfolio?" — a question about the *portfolio*,
not the signal.

`require_positive_net_edge` is configurable and defaults to on. Turning it off is
supported so the counterfactual value of taking cost-negative trades can itself
be measured rather than assumed.

---

## Rejected signals are observations

`trade_decisions` records **SKIP as deliberately as TRADE**. A system that stores
only what it did cannot measure what it missed.

Skips rejected at the *economic* gate retain the position size and cost that were
under consideration. That is what makes the counterfactual answerable: to ask
"what would that rejected trade have returned, net of the fees that caused the
rejection", the size and cost must be on the record. Skips rejected earlier, on
conviction, never got that far and correctly store zero.

### Why the row is denormalised

The economics (spread, fees, slippage, capital, position size) are copied onto
the decision rather than recomputed from the signal and profile. **Profiles are
mutable.** Raising `risk_per_trade` next month must not silently rewrite the
reason a decision was made in March. The row is an immutable record of a moment.

---

## Feedback analytics (phase 4+, not implemented)

### The confusion matrix, and its limits

| | profitable outcome | losing outcome |
|---|---|---|
| **TRADE** | true positive | false positive |
| **SKIP** | false negative (missed) | true negative (avoided) |

This framing is useful for *diagnosis* and dangerous as a *scorecard*.

> **Classification accuracy is not financial performance.**

A strategy that is right 70% of the time and loses money on the other 30% by more
than it gains is a losing strategy with excellent accuracy. Conversely, one right
35% of the time with a 4:1 win/loss ratio is highly profitable. Accuracy is
silent on magnitude, and magnitude is the entire business.

Worse, the matrix is a *classification* frame applied to a *decision-theoretic*
problem. It weights a missed 0.2% move the same as a missed 15% one.

### What must actually be measured

Per profile, and per profile-pair for comparison:

**Outcome**
- net P&L (after fees, spread, slippage)
- gross P&L, and the difference — cost drag
- expected value per decision
- return vs. buy-and-hold over the same window

**Distribution**
- win rate, average winner, average loser
- profit factor (gross profit ÷ gross loss)
- maximum drawdown, and time to recover
- Sharpe, where the sample is large enough to justify one

**Behaviour**
- exposure (time in market), turnover
- fees, spread cost and slippage as separate line items
- decision counts by reason code — *which gate is doing the rejecting*

**Counterfactual**
- realised forward return of SKIP decisions
- opportunity cost: what the skipped set would have returned, net of the costs
  that caused the skip
- whether the cost gate is *correctly* selective or merely restrictive

The counterfactual is the reason skips are stored. If the €50 portfolio's skipped
trades would have been profitable *net of the fees that caused the skip*, the
gate is wrong. If they would have lost money, the gate is doing its job. Only the
decision log can distinguish these.

---

## The critical constraint on feedback

> **tradabot must never modify its trading rules in response to individual
> outcomes.**

The feedback system collects evidence. It does not act on it.

A system that raises a weight after a winning trade and lowers it after a loser is
not learning — it is fitting noise, with a feedback loop that amplifies whatever
the recent regime happened to reward. It will look excellent in-sample and fail
the moment conditions change, and by then the rules will have drifted so far from
their documented form that nobody can say what it is doing or why.

Rule changes require:

1. a sample large enough to distinguish signal from variance;
2. out-of-sample validation on data not used to motivate the change;
3. an explicit, versioned change to the engine (`ENGINE_VERSION`), so signals
   before and after remain distinguishable;
4. a written rationale that survives the change.

We want **measurement**, not self-reinforcing overfitting. This constraint is
architectural: nothing in `app/signals/` reads from `trade_decisions`, and that
dependency must not be added.

---

## Broker abstraction

`app/broker/protocols.py` defines `Broker` — interfaces only.

**Relationship to `ExecutionModel`** (`app/backtesting/protocols.py`), which looks
similar and is not the same thing:

| | `ExecutionModel` | `Broker` |
|---|---|---|
| nature | pure function | stateful counterparty |
| answers | "what price would I have got?" | "what does my account look like?" |
| holds | nothing | cash, positions, order lifecycle |

`PaperBroker` will be implemented *in terms of* an `ExecutionModel`: it delegates
the fill-price question and keeps the bookkeeping. Merging them would force a
backtester's fill calculator to carry account state it has no use for.

The interface is deliberately smaller than a real broker API — no bracket orders,
no trailing stops, no margin calls. Those get added when `PaperBroker` needs them.
An interface designed before its first implementation is a guess, and every unused
method constrains the implementation that follows.

**A `LiveBroker` is not planned.** The absence of order routing is an
architectural boundary, not a missing feature.

---

## What phase 3 built, and what it did not

**Built:** `PaperBroker`, portfolio state per profile, exit logic (stops, targets,
time exits, signal reversal), and enforcement of every risk limit that portfolio
state makes checkable.

**Still outstanding:**

- `max_daily_loss` — needs a session boundary, which arrives with the exchange
  calendar in phase 3b
- Corporate-action handling on open positions: a split changes the share count of
  something you hold
- Shorts, limit orders, partial fills, liquidity impact — each refused explicitly
  rather than approximated


---

## Personal portfolios (phase 4.1)

Three named portfolios sit alongside the nine generic profiles, each with its own
Discord channel:

| Key | Capital | Risk | Channel |
|---|---|---|---|
| `paper-100` | 100 EUR | balanced | #paper-100 |
| `paper-1000` | 1000 EUR | balanced | #paper-1000 |
| `paper-10000` | 10000 EUR | balanced | #paper-10000 |

> Experimental simulation configurations, **not financial recommendations**.

All three share the *same* risk profile, so **capital is the only variable**.
With three risk profiles as well, a difference in outcome could not be attributed
to account size — and account size is the thing being studied. A fixed per-order
fee is a large fraction of a 100 EUR round trip and negligible on a 10,000 EUR
one, which is why the same signal produces different decisions. A test asserts
that paper-100 declines a trade paper-10000 takes; if all three ever agreed on
everything, two of them would be redundant.

**Complete isolation.** Cash, equity, positions, realised and unrealised P&L,
fees, spread costs, slippage, exposure, drawdown, decisions, orders and outcomes
are per portfolio. A loss in one cannot reach another, and this is asserted by
trading in one and checking the others rather than by inspection.

The nine generic profiles are **unchanged**. They have no notification channel
and produce no portfolio messages. These three are instances of that
architecture, not a replacement for it.

Each portfolio belongs to a `TradabotUser` — one local owner today. See
[multi-user-roadmap.md](multi-user-roadmap.md).
