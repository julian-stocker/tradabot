# Filing-event validation (phase 10.2)

**`NO_EVENT_INFORMATION`** for magnitude. **`NO_STABLE_DIRECTIONAL_INFORMATION`**
for direction.

Phase 10.1 noticed, *after* looking at results, that post-filing windows seemed
to lift regardless of EPS direction. This phase pre-registered that observation
and tested it properly. It does not survive.

## The population, rebuilt from filings rather than from EPS

Phase 10.1 derived events from XBRL `EarningsPerShareDiluted` facts, so a company
whose EPS did not normalise had **no events at all**. Rebuilding from filing
submissions fixes that:

| Symbol | Phase 10.1 events | Actual filings (10-Q / 10-K) |
|---|---|---|
| BRK.B | **0** | 83 / 28 |
| V | **0** | 57 / 19 |
| AMZN | 61 | 88 / 26 |
| XOM | **1** | see below |

**XOM is a genuine CIK discontinuity.** The ticker→CIK map returns the *current*
registrant, and a 2026 reorganisation created `ExxonMobil Holdings Corp`
(CIK 2115436) holding one filing. The company's real history — 98 10-Q and
31 10-K back to 1994 — lives under legacy CIK **34088**. Merged in explicitly.

This generalises: any ticker whose registrant was reorganised loses its history
to a silent zero, and counting filings per symbol is the check that catches it.

**Total population: 5,800 filings** (1994–2026) across 52 symbols.

## Usable causal events

**1,166**, spanning 2020-11-06 → 2026-07-17, all 52 symbols (860 10-Q, 306 10-K).

| Reason skipped | n |
|---|---|
| Predates the 2020-07-27 price floor | 4,509 |
| Insufficient forward bars | 48 |
| No pre-event volatility regime | 77 |

The 4,509 are not recoverable: 2020-07-27 is the provider floor on the free IEX
feed, established in phase 10.1.

### Event time

The information instant is SEC `acceptanceDateTime`; entry is the first stored
bar **strictly after** it.

| Acceptance | Share |
|---|---|
| Pre-market (< 13:30 UTC) | 13% |
| Regular session | 27% |
| After hours (≥ 20:00 UTC) | **59%** |

Daily bars carry an 04:00 UTC stamp, so a pre-market filing's entry lands on the
*next* session even though that day was tradeable. **The rule is therefore
conservative for 13% of events** — it forgoes a session rather than risk using
one it could not have. Entering late costs drift; entering early would be
fabrication.

A boundary bug was found by the tests and fixed: `bisect_left` returns the bar
*at* an exact timestamp match, which is "at or after" rather than strictly after.
`bisect_right` is correct. No filing in this dataset landed exactly on a bar
stamp, so results were unchanged — the fix is insurance.

## Matched controls

Same stock, same pre-event volatility regime, 10–60 sessions away, and no filing
within ±5 sessions. **1,131 of 1,166 matched (97%).**

## Event versus matched non-event

The primary question. Lift is event |return| against its matched control:

| Horizon | Event \|ret\| | Control \|ret\| | Lift | Range lift |
|---|---|---|---|---|
| 1d | 1.470 | 1.364 | **+7.7%** | +7.0% |
| 3d | 2.462 | 2.404 | +2.4% | +2.0% |
| 5d | 3.195 | 3.212 | −0.5% | −1.0% |
| 10d | 4.514 | 4.696 | −3.9% | −4.4% |
| 20d | 6.116 | 6.902 | **−11.4%** | −9.6% |

The effect is a **one-day bump that decays to nothing by 5 days and goes
negative after**. At 1d the confidence intervals overlap substantially
(event [1.387, 1.558], control [1.277, 1.448]), and +7.7% is well under the
frozen 15% materiality threshold.

**The 20d figure is contaminated and should not be read as a finding.** The
control exclusion window is ±5 sessions, shorter than a 20-session horizon, so a
control starting 6 sessions before a filing has that filing *inside* its forward
window. Controls at 20d therefore contain event behaviour and events do not. The
1d and 3d numbers are the trustworthy ones.

10-K windows are consistently weaker than 10-Q: at 20d, 49.3% positive
(mean +0.476) against 56.2% (mean +1.708).

## Volatility-v1 around filings

**It stays calibrated.** Realised 1-day range against the frozen phase-8 bands:

| Pre-event regime | n | Within typical | Within stress |
|---|---|---|---|
| LOW_VOL | 287 | 47.4% | 91.6% |
| NORMAL_VOL | 401 | 51.9% | 90.8% |
| HIGH_VOL | 253 | 54.2% | 91.7% |
| EXTREME_VOL | 225 | 61.8% | 95.6% |

Stress coverage of 91–96% matches the 91.3% measured on all bars in phase 9C.
**A filing does not break the risk model.** EXTREME-regime filings are slightly
*calmer* than predicted (61.8% within typical against ~54% expected), so if
anything volatility-v1 over-warns around filings for already-volatile names.

Same-regime magnitude lift at 5d: −6.4% / +3.9% / +0.8% / −4.2% — small and
sign-inconsistent. **A filing adds no magnitude information beyond
volatility-v1.**

## Initial reaction (H2)

Reaction split at within-year 30/70 quantiles: 349 negative, 452 neutral, 349
positive.

| Forward | Neg | Neutral | Pos | Continuation | Reversal |
|---|---|---|---|---|---|
| 3d | 51.6% | 54.4% | 52.7% | +1.1pp | −1.1pp |
| 5d | 56.2% | 54.0% | 57.9% | +1.7pp | −1.7pp |
| 10d | 55.6% | 55.3% | 57.9% | +2.3pp | −2.3pp |
| 20d | 53.9% | 52.2% | 57.6% | **+3.7pp** | −3.7pp |

Continuation grows monotonically with horizon but never reaches the 5pp floor,
and every interval overlaps. Mean returns separate more than rates do
(20d: +2.549% positive against +1.041% negative), which is drift concentrated in
a tail rather than a directional edge.

## Market and sector confirmation

Confirmation **dilutes** the effect rather than stabilising it:

| Cell | 5d spread | 20d spread |
|---|---|---|
| stock only | +1.7pp | **+3.7pp** |
| + market aligned | −0.0pp | +2.7pp |
| + sector aligned | +2.0pp | +1.6pp |
| + market and sector | −0.2pp | +1.3pp |

At 20d the spread falls monotonically as confirmation is added. Independent
confirmation does not improve stability here.

## Extension (descriptive)

At 5d, extension at entry looks like the largest single number in the phase —
and it vanishes:

| Extension at entry | 5d positive | 20d positive |
|---|---|---|
| < −2 ATR | 46.2% | 54.7% |
| −2 to +2 ATR | 56.3% | 54.1% |
| > +2 ATR | 59.6% | 55.2% |

13.4pp at 5d, **0.5pp at 20d**. A short-horizon artefact, and exactly the kind of
cell that would have been promoted if this phase had gone looking for one.

## Year stability — the disqualifier

| Metric | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|
| \|ret\| 5d lift vs control | −10.3% | −6.7% | +1.9% | **+14.7%** | −4.8% | +6.1% |
| Continuation spread 5d | −7.9p | +6.2p | −1.6p | +7.9p | **+11.1p** | **−12.1p** |
| Continuation spread 20d | −3.2p | +0.0p | +7.9p | **+17.5p** | +4.8p | **−12.1p** |

Every row reverses sign. The magnitude lift swings 25pp, the 20d continuation
spread 30pp.

## Multiple-comparison discipline

Four pre-registered primary families (event vs control magnitude; incremental
information beyond volatility-v1; initial reaction; confirmation matrix). About
**75 individual cells** were computed across all sections. Nothing here selects a
best cell — the classification is driven by the pre-registered thresholds, and
both land null.

## Classification

| Question | Verdict | Why |
|---|---|---|
| **Event magnitude** | `NO_EVENT_INFORMATION` | +7.7% at 1d, under the frozen 15% floor; decays by 5d; year-unstable |
| **Direction** | `NO_STABLE_DIRECTIONAL_INFORMATION` | max +3.7pp, under the 5pp floor; reverses every year |

Cost testing was not run: the brief gates it on a candidate surviving stability,
and none did.

## What filing information could still be used for

Magnitude is not robust, so it cannot size a position. But two descriptive uses
are supported by what was measured:

- **Entry avoidance.** 59% of filings are accepted after the close, and the next
  session carries a measurable if modest magnitude bump. A scanner that knows a
  filing landed overnight can decline to treat that bar's move as a normal
  signal — not because the move is predictable, but because it is *explained*.
- **Nothing for risk sizing.** volatility-v1 already covers filing windows at
  91–96% stress coverage, so a filing-specific risk adjustment would add
  parameters without adding accuracy.

No BUY rule follows from any of this.
