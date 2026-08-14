# risk-v1: short-horizon movement risk

**EXPECTED_MOVEMENT_SHORT_HORIZON: `ROBUST`.**
**RISK_ENGINE: `PRODUCTION_READY`.**

`PRODUCTION_READY` means safe to use as a **risk-information service**. It does
not mean `TRADING_STRATEGY_READY`, `BUY_READY` or `LIVE_TRADING_READY`. Those
remain unmet and unrelated: eight research phases found no stable directional
information, and nothing here changes that.

## The rolling semantics

The estimate describes **movement magnitude over the next 1 or 3 trading days,
from the current information state**. It is recomputed as new bars arrive.

It explicitly does *not* describe:

- maximum loss over a position's lifetime
- expected return, direction, or a target price
- probability of profit

A position held for twenty days does not receive a twenty-day risk number at
entry. It receives a fresh one-to-three-day number every day it stays open:

```
Day 1   NVDA  NORMAL_VOL   1d band ±2.6%   3d band ±4.5%
Day 2   NVDA  NORMAL_VOL   1d band ±2.7%   3d band ±4.6%   (recomputed)
Day 3   NVDA  HIGH_VOL     1d band ±3.4%   3d band ±5.9%   (regime changed)
```

That is the canonical behaviour, and it exists because the alternative was
measured: the same model calibrates to **4.06pp at one day and 8.82pp at
twenty**, because volatility mean-reverts.

## Why only 1d and 3d

Validated out of sample on 2024–2026 with parameters frozen on 2020–2023:

| Horizon | Calibration error | Against the 5.00pp bar |
|---|---|---|
| **1 day** | **4.06pp** | **passes** |
| **3 days** | **4.12pp** | **passes** |
| 5 days | 5.16pp | fails |
| 10 days | 7.18pp | fails |
| 20 days | 8.82pp | fails |

`SUPPORTED_HORIZONS = (1, 3)` is enforced: `ShortHorizonRisk.band(5)` raises
`UnsupportedHorizonError` rather than returning a number nobody validated.

### Final validation

33,020 observations, 52 symbols, 208 cells per horizon, intended coverage 80%:

| Regime | 1d coverage | 3d coverage |
|---|---|---|
| LOW_VOL | 81.8% | 79.3% |
| NORMAL_VOL | 80.3% | 77.5% |
| HIGH_VOL | 79.4% | 78.8% |
| EXTREME_VOL | **74.3%** | 78.5% |

**Combined 1d+3d error: 4.09pp — passes.**

Worst regime cell: **EXTREME_VOL at 1 day, 5.72pp**. Worst regime-year:
EXTREME_VOL 2021 at 5.94pp. Every other regime-year sits between 0.5pp and
4.6pp.

**The EXTREME_VOL limitation is stated rather than fixed.** Its 80% band
delivered 74.3%, so it under-warns where movement is largest. Re-fitting it
would mean tuning on the validation period, which the frozen-parameter
discipline forbids. `EXTREME_UNDER_COVERAGE_NOTE` carries this warning into
every CLI rendering.

## The model

```
band(horizon) = k(regime) × √horizon × ATR%
```

Four constants, fitted on 2020–2023 and frozen. Square-root time scaling is a
random-walk property, not a fitted shape; phase 11.1 compared it against a
twenty-parameter alternative that bought 0.32pp, below the 0.50pp justification
bar set in advance.

| Regime | typical (k₅₀) | 80% band (k₈₀) | stress (k₉₅) | gap (p80) |
|---|---|---|---|---|
| LOW_VOL | 2.8154 | 4.3117 | 6.5910 | 1.5798 |
| NORMAL_VOL | 2.4688 | 3.7266 | 5.5625 | 1.5287 |
| HIGH_VOL | 2.2040 | 3.3240 | 5.0436 | 1.4575 |
| EXTREME_VOL | 2.0467 | 3.1177 | 4.8568 | 1.4940 |

`k` **falls** as the regime rises — volatility mean reversion seen from the
multiplier side. The band still widens, because ATR% is already larger.

## Interpretation

| Term | Meaning |
|---|---|
| **Expected move** | Median excursion. Half of sessions move less. |
| **Risk band** | The 80% coverage figure: four times in five, maximum excursion stayed inside it. |
| **Stress move** | The 95% figure. A wider tail estimate. |
| **Overnight gap** | 80th-percentile gap, **part of the band, not additional to it**. |

None of these is a guaranteed bound. **Markets can and do exceed them**, most
often through overnight gaps.

## Overnight gap

The gap is carried as its own field because it is not interchangeable with the
rest of the band to anyone placing a stop:

| Regime | Gap as share of the 1d band |
|---|---|
| LOW_VOL | 36.7% |
| NORMAL_VOL | 40.9% |
| HIGH_VOL | 43.6% |
| EXTREME_VOL | **50.4%** |

In EXTREME_VOL **half the day's movement can arrive before the market opens**. A
future stop engine must not assume execution at its stop price: phase 11.1
measured gap-through at 5.28% for a stop at half the band, and non-zero at every
distance tested.

## The objects

`ShortHorizonRisk` — symbol, calculated_at, bar_timestamp, model_version,
volatility_model_version, regime, percentile, atr_pct, expected_move_1d/3d,
risk_band_1d/3d, stress_move_1d/3d, overnight_gap_pct, stale, data_quality.

There is **no** direction, target, expected price, probability, buy or sell
field, and a test asserts their absence so a future edit cannot add one quietly.

`PositionRisk` — for a hypothetical open position: entry price, current price,
unrealised %, current risk, regime transition (`"NORMAL_VOL -> HIGH_VOL"`),
next-session risk amount. It **reports state and suggests no action**; deciding
what to do belongs to a position-management phase that does not exist.

`size_position()` — pure arithmetic returning `PositionSizing`: max position
value, capital required, risk amount, cost, leverage-capped flag, practical
flag. It does not choose a stop; the caller supplies one, optionally from
`minimum_noise_distance()`.

## Sizing policy by account size

Stop at the LOW_VOL 1-day band (2.46%), 20 bps round trip:

| Equity | Risk | Position | % equity | Cost/risk | Status |
|---|---|---|---|---|---|
| €100 | 0.25% | €10.16 | 10% | 8.1% | **IMPRACTICAL** |
| €100 | 0.50% | €20.33 | 20% | 8.1% | PRACTICAL |
| €100 | 1.00% | €40.65 | 41% | 8.1% | PRACTICAL |
| €1,000 | 0.25% | €101.63 | 10% | 8.1% | PRACTICAL |
| €10,000 | 0.25% | €1,016.26 | 10% | 8.1% | PRACTICAL |

| Account | Classification |
|---|---|
| **€100** | **LIMITED** — impractical below 0.5% risk, and only viable at all with fractional shares. A whole-share constraint on a €250 stock produces a €0 position. |
| **€1,000** | **PRACTICAL** at every budget tested |
| **€10,000** | **PRACTICAL** at every budget tested |

Cost is a flat 8.1% of the risk budget regardless of account size — it scales
with position, and position scales with risk. The €100 constraint is structural,
not a fee problem, and no higher-turnover approach was invented to hide it.

## Future stop interface

The engine exposes `minimum_noise_distance(horizon)` — the distance below which
a stop sits inside ordinary movement. **It is a floor, not a recommendation.**
Phase 11.1 measured a stop at half the 1-day band being touched 33.7% of the
time within a single session.

A future stop engine may consume: expected movement, ATR, gap risk, position
P/L, time in trade. Choosing the actual stop is out of scope here.

## CLI

```
tradabot risk              # whole watchlist, ranked by 1d band
tradabot risk NVDA,KO      # specific symbols
```

Read-only, no provider call — it answers from candles the sync job already
stored. Every rendering carries the magnitude-only disclaimer, the gap
explanation and the EXTREME_VOL caveat.

## Discord

**Not enabled.** The existing #market-trends volatility section already carries
regime transitions and expected range. Adding 1d and 3d bands to every symbol
would roughly double that block's length for a channel whose value is brevity.

The proposed form, if ever enabled, adds two lines to the *elevated* symbols
only:

```
NVDA — HIGH_VOL (92nd pct)
  next session  typical ±1.9%   80% band ±2.8%
  next 3 days   typical ±3.2%   80% band ±4.9%
```

That decision is deferred: the risk engine is production-ready as a service, and
a channel change is a product choice rather than a research outcome.
