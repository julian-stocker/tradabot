# Signal intelligence

What a tradabot opportunity means, what it does not, and what the research
actually shows.

## What an opportunity is

A **setup that cleared a threshold**, described over explicit horizons, with the
evidence attached. It is not a prediction, not a recommendation, and not a
probability.

Language is deliberate throughout: OPPORTUNITY, BULLISH SETUP, WATCH,
SIMULATED BUY. Never "BUY", never "will".

## Observation vs episode

The single most important distinction in the research.

An **observation** is one scanner evaluation of one symbol at one instant. An
**episode** is one continuous opportunity, however many observations it produced.

```
NVDA qualifies 10:00 · still qualifies 11:00 · strong 12:00 · strong 13:00
```

That is **one** episode and **four** observations. Counting it as four
independent pieces of evidence inflates every sample size and narrows every
confidence interval — the most common way a backtest manufactures significance.

Episode rules (`app/research/episodes.py`), deterministic and derived, never
stored:

| Boundary | Rule |
|---|---|
| identity | `(symbol, direction)` |
| continuation | gap ≤ **24 hours** between qualifying observations |
| new episode | longer lapse, or a direction reversal |
| scoring | the episode's **first** observation, never its peak |

Scoring at the peak would be look-ahead dressed up as aggregation: a human acts
when the alert fires, not at the point that turned out to be optimal.

## What the research shows

From the phase-5.5 benchmark — 116,844 observations, 376 sessions, 52 symbols.
**617 qualifying observations collapse to 228 episodes** (2.7× inflation).

### 1-day horizon

| Band | obs n | obs positive | **episode n** | **episode positive** |
|---|---|---|---|---|
| <75 (baseline) | 116,221 | 51.6% | — | — |
| 75–80 | 290 | 59.0% | 102 | **53.9%** |
| 80–85 | 190 | 57.9% | 69 | **56.5%** |
| ≥85 | 137 | 57.7% | 57 | **61.4%** |

### 5-day horizon

| Band | obs positive | episode positive | episode mean |
|---|---|---|---|
| 75–80 | 54.2% | 54.9% | +1.64% |
| 80–85 | 53.5% | 56.5% | +2.69% |
| ≥85 | 60.0% | **70.2%** | **+3.77%** |

**The ≥75 advantage survives clustering but shrinks by roughly two-thirds** — from
+6.7pp to about +2.3pp for the 75–80 band at 1d. It concentrates in ≥85, which
*improves* under clustering.

### Why this is not proof of predictive edge

1. **n = 57 episodes at ≥85.** A 61% positive rate on 57 samples has a confidence
   interval wide enough to include 50%.
2. **One window.** February 2025 – August 2026, one universe, one regime mix.
3. **No multiple-comparison correction.** Nine bands × seven horizons × two
   aggregation levels were inspected. Some cell was going to look good.
4. **Episode statistics are compared against an observation-level baseline.**
   Non-qualifying observations are not clustered, so the comparison is
   conservative but not strictly like-for-like.
5. **Costs are still modelled**, not observed, and at 100 EUR they exceed the
   entire edge.

### The 70–75 anomaly

The band immediately *below* the threshold is the worst in the dataset — 49.0%
positive at 1d and 47.9% at 5d, both under the 51.6% baseline. It is unexplained.
It could mean the threshold sits on a genuine discontinuity, or it could be noise
in 384 observations. **It is not a reason to move the threshold**, in either
direction.

## Component attribution (descriptive)

Individual components against the 1d outcome, 116,838 observations. Baseline
positive rate 51.7%.

| Component | Range across buckets | Verdict |
|---|---|---|
| Trend (EMA spread) | 50.7% – 53.0% | U-shaped; both extremes beat the middle |
| Momentum (RSI) | 51.2% – 52.0% | flat |
| Volume (relative) | 51.2% – 52.2% | flat; "confirmed" is *below* average |
| Volatility | 51.5% – 52.2% | flat rate, but mean return and MFE/MAE scale strongly |
| Structure | BREAKDOWN 53.5%, BREAKOUT 51.2% | breakdown beats breakout |
| MTF agreement | 51.7% – 51.9% | negligible |
| Confidence | 51.3% – 52.1% | flat, slightly **inverted** |

**No single component discriminates meaningfully.** Every bucket sits within
about ±1.5pp of the base rate, while the composite score reaches 58.3% at ≥75.
Two readings are possible and this data cannot separate them: the components
interact in a way the hand-set weights happen to capture, or the weights are
overfitted to the period they were chosen in.

Volatility is worth calling out because it is easy to misread: higher volatility
raises the *mean* return sharply (0.034% → 0.421%) and widens MFE and MAE
together. That is amplitude, not edge — bigger moves in both directions.

## Interaction analysis (exploratory)

A small **predefined** set, fixed before outcomes were inspected. Nine
combinations, not a search.

| Combination | n | positive | vs base |
|---|---|---|---|
| score ≥75 (reference) | 617 | 58.3% | +6.7pp |
| score ≥75 + poor liquidity | 140 | 63.6% | +11.9pp |
| score ≥75 + high volatility | 459 | 56.6% | +5.0pp |
| strong trend + weak volume | 4,763 | 53.5% | +1.8pp |
| strong trend + strong volume | 1,687 | 53.0% | +1.3pp |
| momentum, no structure | 33,860 | 52.1% | +0.5pp |
| high MTF agreement | 43,120 | 51.9% | +0.2pp |
| low MTF agreement | 29,951 | 51.7% | +0.0pp |
| momentum + breakout | 4,221 | 51.2% | −0.5pp |

**EXPLORATORY.** The "poor liquidity" cell is the one that looks exciting and is
the one most likely to be noise: n=140, a subset of the 617, and no mechanism
explains why thin volume would help. Volume and breakout *confirmation* — the two
things the score weights most — are the two that add least.

## Horizons

Answered independently, from the timeframes that bear on each
(`app/scanner/horizons.py`).

| Horizon | Period | Evidence | Labels | Supported |
|---|---|---|---|---|
| INTRADAY | minutes → session close | 5m, 15m | 15m, 1h, 4h | yes |
| SHORT_TERM | 1–5 trading days | 1h, 15m | 1d, 3d, 5d | yes |
| MEDIUM_TERM | 1–4 weeks | 1d, 1h | 20d | yes |
| LONG_TERM | 1–6 months | — | — | **NOT_AVAILABLE** |

A stock may legitimately be `INTRADAY BULLISH · SHORT_TERM BULLISH ·
MEDIUM_TERM NEUTRAL · LONG_TERM NOT_AVAILABLE`. Collapsing that into one verdict
discards three quarters of the answer.

**NOT_AVAILABLE is not NEUTRAL.** "No opinion" and "no movement expected" are
different statements.

### Why LONG_TERM is unavailable

Three independent reasons, any one sufficient:

- **No label reaches that far.** Outcomes stop at 20 trading days, so no
  multi-month claim has ever been checked against anything.
- **No feature looks that far.** EMA 20/50, RSI 14, ATR 14, volatility 20,
  60-bar structure — the entire set has a lookback measured in weeks.
- **No fundamental input exists.** Earnings, guidance, valuation and balance
  sheet are what drive a multi-month thesis.

Adding daily candles does not fix this. Requirements are listed in
`LONG_TERM_REQUIREMENTS`.

## Opportunity lifecycle

| State | Meaning | Notifies |
|---|---|---|
| DISCOVERED | tracked, below threshold | no |
| QUALIFIED | crossed 75 | **yes, once** |
| STRONG | crossed 85 | **yes, once** |
| WEAKENED | fell materially | yes |
| INVALIDATED | premise broken | yes |
| EXPIRED | aged out | no |

Discord announces **transitions, not levels**: 72 → silence; 76 → QUALIFIED;
78 → silence; 87 → STRONG; 86 → silence; 69 → WEAKENED. State persists in
`tracked_signals`, so a restart does not re-announce an unchanged signal.

Thresholds are 75 and 85 and were not changed by any of this work.
