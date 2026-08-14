# Volatility-conditioned opportunity validation (phase 9C)

**Result: `B` — volatility is useful for magnitude and risk; direction remains
unsupported.**

No BUY rule was manufactured. No candidate reached `ROBUST`.

## The question

Not "does high volatility predict up". The question was whether *conditional on
expected movement being elevated*, any existing directional feature becomes
materially more informative — turning phase 8's one robust finding into
opportunity selection.

Everything below was frozen in `app/research/phase9c.py` before outcomes were
inspected: regimes, the eleven features, the five matches, the extension bands,
the calibration multipliers and the cost scenarios.

## Part A — baseline, on corrected split-adjusted data

692,144 hourly bars. Persistence is unchanged from phase 9B:

| Horizon | Spearman ρ | n |
|---|---|---|
| 1d | **0.956** | 690,864 |
| 5d | 0.807 | 689,072 |
| 10d | 0.729 | 686,832 |
| 20d | 0.680 | 682,352 |

| Regime | Bars | Share |
|---|---|---|
| LOW_VOL | 196,266 | 28.5% |
| NORMAL_VOL | 274,967 | 40.0% |
| HIGH_VOL | 129,414 | 18.8% |
| EXTREME_VOL | 86,889 | 12.6% |

volatility-v1 was not recalibrated.

## Part C — conditioning makes direction *worse*, not better

Eleven features × four regimes × four horizons × two streams. Spreads in
positive-rate points; production-faithful, 1d shown:

| Feature | Uncond. | LOW | NORMAL | HIGH | EXTREME | ELEVATED |
|---|---|---|---|---|---|---|
| `bars_above_ema50` | −0.4 | +0.4 | −2.1 | −3.4 | +2.5 | −0.8 |
| `px_vs_ema50_pct` | −0.3 | +1.5 | +1.5 | −5.3 | −5.1 | −2.9 |
| `ret_1d_pct` | +0.9 | +4.0 | +1.6 | −4.3 | −2.9 | −3.9 |
| `rsi14` | +1.3 | +3.0 | +2.5 | −3.8 | −5.1 | −2.8 |
| `relative_strength_market_1d` | +2.5 | +5.0 | +4.2 | −3.8 | +0.7 | −2.0 |
| `score` (signal-v1) | −0.9 | +0.6 | +0.6 | −4.3 | −1.3 | −3.1 |

Two things are visible and both are negative findings:

1. **Signs flip between regimes** for nearly every feature. `ret_1d_pct` goes
   +4.0 in LOW to −4.3 in HIGH. That is not conditioning revealing structure;
   it is a feature with no stable relationship being sliced four ways.
2. **The ELEVATED column is the weakest**, not the strongest. Conditioning on
   the state this phase was built around *reduces* separation.

The largest conditional spread in any cell was 9.7pp (`ema50_slope_pct` @
EXTREME, production 3d) — but the identity of the "strongest" cell changes in
every one of the eight runs (`rel_volume`, `ema50_slope_pct`, `px_vs_ema50_pct`,
`ret_5d_pct`, at EXTREME, LOW and HIGH respectively). A winner that never
repeats is the signature of noise.

## Part D — every pre-registered match underperforms its baseline

Production-faithful, 1d. Baseline is the whole universe:

| Group | Episodes | Positive | Edge | CI |
|---|---|---|---|---|
| baseline (all rows) | 11,694 | 52.2% | — | [51.3, 53.1] |
| elevated volatility only | 5,854 | 50.8% | **−1.4pp** | [49.5, 52.0] |
| A vol + trend persistence | 761 | 50.2% | −2.0pp | [46.6, 53.6] |
| B vol + RS vs market | 4,379 | 50.9% | −1.2pp | [49.5, 52.4] |
| C vol + RS vs sector | 4,423 | 51.1% | −1.0pp | [49.7, 52.5] |
| D vol + full alignment | 1,430 | 50.0% | −2.2pp | [47.5, 52.6] |
| E vol + extended | 1,148 | 48.6% | −3.6pp | [45.8, 51.5] |

**Elevated volatility on its own costs 1.4pp of hit rate**, and the most
intuitive combination — market up, sector up, stock trending, volatility
elevated — is the second worst at −2.2pp.

Across all eight stream × horizon cells, **every match changes sign**:

| Match | Range across cells |
|---|---|
| A | −2.0 … +4.7 |
| B | −1.2 … +3.1 |
| C | −1.7 … +3.4 |
| D | −2.7 … +1.4 |
| E | −3.6 … +3.3 |

## Part D(E) — overextension: a real pattern, no asymmetry

Under elevated volatility only, the extension bands are monotone:

| Distance from EMA20 | Episodes | Positive | MFE | MAE |
|---|---|---|---|---|
| deeply below (< −2 ATR) | 1,458 | 53.1% | 0.02 | −0.02 |
| below (−2 to −0.5) | 2,947 | 51.6% | 0.02 | −0.02 |
| near EMA20 | 3,362 | 52.0% | 0.02 | −0.02 |
| extended (0.5 to 2) | 2,667 | 48.5% | 0.02 | −0.02 |
| deeply extended (> 2 ATR) | 1,148 | 48.6% | 0.02 | −0.02 |

A 4.5pp end-to-end spread — the cleanest directional pattern in the phase, and
still under the 5pp floor. Across all regimes the same cut is flat (2.0pp), so
the pattern is genuinely specific to elevated volatility.

**But there is no asymmetry.** MFE and MAE are identical to two decimals in
every band. "Overbought falls harder" is not what the data shows; what it shows
is a slightly lower probability of an up-close with unchanged excursion in both
directions — which is phase 6's finding restated, now conditional.

Note the direction of the folk claim is *reversed* here: the best band is
**deeply below** the EMA20, not above it.

## Part E — the positive result: volatility is excellent risk information

This is where volatility-v1 earns its keep. Testing containment of realised
next-session range inside the frozen phase-8 bands:

**Regime-aware bands (`typical` basis):**

| Regime | n | 0.5× | 1.0× | 1.5× | 2.0× |
|---|---|---|---|---|---|
| LOW_VOL | 195,882 | 11.9% | **54.8%** | 79.3% | 90.1% |
| NORMAL_VOL | 274,908 | 11.0% | **53.8%** | 78.8% | 90.0% |
| HIGH_VOL | 129,410 | 11.2% | **54.0%** | 79.2% | 90.1% |
| EXTREME_VOL | 86,888 | 12.1% | **54.0%** | 78.7% | 89.4% |

**`stress` basis (the p90 figure):** 91.5 / 91.3 / 91.3 / 91.1% at 1.0×.

**The control — one fixed 2.05% band for every regime:**

| Regime | Covered |
|---|---|
| LOW_VOL | 62.7% |
| NORMAL_VOL | 53.8% |
| HIGH_VOL | 45.7% |
| EXTREME_VOL | **34.6%** |

That is the whole finding in two tables. A regime-aware band is calibrated to
within **1.0pp across regimes**; a single fixed band ranges over **28.1pp**. The
regime-aware version is roughly **28× better calibrated**.

The `typical` figure is a median and delivers 54% — correctly centred, very
slightly conservative. The `stress` figure is a p90 and delivers 91.3%. Realised
medians (1.70 / 1.95 / 2.17 / 2.56) sit ~5–7% below the predictions across all
four regimes, so the model errs consistently toward caution, which is the right
direction for a risk estimate.

**Caveat:** the calibration was derived in phase 8 on largely these bars, so this
is an in-sample check of *fit*. What it establishes is that the regime split
carries the information; the fixed-band control is the part that cannot be
explained by in-sample fitting, because both are scored on identical rows.

## Part F — the opportunity matrix, populated only from measurement

|  | Direction weak | Direction strong |
|---|---|---|
| **LOW** | ✅ best directional spread +5.0pp, reverses across cells; range well bounded (median 1.70%) | — no evidence |
| **NORMAL** | ✅ +5.4pp, reverses; range bounded (1.95%) | — no evidence |
| **HIGH** | ✅ +5.8pp, reverses; range bounded (2.17%) | — no evidence |
| **EXTREME** | ✅ +9.7pp, reverses and never repeats; range bounded (2.56%) | — no evidence |

The right-hand column is empty. That is the result, not a gap in the work.

Mapping to the four hypotheses in the brief:

1. *high movement + directional evidence* → **not observed**. Elevated volatility
   reduced hit rate by 1.4pp.
2. *high movement + no directional evidence* → **this is the whole population**.
   Supports magnitude/risk description only — which is exactly what
   #market-trends already does.
3. *low movement + directional evidence* → not observed; LOW's apparent spreads
   reverse across cells.
4. *high movement + overextension* → the one measurable pattern (4.5pp,
   sub-floor, no asymmetry). Suggestive of a *wait* condition, not a signal.

## Part G — costs

Mean return edges over baseline ranged from −0.005% to +0.007%. Against a 20 bps
round trip, **every match in every production-stream cell is consumed**. Exactly
one cell in the entire phase survives 50 bps: match C at coarse-historical 20d,
+0.007% edge on 9,208 episodes.

That cell's own twin — match C at production 20d — is **−1.7pp with a −0.005%
edge**. The one cost-surviving result reverses in the other stream.

## Part H — walk-forward

Coarse historical, 1d, edge over that year's baseline:

| Match | 2020 | 2021 | 2022 | 2023 | 2024 | Verdict |
|---|---|---|---|---|---|---|
| A | +1.9 | −1.7 | **+10.9** | −0.1 | −1.4 | REVERSES |
| B | −0.1 | +1.4 | +1.3 | +2.5 | +2.7 | REVERSES |
| C | +0.8 | +2.4 | +2.0 | +3.4 | +3.5 | **stable** |
| D | −3.6 | −4.2 | **+9.0** | +0.3 | +3.8 | REVERSES |
| E | −9.2 | −0.7 | +3.0 | +1.0 | +1.8 | REVERSES |

Match C is positive in all five coarse years. **It is not bull-market drift** —
its weakest year is 2020 (+0.8) and its strongest are 2023–24 (+3.4, +3.5).

But on the production stream it reads −3.4pp in 2025 and +2.0pp in 2026. Stable
within one stream, reversing across streams. A and D swing by 12.6pp and 13.2pp
respectively, both driven almost entirely by 2022.

## Part I — decision gate

| Candidate | Episodes | Best edge | Sign stable | Costs | **Verdict** |
|---|---|---|---|---|---|
| A vol + trend | 761–~5k | +4.7pp | no | consumed | `NO_EDGE` |
| B vol + market RS | ~4.4k | +3.1pp | no | consumed | `NO_EDGE` |
| C vol + sector RS | 4.4–9.2k | +3.4pp | coarse only | 1 of 8 cells | `PROMISING` |
| D vol + full alignment | ~1.4k | +1.4pp | no | consumed | `NO_EDGE` |
| E vol + extended | ~1.1k | +3.3pp | no | consumed | `NO_EDGE` |
| Extension as *avoid* | ~11.6k | 4.5pp | untested across streams | n/a | `INSUFFICIENT` |
| **Volatility as risk information** | 687,536 | 28.1pp → 1.0pp calibration | yes, all regimes | n/a (not a trade) | **`ROBUST`** |

No directional candidate reaches `ROBUST`. C is the only one worth naming, and
it fails on cross-stream sign reversal and on cost in seven of eight cells.

## Part J — what external data is actually reachable

Probed read-only on the existing account. **No plan was enabled, nothing
purchased.**

| Source | Status |
|---|---|
| Option **chain** | ✅ readable — 14,001 SPY contracts |
| `implied_volatility` field | ❌ present in schema, returns `None` |
| `greeks` field | ❌ present in schema, returns `None` |
| Historical **option bars** | ❌ `OPRA agreement is not signed` |
| Historical chain "as of" a past date | ❌ no such parameter — only `updated_since` |
| Earnings / surprise / guidance | ❌ not offered; the 13 corporate-action types contain no earnings |
| Analyst revisions | ❌ not offered in any form |
| News | ✅ 6+ years, but headline/summary text only — unstructured |

**The decisive limitation:** even with OPRA signed, the chain endpoint returns
*current* snapshots only. A causal IV/skew backtest cannot be reconstructed
backwards — it would require storing snapshots forward from today, meaning any
IV study has a lead time measured in months before it has enough history to test.

## Production status

Unchanged: signal-v1, thresholds 75/85, WATCH/BUY/SELL disabled, paper
portfolios, volatility-v1 calibration. No Discord message sent, no webhook
printed, `.env` untouched. Six jobs healthy. No migration; head 0011.

format ✅ lint ✅ mypy --strict (205 files) ✅ **1,621 passed, 2 skipped** ✅
alembic check ✅ quick_check ✅ integrity_check ✅
