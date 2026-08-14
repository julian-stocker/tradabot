# Expected movement and risk (phases 11 and 11.1)

**EXPECTED MOVEMENT: `PROMISING_BUT_INSUFFICIENT`.**
**RISK ENGINE READINESS: `RESEARCH_READY`.**

Phase 11 missed a 5.00pp bar by 0.09pp at a single horizon. Phase 11.1 tested
three pre-registered horizon-aware forms across all five horizons and **the gate
still fails** — see [Phase 11.1](#phase-111-calibration-hardening) at the end.
The failure is now precisely located: short horizons calibrate, long ones do
not.

## The frozen specification

volatility-v1 was audited and **no defect found**. Nothing in it was changed.

| Property | Value |
|---|---|
| Model version | `volatility-v1` |
| Input | H1 bars, split-adjusted on read (phase 9B) |
| ATR | Wilder, period 14, on classic true range |
| ATR% | ATR ÷ latest close × 100 |
| Percentile | rank of current ATR% within trailing **252** hourly bars (~36 sessions) |
| LOW_VOL | percentile [0.00, 0.25) |
| NORMAL_VOL | [0.25, 0.70) |
| HIGH_VOL | [0.70, 0.90) |
| EXTREME_VOL | [0.90, 1.01) |
| Minimum history | 60 bars; refused below |
| Freshness | estimate valid 7h; bars stale after 95 min |

One convention worth stating: the percentile ranks the current ATR% against a
window that **includes itself**, so it can reach exactly 1.0. That matches
`_rank_last` in the research featureset and is a convention, not a defect.

## Dataset

**95,192 observations** — one per symbol per session at the session's final
hourly bar, 2020-08-06 → 2026-07-16. Regime from the trailing hourly window;
forward movement measured on daily bars, which give clean session boundaries and
the opens that gap analysis needs.

The 12 benchmark ETFs are excluded from calibration (they are context, not
candidates); the modelling sections use the **52 tradable stocks**.

| Regime | Observations |
|---|---|
| LOW_VOL | 35,218 |
| NORMAL_VOL | 36,764 |
| HIGH_VOL | 14,806 |
| EXTREME_VOL | 8,404 |

## Movement distributions

Maximum absolute excursion, in percent, by regime and horizon:

| Regime | h | p50 | p70 | p80 | p90 | p95 |
|---|---|---|---|---|---|---|
| LOW | 1d | 1.41 | 1.98 | 2.45 | 3.29 | 4.23 |
| LOW | 5d | 3.54 | 4.94 | 6.06 | 8.03 | 10.11 |
| LOW | 20d | 7.93 | 10.82 | 13.02 | 16.90 | 21.41 |
| NORMAL | 1d | 1.61 | 2.24 | 2.76 | 3.70 | 4.74 |
| NORMAL | 5d | 3.85 | 5.33 | 6.52 | 8.69 | 11.07 |
| HIGH | 1d | 1.82 | 2.54 | 3.11 | 4.13 | 5.28 |
| HIGH | 5d | 4.12 | 5.63 | 6.92 | 9.30 | 11.83 |
| EXTREME | 1d | 2.21 | 3.14 | 3.95 | 5.49 | 7.42 |
| EXTREME | 5d | 4.62 | 6.34 | 7.82 | 10.57 | 13.65 |
| EXTREME | 20d | 8.79 | 11.93 | 14.41 | 19.02 | 23.50 |

**Regime separation decays with horizon.** At 1d, EXTREME moves 1.57× LOW; at
20d only 1.11×. Volatility mean-reverts, so a regime label is most informative
about tomorrow and nearly uninformative about next month.

## The representation that actually works

Three ways to state an 80% band were compared out of sample (bands fitted on
2020–2023, tested on 2024+):

| Representation | Mean \|calibration error\| | Parameters |
|---|---|---|
| One global band | **13.55pp** | 4 |
| Per-symbol band | 7.19pp* | 52 × 4 |
| **k(regime) × ATR%** | **5.09pp** | **4** |

\* measured on the six watched names; the global figure there is 27.29pp.

A single global band is unusable per symbol: it covers **29% for TSLA** and
**100% for KO**. The reason is structural — the *regime* is relative to each
symbol's own history, but a band stated in percent is absolute, and a stock in
LOW_VOL can still be TSLA.

Normalising by the symbol's own ATR fixes it with four constants and beats
per-symbol lookup tables:

```
band(horizon, regime, 80%) = k(regime) × √horizon × ATR%
```

| Regime | k (5d) | k ÷ √h across 1/3/5/10/20d |
|---|---|---|
| LOW | 9.71 | 3.85, 4.17, 4.34, 4.49, 4.64 |
| NORMAL | 8.35 | 3.56, 3.68, 3.73, 3.81, 3.85 |
| HIGH | 7.43 | 3.34, 3.33, 3.32, 3.32, 3.30 |
| EXTREME | 6.97 | — |

√t scaling holds well, and best where it matters: for HIGH_VOL `k/√h` is flat to
three decimals. LOW drifts up ~21%, which is volatility mean-reverting *upward*
out of calm states. `k` falls as the regime rises because ATR% is already larger
there — the same mean reversion seen from the other side.

## Calibration

Out of sample, using the normalised model, claimed against actual coverage:

| Year | LOW | NORMAL | HIGH | EXTREME |
|---|---|---|---|---|
| 2020 | 81.6% | 79.7% | 79.5% | 73.0% |
| 2021 | 79.8% | 79.9% | 80.6% | 77.2% |
| 2022 | 80.8% | 81.0% | 79.0% | 82.4% |
| 2023 | 78.9% | 79.2% | 80.8% | 83.5% |
| 2024 | 77.8% | 76.5% | 76.6% | 82.3% |
| 2025 | 74.8% | 76.2% | 78.3% | 81.6% |
| 2026 | 78.5% | 74.6% | 77.5% | 73.4% |

**Worst single-year error: 7.0pp. Pooled out-of-sample error: 5.09pp.**

The contrast with the global lookup is the whole argument for normalising. That
band, tested the same way, ranged from **68.0% in 2022 to 87.2% in 2023** — it
under-warned by 12pp in the bear market, which is precisely the year someone
would have consulted it. The normalised model delivers 79–82% in 2022 because
ATR% itself rose.

### Why this is not `ROBUST`

`MAX_CALIBRATION_ERROR` was frozen at **5.00pp** before any measurement. The
result is **5.09pp**. That is a miss by 0.09pp, and the threshold does not move
after the fact — the entire value of a pre-registered bar is that it binds when
the result is close.

## Market and sector volatility add nothing

| Model | Mean \|calibration error\| |
|---|---|
| Stock volatility only | **5.11pp** |
| Stock × market volatility ratio | 13.79pp |

Scaling by market volatility makes calibration **2.7× worse**. The stock's own
ATR already contains the market component, and multiplying by it double-counts.
Rejected.

## Gap risk

A stop cannot be honoured at its requested price through an overnight gap, so
the split matters:

| Regime | Gap median | Gap p90 | Gap p99 | Next-day intraday median | Gap share |
|---|---|---|---|---|---|
| LOW | 0.37% | 1.31% | 3.56% | 1.60% | 18.9% |
| NORMAL | 0.45% | 1.58% | 4.18% | 1.79% | 20.1% |
| HIGH | 0.51% | 1.80% | 4.95% | 2.02% | 20.2% |
| EXTREME | 0.66% | 2.56% | **9.38%** | 2.40% | 21.5% |

**Roughly a fifth of movement arrives overnight, in every regime.** The share is
remarkably stable; what changes is the tail — an EXTREME-regime p99 gap of 9.38%
is far beyond any stop distance one would set.

## Stop-touch probabilities (research, not a strategy)

How often normal noise reaches a symmetric stop at k × ATR%:

| Regime | Multiple | 1d touch | 5d touch | 20d touch | Gapped through, 1d |
|---|---|---|---|---|---|
| NORMAL | 0.5× | 68.9% | 84.7% | 91.9% | **28.17%** |
| NORMAL | 1.0× | 55.0% | 77.7% | 88.3% | 15.30% |
| NORMAL | 2.0× | 30.9% | 62.8% | 80.2% | 4.45% |
| NORMAL | 3.0× | 15.3% | 49.4% | 72.6% | 1.63% |
| EXTREME | 2.0× | 27.1% | 53.6% | 71.1% | 5.23% |
| EXTREME | 3.0× | 12.8% | 38.3% | 60.0% | 2.55% |

Two things a reader should take from this:

- **A 0.5× ATR stop is noise.** It is touched about 69% of the time within one
  day, and **gapped straight through 28% of the time** — meaning more than a
  quarter of its stop-outs would fill somewhere other than the requested price.
- Even a 3× ATR stop is touched by half of all 5-day windows. Stop distance is
  not a lever for avoiding losses; it is a choice about how much normal noise to
  tolerate.

## Position sizing (hypothetical; nothing is traded)

Assuming a stop at 2× ATR% and 20 bps round trip. Median ATR%: LOW 0.57%,
NORMAL 0.70%, HIGH 0.85%, EXTREME 1.06%.

| Equity | Risk | Regime | Stop | Position | % equity | Cost | Cost ÷ risk |
|---|---|---|---|---|---|---|---|
| €100 | 0.25% | LOW | 1.14% | €21.95 | 21.9% | €0.04 | **17.6%** |
| €100 | 0.25% | EXTREME | 2.12% | €11.81 | 11.8% | €0.02 | 9.4% |
| €100 | 2.00% | LOW | 1.14% | €100.00 | **100%** (capped) | €0.20 | 10.0% |
| €1,000 | 0.50% | LOW | 1.14% | €438.92 | 43.9% | €0.88 | 17.6% |
| €10,000 | 1.00% | EXTREME | 2.12% | €4,723.60 | 47.2% | €9.45 | 9.4% |

Three consequences worth stating plainly:

1. **Cost consumes 9–18% of the risk budget** before the position moves. A
   tighter stop makes this worse, not better, because it buys a larger position.
2. **A €100 account is barely viable.** At 0.25% risk the position is €21.95 —
   just above the €20 practical floor — and at 2% risk the sizing formula hits
   the no-leverage cap, meaning the stop no longer defines the risk.
3. **Lower volatility implies a larger position**, which is correct but
   counter-intuitive: risk is held constant, so a tighter stop buys more shares.

## Discord proposal (designed, not enabled)

The existing volatility section already carries regime transitions. The proposed
addition states bands and nothing else:

```
EXPECTED MOVEMENT

NVDA — HIGH_VOL
  1d   typical ±1.8%   80% band ±3.1%
  5d   typical ±4.1%   80% band ±6.9%

Magnitude only. Not a direction forecast.
```

No target, no BUY/SELL, no probability of an increase, no bullish/bearish. The
type system already enforces this — `ExpectedMovement` has nowhere to put a
price or a sign, and a test asserts that.

**Recommendation: keep it internal for now.** The engine is
`PROMISING_BUT_INSUFFICIENT`, and publishing a band that misses its own claimed
coverage by 5pp would put a number in front of a person who would reasonably
size a position from it.

## Paper-trading readiness

Phase 11 solves **"how much can this move and how much should I risk"**. It does
not touch **"what should I buy"**, and after eight phases that question remains
unanswered.

Still missing before volatility-v1 could serve as a risk layer:

- calibration inside the frozen 5pp bar (currently 5.09pp);
- a fill model that accounts for gapping through a stop — 4–5% of 2× ATR stops
  and 28% of 0.5× ATR stops do not fill at their requested price;
- a decision about the €100 account, where cost is 17.6% of the risk budget;
- **an opportunity-selection layer that does not exist**, because no directional
  evidence supports one.

The risk half is nearly ready. The direction half has no candidate.

## Options collector

Seventh job healthy: `runs = 3`, exit 0, firing on its 20-minute interval and
declining correctly outside the window (`no capture: 15:42 UTC is outside the
capture window`). **0 snapshots so far** — the first capture is due in the
19:30–21:00 UTC window. IV and greeks availability unchanged at ~78.5% of
contracts. No historical IV has been fabricated.


## Phase 11.1: calibration hardening

Three candidate representations, frozen before any validation outcome was seen,
fitted on **2020–2023 only** (44,311 observations) and scored on **2024–2026**
(33,020 observations), 52 stocks, intended coverage 80%.

| Candidate | Parameters | Validation error |
|---|---|---|
| `k(regime) × ATR%` (horizon-agnostic) | 4 | **19.05pp** |
| `k(regime, horizon) × ATR%` | 20 | **5.55pp** |
| `k(regime) × ATR% × √horizon` | 4 | **5.87pp** |

The horizon-agnostic form is hopeless across horizons, as expected — one band
cannot be right at both one day and twenty. The interesting comparison is the
other two: **twenty parameters buy 0.32pp over four**, against a 0.50pp
justification bar frozen in advance. **Not justified.** The parsimonious √t form
wins on the pre-registered rule.

Pooling HIGH_VOL and EXTREME_VOL was measured and made calibration *worse*
(5.99pp against 5.87pp), so EXTREME keeps its own parameter.

### Where the error actually lives

Mean absolute calibration error by horizon, √t model:

| Horizon | Error | Against the 5.00pp bar |
|---|---|---|
| 1d | **4.06pp** | passes |
| 3d | **4.12pp** | passes |
| 5d | 5.16pp | fails |
| 10d | 7.18pp | fails |
| 20d | **8.82pp** | fails |

Coverage by regime shows why. At 20 days LOW_VOL delivers 69.1% against a
claimed 80% — the band is too narrow — while EXTREME_VOL delivers 81.6%. That is
volatility mean reversion stated as a calibration error: a stock that is calm
today does not stay calm for a month, and one that is wild today does not stay
wild. The 20-parameter model shows the same shape (73.8% and 78.1%), which is
the evidence that this is **structural, not a fitting failure**.

Higher coverage levels calibrate *better* — 90% at 4.48pp, 95% at 3.27pp. The
tails are easier to bound than the middle.

### Year stability

Mean absolute error by regime and year, √t model, all years:

| Regime | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|
| LOW | 2.6 | 3.6 | 1.9 | 3.3 | 4.6 | **6.1** | 3.3 |
| NORMAL | 1.0 | 1.2 | 1.6 | 1.9 | 4.3 | 4.7 | 5.6 |
| HIGH | 1.1 | 1.1 | 0.6 | 1.0 | 2.8 | 2.3 | 3.0 |
| EXTREME | 5.5 | 3.3 | 3.2 | 4.2 | 2.0 | 4.5 | 5.5 |

Worst regime-year 6.13pp (LOW_VOL, 2025); worst horizon 20d at 8.82pp; worst
regime LOW_VOL at 6.35pp. No single year dominates — the development years are
not systematically better than the validation years, which is what rules out
leakage.

### The gate

| Condition | Result |
|---|---|
| Validation error ≤ 5.00pp | **✗ 5.87pp** |
| No obvious leakage | ✓ chronological split, fit takes no validation argument |
| No single year dominates | ✓ worst regime-year 6.13pp |
| No catastrophic EXTREME_VOL failure | ✓ 4.0pp mean, its own parameter justified |
| Improvement is out of sample | ✓ +13.18pp over the horizon-agnostic baseline |
| Complexity justified | ✓ four parameters chosen over twenty |

**Five of six pass. The one that matters fails.** `EXPECTED_MOVEMENT` stays
`PROMISING_BUT_INSUFFICIENT` and `RISK_ENGINE_READINESS` stays `RESEARCH_READY`.

### Calibrated gap and stop-touch

With the calibrated 1-day 80% band as the reference distance:

| Regime | 1d band | p80 gap | Gap as share of band |
|---|---|---|---|
| LOW | 2.46% | 0.90% | 36.7% |
| NORMAL | 2.60% | 1.07% | 40.9% |
| HIGH | 2.81% | 1.23% | 43.6% |
| EXTREME | 3.30% | 1.66% | **50.4%** |

In EXTREME_VOL the overnight gap alone is **half the entire one-day band**, which
is the clearest possible argument for reporting it separately rather than
folding it into one number.

Stop-touch against the calibrated band (NORMAL_VOL):

| Stop | 1d touch | 5d touch | 20d touch | Gapped through |
|---|---|---|---|---|
| 0.5 × band | 33.7% | 64.8% | 81.3% | 5.28% |
| 1.0 × band | 9.3% | 41.0% | 67.2% | 0.97% |
| 2.0 × band | 0.9% | 13.9% | 43.0% | 0.14% |

Gap-through is non-zero at every distance. A stop is a request, not a guarantee.

### Sizing under the calibrated stop

The calibrated stop is wider than phase 11's 2×ATR, which changes two things:
**the no-leverage cap no longer binds anywhere**, and cost falls from 9–18% of
the risk budget to **6.1–8.1%**.

| Equity | Risk | Position (LOW) | Executable? |
|---|---|---|---|
| €100 | 0.25% | €10.18 | **no** — under the €20 floor |
| €100 | 0.50% | €20.36 | yes, barely |
| €100 | 1.00% | €40.72 | yes |
| €1,000 | 0.25% | €101.80 | yes |
| €10,000 | 0.25% | €1,017.97 | yes |

**€100 is structurally impractical below 0.5% risk.** At 0.25% the position is
€10 — under any sensible minimum — and in EXTREME_VOL it stays impractical up to
0.5%. That is reported rather than engineered around: making €100 viable would
require either a larger risk budget than the account can absorb or a
high-turnover approach nothing in this project's evidence supports.
