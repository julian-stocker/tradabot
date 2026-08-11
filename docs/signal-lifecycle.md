# Signal lifecycle

A continuing market setup keeps **one identity** across scans. Without that,
"how long has this been true?" is unanswerable and every fifteen-minute cycle
invents a fresh discovery.

---

## Two concepts, one relationship

```
TrackedSignal  (identity: a continuing setup)
      │
      └──< SignalEvaluation  (what tradabot knew at time T)
```

| | `tracked_signals` | `signal_evaluations` |
|---|---|---|
| Answers | "is this setup still on, and since when?" | "what did it look like at 14:15?" |
| Cardinality | one per setup | one per scan per symbol |
| Mutable | yes — lifecycle advances | **no** — an observation is history |

The existing `signals` table is **not** duplicated: it already stores a full
single-timeframe scored snapshot, and `signal_evaluations.primary_signal_id`
references it. What phase 4 adds is the multi-timeframe context around it.

## Identity

Two observations belong to the same signal when **all five** match, and the
existing signal is still active:

| Field | Why a change starts a new signal |
|---|---|
| instrument | obvious |
| direction | a long turning into a short is a different idea |
| primary timeframe | a 1h and a 1d setup are different claims |
| horizon | likewise |
| **setup premise** | a breakout that becomes a breakdown has been *falsified* |

The rules are deliberately strict. Merging two distinct setups loses information
irrecoverably; splitting one into two is visible and can be reasoned about later.

Only *falsifiable* structures become a setup identity: `BREAKOUT`, `BREAKDOWN`
and `CONSOLIDATION`. `RANGING` and `UNKNOWN` collapse to `UNKNOWN`, because
neither is a premise that could break, and treating them as distinct would churn
signals every time price wandered between them.

## States

```
DISCOVERED ──► QUALIFIED ──► STRONG
     │             │  ▲         │
     │             ▼  └─────────┘
     │         WEAKENED
     │             │
     ▼             ▼
  EXPIRED     INVALIDATED
```

| State | Meaning |
|---|---|
| `DISCOVERED` | Evaluated, below threshold. **The common case, still recorded.** |
| `QUALIFIED` | At or above `signal_threshold` (75) |
| `STRONG` | At or above `strong_signal_threshold` (85) |
| `WEAKENED` | Was strong, has eased but still qualifies |
| `INVALIDATED` | Fell below the threshold. Terminal. |
| `EXPIRED` | Not evaluated for `signal_expiry_hours`. Terminal. |

**`WEAKENED` exists** so a setup that dips and recovers is not recorded as two
separate discoveries.

**`EXPIRED` is not `INVALIDATED`.** One means the scanner stopped looking — a
symbol disabled, a weekend of downtime; the other means the market said no. A
future model that conflated them would learn from labels partly describing
tradabot's uptime.

**Terminal is terminal.** A recovering score starts a *new* signal rather than
resurrecting a dead one, so the record of what was invalidated, and when, stays
intact.

## Data quality and promotion

Unusable data can **downgrade** a signal but never **promote** one:

| | Promote | Downgrade |
|---|---|---|
| `OK` | yes | yes |
| `STALE` / `INSUFFICIENT` | **no** | yes |

A setup that only looks qualified on stale data is not qualified — promoting on
bad input is how a feed outage becomes a trade. A setup breaking overnight has
still broken.

## Timestamps

Written **once**, the first time each state is reached:

```
discovered_at, qualified_at, strong_at, weakened_at, invalidated_at, expired_at
last_evaluated_at, evaluation_count, current_score, peak_score
```

`qualified_at` records when the setup *first* qualified, not the last time it
happened to be qualified — a signal oscillating around the threshold otherwise
loses its own history. `peak_score` survives a decline, so "how good did this
get?" stays answerable.

## Thresholds

```
signal_threshold        75
strong_signal_threshold 85
signal_cooldown_minutes 60
minimum_score_change     5
```

**These are operational heuristics that control notification volume.** They are
not validated probabilities. A score of 85 is **not** an 85% chance of profit,
and nothing in the scoring model treats either number as special.

Worth knowing before you tune them: the baseline engine cannot reach 75 on price
trend alone. With flat volume, momentum and trend saturate near 97–99 and the
score still tops out around 63 — half the weight sits in volume, volatility,
regime and spread. Reaching 75 in practice needs a **volume step change** as well
as a trend. Expect few qualifying signals; that is the design working.

## Notification mapping

| Transition | Event |
|---|---|
| → `QUALIFIED` | `MarketSignalQualified` |
| → `STRONG` | `MarketSignalStrengthened` |
| → `INVALIDATED` (from qualified) | `MarketSignalInvalidated` |
| → `DISCOVERED`, → `WEAKENED`, → `EXPIRED` | *(silent)* |

The scanner routes all three through `NotificationService.notify_signal`; the
notification policy owns deduplication and cooldown. Deciding that in two places
would let the rules drift.
