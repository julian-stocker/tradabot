# Expected movement (`volatility-v1`)

## The product boundary

This is the one research result from phases 6–8 that earned production. It is
also the *only* thing tradabot claims about the future.

**OHLCV currently supports:**

- volatility estimation and expected movement magnitude
- market activity description
- historical market structure
- execution and risk context

**OHLCV currently does NOT support, with sufficient evidence:**

- reliable directional prediction
- BUY recommendation
- profitable breakout selection
- profitable range selection
- profitable stop optimisation

Phase 6 tested 130 feature × stream × horizon combinations and found 128
`NO_INFORMATION`. Phase 7 found cross-sectional ranking unstable across years.
Phase 8 found every volatility-based *strategy* at or below a buy-and-hold
baseline, with the breakout trigger actively subtracting value. Volatility
persistence survived all three.

This distinction is enforced structurally, not by convention:
`ExpectedMovement` has nowhere to put a price, target or direction, so no
downstream formatter can render one.

## What it measures

For each symbol, from stored hourly candles only:

| Field | Meaning |
|---|---|
| `regime` | LOW / NORMAL / HIGH / EXTREME, by percentile of the symbol's **own** trailing ATR% |
| `percentile` | where today sits in its last 252 hourly bars (~36 sessions) |
| `typical_range_pct` | median realised next-session range for that regime |
| `stress_range_pct` | 90th percentile of the same |
| `recent_range_pct` | actual high-low range over the last session |

Relative rather than absolute because a 2% session is ordinary for a
semiconductor and remarkable for a utility.

### Calibration (measured, frozen)

| Regime | Typical next-session range | Stress (p90) | Sample |
|---|---|---|---|
| LOW_VOL | 1.82% | 3.85% | 156,923 |
| NORMAL_VOL | 2.05% | 4.31% | 224,021 |
| HIGH_VOL | 2.30% | 4.82% | 105,123 |
| EXTREME_VOL | 2.71% | 5.83% | 68,693 |

Monotone across all four bands. EXTREME delivers ~1.5× LOW — a real separation,
quoted honestly rather than inflated.

## Freshness

Phase 8 measured the decay directly: after one session 96% of EXTREME states are
still elevated; after five, 55%.

- `VALIDITY` = 7 hours (one session). Past this an estimate is stale.
- `MAX_BAR_AGE` = 95 minutes. A stalled feed invalidates the inputs, not just
  the conclusion.

A stale estimate is **marked**, never silently dropped — disappearing would look
like the symbol became calm.

## The whole universe moves together

Volatility is common-factor driven, so the count of elevated symbols swings with
the market rather than hovering near a fixed fraction. Measured:

| Date | Median percentile | Elevated (HIGH+EXTREME) |
|---|---|---|
| 2020-10 (election) | 0.88 | 34 / 52 |
| 2023-07 | 0.62 | 18 / 52 |
| 2022-06 (bear) | 0.44 | 15 / 52 |
| 2026-08 (calm) | 0.05 | **0 / 52** |

**Zero elevated symbols is a valid and common state.** #market-trends stays quiet
then, by design.

## Storage: computed on demand

Not persisted. Every input is already in `candles`; an estimate is a pure
function of the trailing window; 52 symbols × 267 bars is a bounded read. A table
would duplicate candle-derived data, could disagree with the candles it came
from, and would need a migration to change.

Had it been persisted: ~120 bytes/row × 52 symbols × 26 cycles/day ≈ 160 KB/day,
≈ 57 MB/year, ≈ 285 MB over five years. Cheap — but bought nothing.

## Commands

```bash
tradabot volatility            # whole watchlist, ranked by percentile
tradabot volatility NVDA       # one symbol
tradabot volatility --preview  # renders the Discord view, sends nothing
```

Read-only, and no provider call: the same stored candles the scheduled jobs use.
