# The ML dataset

Phase 4 does not train anything. It **collects the data a future model would
need**, and the design decisions that make that data usable are worth stating
now, because most of them are impossible to fix retroactively.

---

## X and Y are separate tables

```
signal_evaluations   →  X: what tradabot knew at time T
(phase 5)            →  Y: what happened afterwards
```

A `signal_evaluations` row contains **only values knowable at `evaluated_at`**.
Outcome labels — returns after 1h/1d/3d/5d/20d, maximum favourable and adverse
excursion, stop hit, target hit — arrive in phase 5, in their own table, joined
on evaluation id.

**This separation is structural, not a convention.** A label in the input row
would leak the first time someone wrote `SELECT *`, and a model trained on that
would report excellent performance that vanishes in production. A test asserts
that no outcome-shaped column exists in the table.

## Every evaluation is stored

Not only the qualified ones. Not only the traded ones. **Every one.**

| | Stored | Notified |
|---|---|---|
| Score 12, neutral | ✅ | ❌ |
| Score 60, below threshold | ✅ | ❌ |
| Score 78, qualified | ✅ | ✅ |
| Score 88, strong | ✅ | ✅ |
| Insufficient data | ✅ | ❌ |
| Stale data | ✅ | ❌ |
| Outside market hours | ✅ | ❌ |

A score-60 signal is never announced and is always stored, and its forward
outcome stays measurable.

**Why this is the most important requirement in the phase.** A classifier needs
negatives to learn a boundary. A dataset containing only what cleared a
notification threshold teaches survivorship, and worse, the filtering is
correlated with the very thing the model would be predicting — a selection bias
that looks, in evaluation, exactly like signal. It cannot be corrected for
afterwards, because the discarded rows do not exist.

Notification thresholds control **message volume only**. They never gate
persistence, `TradeDecision` writes, counterfactual tracking or outcome storage.

## What a row contains

```
identity      instrument, evaluated_at, market_data_timestamp
verdict       score, confidence, classification, direction, qualified
context       agreement, aligned, timeframe_states (all four, individually)
metrics       trend / momentum / volume / volatility / structure / liquidity
economics     expected_move_bps, cost_bps, net_edge_bps, expected_horizon
liquidity     bid, ask, spread_bps, quote_age_seconds
explanation   reason_codes, risk_codes
provenance    data_quality, session_phase
versions      feature_set_version, signal_model_version, scanner_policy_version
```

### Timeframe states are stored individually

Not collapsed into the score. A future model must be able to inspect what the
scanner actually saw on each timeframe — including which one disagreed — and a
single blended number destroys exactly the information that made computing four
timeframes worthwhile.

### `market_data_timestamp` is separate from `evaluated_at`

The gap between them is the data's age. Storing only one makes staleness
unauditable after the fact, and "was this evaluation made on fresh data?" is a
question a future model will need answered per row.

### Version columns are mandatory

Rows produced by different feature sets or scoring models are **not comparable**.
Without the versions, a model silently trains on a moving target and nobody can
tell which rows are which. Three separate versions because the three change
independently.

## No look-ahead

Enforced in several places, each cheap and each covering a different mistake:

- **Swing points** are only reported once confirmed by later bars. An
  unconfirmed swing at the last bar might still be exceeded.
- **Features** load only bars at or before `as_of` (phase 1 `LookAheadError`).
- **Paper execution** happens strictly after the signal bar; the engine raises
  rather than filling on it.
- **The evaluation row** carries nothing derived from after `evaluated_at`.

## Interpreting the data later

Two things a future analysis must not do.

**Do not treat `qualified` as a label.** It records whether a score cleared a
configurable threshold on that day, under that policy version. It is a fact about
tradabot's configuration, not about the market.

**Do not ignore the base rate.** Every scan records `hit_rate`. Fifty
instruments scanned 26 times a day is 1,300 evaluations; at any plausible
false-positive rate a threshold produces hits daily whether or not the signal
predicts anything. The multiple-comparisons hazard has been recorded in
`app/scanner/models.py` since phase 1 for precisely this reason.

## Volume

At 50 symbols and 26 scans a day: ~1,300 evaluations/day, ~475,000/year. Each
row carries several JSON blobs. SQLite handles this comfortably for a year or
two; see [scanner.md](scanner.md#sqlite) for when to move.

Retention: **do not prune by score.** Pruning the low scores would delete exactly
the negatives that make the dataset trainable. If storage ever needs managing,
prune by *age*, uniformly.
