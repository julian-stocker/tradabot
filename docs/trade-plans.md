# Trade plans, targets and the retest experiment

## The problem, measured

A qualified bullish signal usually has nowhere structural to aim.

| Score band | in price discovery (no resistance above) |
|---|---|
| 60–75 | 74.7% |
| 75–85 | 84.0% |
| **≥85** | **94.7%** |

The stronger the signal, the less likely a structural target exists. The scanner
finds new highs; a resistance target requires prior failure overhead. They are
close to mutually exclusive.

## The retest hypothesis — and its result

**Hypothesis:** don't enter on breakout confirmation (median invalidation 497 bps,
too far to size). Wait for price to return to the broken level, which puts entry
beside support again.

**Mechanically it works:**

| | median invalidation distance |
|---|---|
| immediate entry after confirmation | **497 bps** |
| after confirmed retest | **141 bps** |

A 3.5× risk reduction, and 54 actionable episodes — above the declared floor of 30.

**Empirically it fails:**

| Group | obs n | positive | mean | episode n | epi positive |
|---|---|---|---|---|---|
| A ≥75 raw | 344 | 53.2% | +1.664% | 218 | 55.5% |
| B ≥75 immediate breakout | 307 | 53.1% | +1.688% | 199 | 54.3% |
| **C ≥75 retest confirmed** | 72 | **40.3%** | **−0.132%** | 54 | **42.6%** |
| D ≥85 raw | 82 | 53.7% | +1.867% | 59 | 54.2% |
| E ≥85 immediate breakout | 74 | 54.1% | +1.727% | 56 | 55.4% |
| **F ≥85 retest confirmed** | 13 | **15.4%** | **−3.971%** | 11 | **18.2%** |

Retest entries are **worse than the raw signal**, consistently across both score
bands and both aggregation levels.

### Why — the selection effect

Only 32% of breakouts retest at all. Waiting for one means systematically
selecting the breakouts that **stalled and came back**. The ones that ran away
never retest, and those are the winners. The rule filters *for* failure.

This is the opposite of the intended effect and it is the most useful thing the
experiment produced.

## Consequence: BUY stays disabled

The sample floor passed (54 ≥ 30) but the measured outcome is negative. Enabling
a live BUY feed on a 40.3% hit rate with a negative mean return would ship a
feature that loses money with a green tick next to it.

**BUY is not wired live.** Not for want of sample — for want of evidence it helps.
