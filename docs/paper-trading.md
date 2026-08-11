# Paper trading

A multi-profile virtual trading engine. No real broker, no real money, no order
routing — and no path to any of those.

> **Synthetic data validates execution and accounting mechanics only.** It says
> nothing about trading profitability, signal quality or predictive power. A
> positive P&L in the demo means the arithmetic is right, not that the strategy
> works. Do not report it as evidence of anything else.

---

## The lifecycle

```
signal ──▶ decision ──▶ sizing ──▶ order ──▶ fill ──▶ position
                                                        │
                                          ┌─────────────┴──────────────┐
                                          │      bar monitoring        │
                                          │  mark · excursions · exits │
                                          └─────────────┬──────────────┘
                                                        ▼
                                              exit ──▶ trade ──▶ performance
```

Two gates, deliberately separate:

| Stage | Question | Enum | State it consults |
|---|---|---|---|
| **Decision** (phase 2) | Does this profile *want* this trade? | `DecisionReason` | the signal |
| **Execution** (phase 3) | *Can* this portfolio do it right now? | `OrderRejectionReason` | live cash, positions, drawdown, quote age |

A €50 portfolio declining a signal on expected-edge grounds never reaches the
broker, so it has no order record at all. A €5000 portfolio that wants the trade
but has no free cash gets a stored, rejected order. Collapsing the two would lose
which stage refused — and "we didn't want it" and "we couldn't afford it" are very
different findings.

---

## Order lifecycle

`OrderStatus` is reused from the phase 2 `Broker` protocol rather than a parallel
enum:

```
                  ┌──▶ FILLED
submit ──▶ (gates) ┤
                  └──▶ REJECTED
```

`PENDING`, `PARTIALLY_FILLED` and `CANCELLED` exist in the enum but are
unreachable today: a market order fills or is refused synchronously. They are
retained for limit orders rather than invented later.

**Rejections are stored as orders.** "How often does this portfolio ask for
something it cannot have, and why" is a property of the strategy, and a system
that records only successes cannot answer it.

Every order carries an `idempotency_key` with a UNIQUE constraint:

| Kind | Key |
|---|---|
| Entry | `entry:<trade_decision_id>` |
| Exit | `exit:<position_id>:<bar_timestamp>` |

Replaying a signal or a candle is therefore a no-op at the database level, not
merely by convention.

---

## Position lifecycle

`OPEN → CLOSED`. A position carries full provenance — `originating_signal_id` and
`originating_trade_decision_id` — so every trade traces back to the evidence that
opened it.

A partial unique index enforces at most one `OPEN` position per
`(profile, instrument)` when pyramiding is disabled, in the database rather than
in a check the engine might skip.

**Long only.** `Side` is persisted and `allow_short` is configurable, but the
engine *refuses* shorts (`SHORT_NOT_SUPPORTED`) rather than simulating them
approximately. `evaluate_exit` raises `NotImplementedError` for a short. Adding
shorts is a code change with its own tests, not a migration.

---

## Portfolio accounting

The invariant:

```
equity = cash + Σ(liquidation value of open positions)
```

**Cash is a stored ledger balance**, not a figure recomputed from an event log.
Replaying events on every read is slow and fragile — one unhandled event type and
the balance silently drifts with nothing to compare against. Here the balance moves
inside the same transaction as the event that moved it.

**Equity and unrealised P&L are not stored** on the portfolio row: they depend on
current prices and would be stale the instant they were written. They are computed
on demand and captured into `portfolio_snapshots` when a valuation is worth keeping.

### Valuation is at the bid

A holder cannot realise the mid — they must sell into the bid. Marking at the mid
overstates equity by half a spread on *every* open position, which flatters both
the equity curve and the drawdown.

**Known optimism:** exit fees are not deducted from open positions, because that
exit has not happened. Equity is therefore optimistic by roughly one exit fee per
position. Documented rather than patched with an arbitrary reserve.

### Realised P&L is cash-based

```
entry outflow = entry_fill × qty + entry_fee
exit inflow   = exit_fill  × qty − exit_fee
realised      = exit inflow − entry outflow
gross         = realised + all costs
```

Defining realised P&L as *cash that actually moved* is the only definition that
cannot double-count: spread and slippage are already inside the fill prices.
Deriving `gross` from it makes `gross − fees − spread − slippage == net` true **by
construction** rather than by two calculations happening to agree. Asserted in
`test_trade_cost_breakdown_reconciles`.

---

## Execution model

```
BUY :  base = ask   then slippage pushes it UP
SELL:  base = bid   then slippage pushes it DOWN
```

Slippage is always adverse — there is no branch where it helps. Its magnitude is
`slippage_spread_multiple × half-spread`, so one configured number drives both the
paper broker and the phase 1 cost model. `test_per_leg_reconciles_with_round_trip`
pins that they agree exactly.

With no quote, the touch is reconstructed from the profile's `default_spread_bps`
and the order is flagged `used_live_quote=False`. A fallback, marked as one.

### Costs, itemised

Fees, spread and slippage are tracked separately all the way to the trade record,
because they have different natures: the fee is contractual, the spread is
observed, the slippage is an *assumption*. A single "costs" number cannot answer
"is the spread or the fee killing the small portfolio?".

The fixed fee is why portfolio size matters so much:

| Position | Round-trip cost |
|---|---|
| €50 | **415 bps** |
| €500 | 55 bps |
| €5000 | 19 bps |

*(€1.00 fee, 10 bps spread, 0.5× slippage — `TestPositionSizeCostImpact`)*

---

## Position sizing

```
risk_budget    = equity × risk_per_trade
risk_per_share = entry_price − stop_loss
quantity       = risk_budget / risk_per_share
```

Sizing by *what a loss would cost* is the only way "1% risk" means the same thing
on a volatile instrument as on a quiet one — a wider stop buys fewer shares.

The result is then **capped, never raised**, by: the position cap, available cash
(including the entry fee), and the exposure limit. The binding constraint is
recorded on every order as a `SizingConstraint`, which is a useful diagnostic:
a portfolio permanently bound by `AVAILABLE_CASH` is running a materially
different strategy from one bound by `RISK_BUDGET`.

Risk fractions apply to **current equity**, not initial capital, so a drawdown
automatically shrinks position sizes.

### No stop, no risk-based size

`risk_per_share` is the denominator. Without a stop there is no denominator, and
**tradabot will not invent one**:

- `require_stop_loss=True` (default) → refuse, reason `INVALID_STOP`
- `require_stop_loss=False` → notional sizing at `max_position_percent`, an
  explicit documented rule

Stops are placed at `stop_loss_atr_multiple` ATRs below entry, so they scale with
the instrument's own volatility. Targets are expressed in **R multiples** —
multiples of the risk distance — making the reward:risk ratio a configured number
rather than an accident of two unrelated settings.

---

## Exit rules

| Reason | Trigger |
|---|---|
| `STOP_LOSS` | bar low ≤ stop, or bar opened below it |
| `TAKE_PROFIT` | bar high ≥ target, or bar opened above it |
| `MAX_HOLDING_PERIOD` | `bars_held ≥ max_holding_bars` |
| `SIGNAL_REVERSAL` | caller closes on a flipped signal |
| `SIMULATION_END` | run finished with positions open |
| `MANUAL` | explicit close |

### Same-bar ambiguity

A candle `open=100, high=110, low=90, close=105` with `stop=95, target=108`
touched **both**. OHLC is four numbers; the path between them is lost.

> **The simulator must never resolve the ambiguity in its own favour.**

| Policy | Behaviour |
|---|---|
| **`CONSERVATIVE`** (default) | Assume the **stop** hit first |
| `OPTIMISTIC` | Assume the target — provided *only* to measure how much a result depends on the guess |
| `INTRABAR_DATA_REQUIRED` | Raise, rather than guess |

Ambiguous exits are flagged on the position (`exit_was_ambiguous`), so results
resting on an unresolvable guess can be identified and quantified rather than
silently absorbed.

### Gaps

A stop at 100 does **not** fill at 100 when the market opens at 95 — there was no
trade at 100 to fill against.

| Situation | Fill |
|---|---|
| Opened below the stop | **the open** (worse) |
| Opened above the target | **the open** (better) |
| Opened exactly at a level | the level; not flagged as a gap |

Favourable gaps are real and are not clipped back to the target — that would be
pessimism for its own sake. Gaps cluster where losses are largest, so getting this
wrong is worst exactly where it matters most. Flagged via `exit_was_gap`.

**Gap-through-stop beats an intrabar target**: a bar that opened below the stop was
already stopped out, even if it later rallied through the target. This is the case
a naive implementation books as a winner.

---

## Risk limits

| Limit | Where enforced | Status |
|---|---|---|
| `min_signal_score`, `min_confidence` | decision stage | enforced |
| `require_positive_net_edge` | decision stage | enforced |
| `max_position_percent` | sizing | enforced |
| `max_total_exposure` | sizing + execution gate | enforced |
| `max_open_positions` | execution gate | enforced |
| `max_drawdown` | execution gate + sticky halt | enforced |
| `allow_pyramiding` | execution gate + DB index | enforced |
| `max_quote_age_seconds` | execution gate | enforced |
| `max_daily_loss` | — | **stored, not enforced** (needs a session boundary) |

A drawdown breach sets `halted_reason` on the portfolio. The halt is **sticky**: a
recovering equity curve does not quietly resume trading. Clearing it is a
deliberate act.

---

## Idempotency and restart

Everything lives in the database; the engine holds no state between calls.

- **Same signal twice** → same `entry:<decision_id>` key → one order, one position
- **Same candle twice** → same `exit:<position>:<timestamp>` key → one close, and
  the snapshot for that timestamp is overwritten rather than duplicated
- **Same position closed twice** → `virtual_trades.position_id` is UNIQUE

Restart recovery needs no special case: `ensure_portfolio` finds the existing row,
open positions are still `OPEN`, and the next bar continues monitoring them.
`TestRestartRecovery` opens a position in one session, discards all in-memory
state, and exits it correctly in a fresh one.

### Transactions

The broker **never commits**. Order, position and cash mutations flow through the
caller's session so "order filled + cash reduced + position created" is one
transaction. `TestTransactionAtomicity` rolls back mid-entry and asserts no order,
no position, and untouched cash.

---

## Performance metrics

Derived from stored trades and snapshots, never cached — a cached metric and its
record eventually disagree.

`win_rate` and `profit_factor` return `null` below 5 trades. A win rate from three
trades is noise with a decimal point.

**No Sharpe ratio.** It needs a return series with a defined periodicity, and
snapshots are event-driven — one per processed bar per instrument. A Sharpe from
irregularly spaced observations looks authoritative and means nothing. It arrives
when a proper return-series design does.

`cost_drag_pct` is reported alongside the return, because together they answer the
only question that matters for a small portfolio: was there an edge, or did the
fees eat it?

---

## Counterfactual tracking

`decision_outcomes` records what happened *after* a decision — for `SKIP` as well
as `TRADE`. A cost gate that rejects everything looks identical, in a P&L report,
to one that is correctly protecting the portfolio. The difference is only visible
by measuring the rejected set.

Two load-bearing properties:

1. **It never touches portfolio state.** A bug here produces a wrong number in a
   report; it cannot corrupt a balance.
2. **Returns are GROSS.** The counterfactual position was never sized, and costs
   depend on size. Comparing a gross counterfactual against a net realised return
   is an error; the field name says so.

It is a data structure and a measurement service, not an analytics engine. See
[simulation-design.md](simulation-design.md) for the feedback constraint: this
records evidence and **nothing reads it back into the trading rules**.

---

## Running it

```bash
make migrate
make demo-simulation      # deterministic; identical output every run
```

Or via the API (read-only):

```
GET /api/v1/simulation/overview
GET /api/v1/simulation/profiles/{name}/portfolio
GET /api/v1/simulation/profiles/{name}/positions
GET /api/v1/simulation/profiles/{name}/orders
GET /api/v1/simulation/profiles/{name}/trades
GET /api/v1/simulation/profiles/{name}/performance
```

There is **no endpoint that opens a position, moves cash, or edits a trade**. The
simulation is driven by the engine; HTTP observes it. An editable P&L history is
not a record of anything.

---

## Limitations

1. **Long only.** Shorts are refused, not approximated.
2. **Market orders only.** `LIMIT` is declared but raises.
3. **No partial fills.** An order fills entirely or is rejected.
4. **No liquidity or market-impact model.** Size is not checked against bar volume,
   so a large position in an illiquid name is unrealistically easy to fill.
5. **`max_daily_loss` is stored but not enforced** — it needs a session boundary
   that the exchange-calendar work will provide.
6. **Holding periods are counted in bars**, not trading days.
7. **No corporate-action handling on open positions.** A split changes the share
   count of something held; the adjustment layer covers price series, not positions.
8. **Equity excludes exit costs** on open positions.
9. **Historical quotes are not stored**, so `as_of` simulations use the configured
   default spread.
10. **Synthetic data throughout.** Mechanics validated; profitability not.
