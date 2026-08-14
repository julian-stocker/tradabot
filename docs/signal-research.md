# Signal research: what the data does and does not contain

## Phase 6 result: `CURRENT_FEATURE_SET_INADEQUATE`

130 feature × stream × horizon analyses over 426,229 causally-joined
observations. **128 returned NO_INFORMATION.** The largest end-to-end separation
in positive rate produced by *any* single feature was 5.5 percentage points, and
the two analyses that exceeded the 5pp floor were the same feature, which
reverses sign between years and between streams.

This is not "the model needs tuning". Signal-v1's components were never the
problem: the inputs themselves do not separate outcomes.

## What was tested

| Family | Features | Best \|spread\| | Verdict |
|---|---|---|---|
| Trend | price vs EMA20/50/200, EMA slopes, stacking, bars above EMA50 | 2.9pp | NO_INFORMATION |
| Momentum / exhaustion | RSI, RSI change, returns 1h/4h/1d/5d, acceleration, EMA distance in ATR | 1.3pp | NO_INFORMATION |
| Volatility | ATR, ATR%, ATR percentile, realised vol, range/body vs ATR | 4.0pp | NO_INFORMATION |
| Volume | relative volume, volume acceleration | 1.4pp | NO_INFORMATION |
| Structure | breakout/breakdown, distance from 20-bar high/low, price discovery | 1.4pp | NO_INFORMATION |
| Market context | equal-weight proxy return, breadth, relative strength vs market/sector | 5.5pp | REGIME_DEPENDENT — **superseded, see phase 9A** |
| Time of day | opening / early / midday / late | 2.1pp | NO_INFORMATION |

Horizons 1d, 5d and 20d. Streams: production-faithful (2025-02→2026-08, four
timeframes) and coarse (2020-08→2024-07, H1+D1). Everything episode level under
the frozen 24-hour rule.

## Two phase-5.9 findings did not survive

**RSI exhaustion did not reproduce.** Phase 5.9 saw positive rate fall
55.4% → 40.3% across RSI quartiles. Under phase-6 methodology RSI is flat in all
four stream/horizon combinations (+1.3, −0.5, +0.4, +1.0pp). The original was
measured on the ≥85 subset — 262 *observations*, not episodes — which is both a
selected sub-population and far too small.

**ATR extension does not asymmetrically worsen downside.** MAE does deteriorate
across ATR quartiles (−0.9% → −2.2% at 1d), but MFE deteriorates by the same
factor (1.0% → 2.4%). The MFE/|MAE| ratio is essentially constant (1.11 → 1.09).
ATR is a volatility scaler: it makes both tails bigger and tells you nothing
about which one you will get.

## Setup quality is not separable from entry risk

Five predefined interactions pairing a trend/strength dimension with an
exhaustion/extension dimension. All 20 cells landed between 49.9% and 53.0%,
with MFE and MAE flat across every cell. The appealing hypothesis — "good
company, bad moment" — is not visible in this data.

## The features are one variable wearing many hats

| Pair | r |
|---|---|
| RSI ↔ distance from EMA20 in ATR | **+0.978** |
| 1-day return ↔ relative strength vs market | +0.950 |
| RSI ↔ distance from EMA50 in ATR | +0.897 |
| price vs EMA20 ↔ price vs EMA50 | +0.874 |
| ATR% ↔ realised volatility | +0.817 |

Signal-v1 gives separate weights to momentum, trend and volatility. At r = 0.98
between RSI and EMA-distance, those are not independent votes — they are one
measurement counted repeatedly, which is why the aggregate never behaved like a
diversified score.

## Phase 9A: the market-context row does not survive a real reference

Market context was the only family above the 5pp floor, and it was measured
against a proxy built from the same 52 symbols it was providing context for.
Phase 9A added SPY, QQQ and nine sector funds — a reference the universe does
not appear in — and re-ran the family across 1d/3d/5d/20d.

**111 of 112 analyses returned `NO_INFORMATION`**, and the exception flips sign
between adjacent horizons on the study's smallest subsample. Proxy and real
agreed on the verdict in all 32 pairings. Walk-forward by year reversed sign for
every feature tested. Classification: `NO_ADDITIONAL_INFORMATION`.

The clinching number is redundancy: the new real feature correlates **+0.978**
with the proxy it replaces, and **+0.906** with the stock's own 1-day return. It
is the same measurement wearing a third hat — which is the pattern this document
already described at r = 0.978 between RSI and EMA-distance.

The 5.5pp itself was `proxy_breadth_stacked`, which clears the floor in one of
four cells and swings from −9.8 to +4.6 across years. Breadth is also the one
context feature an ETF cannot replace, being inherently cross-sectional.

Separately, phase 9A found the research pipeline had been reading **unadjusted**
candles: sixteen splits appeared as one-bar returns of −95% to +686%, and
`atr_pct` was overstated 24× at the tail. This is now fixed. It did **not**
change any phase-6 verdict — rank-based bucketing absorbed it — but it did
matter for magnitude features. See [market-context.md](market-context.md).

## Phase 10.1: the first external directional source also fails

SEC EDGAR was selected as the first genuinely independent information source —
free, 17 years deep, exact `acceptanceDateTime`, restatements preserved. The
frozen hypothesis was as-first-reported diluted EPS YoY growth > +20% versus
< −20%, entered at the first bar after SEC acceptance.

On 167 events it looked like the largest directional separation this project had
seen (+8 to +11pp). On **916** events — the same hypothesis, the same
thresholds, 52 symbols instead of 10 — the spread is −0.1pp at 1d, +4.4pp at 5d,
and **inverts to −4.5pp and −6.0pp** at 10d and 20d. Every year reverses.

A matched baseline (same symbol, same year) shows all three buckets lifting at
5d, with DECLINE *beating* STRONG_GROWTH at 10d and 20d. The filing event
carries something; EPS growth direction does not order it.

Classification: `NO_STABLE_DIRECTIONAL_INFORMATION`. See
[options-collection.md](options-collection.md).

## Why no signal-v2 was built

The brief's own rule: *do not manufacture a composite before validating its
components.* No component validated. A weighted blend of features that
individually separate outcomes by under 5pp — and which are 80–98% correlated
with each other — cannot produce a stable edge, and constructing one would only
move the overfitting from the components into the weights.

## Reproducing this

```bash
python -m app.cli research walkforward --run-id 4 --folds 9 --horizons 1d,5d
```

Feature construction lives in `app/research/featureset.py`, analysis in
`app/research/phase6.py`. Causality is enforced structurally and tested by
appending a violent future bar and asserting no earlier feature value moves
(`tests/unit/test_featureset_causality.py`).
