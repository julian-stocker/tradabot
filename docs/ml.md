# Machine learning: the plan, and the entry criteria

**No machine learning is implemented, and none should be added yet.**

This document describes how models will be introduced, what they must predict, and how
they will be evaluated. It exists now so that the data model and interfaces do not have
to be redesigned later, and so the evaluation rules are fixed *before* anyone has a
result they are attached to.

---

## Entry criterion

> Machine learning is not attempted until the phase 4 backtesting engine can evaluate
> the existing rule-based signals honestly.

The reason is not caution for its own sake. Without a trustworthy backtester there is
no way to tell a model that found signal from a model that found a leak — and the
second is far more common. A model trained on data with look-ahead bias will report
excellent cross-validated accuracy and lose money immediately.

Rule-based signals first, honestly evaluated. Then models, measured against them.

---

## What the models predict

Not a price. A single-point price forecast — "NVDA will be at 132" — is unfalsifiable
in any useful sense and cannot be position-sized.

`ProbabilisticForecast` (in `app/forecasting/models.py`) fixes the shape:

| Field | Meaning |
|---|---|
| `prob_positive_return` | P(return > 0 over the horizon) |
| `prob_exceeds_threshold` | P(return > `threshold_bps` over the horizon) |
| `threshold_bps` | The threshold. **Should default to the round-trip cost.** |
| `expected_return_bps` | Mean of the predicted distribution |
| `expected_range_low/high_bps` | Prediction interval, with stated coverage |

The single most important design point is `threshold_bps`:

> **P(profitable) is the useful question. P(up) is not.**
>
> A model that is right about direction 58% of the time is worthless if the average
> move is 8 bps and the round trip costs 19 bps. Setting the threshold to the cost
> makes the label answer the question that actually matters.

---

## Model progression

Strictly in this order. Each stage must beat the previous one out-of-sample to justify
the next.

### 1. Logistic regression baseline

Predicting `P(return > cost)` over one horizon, on the existing features.

Not a placeholder — a genuine baseline. Coefficients are inspectable, it cannot
overfit 15 features on thousands of bars in interesting ways, and it establishes the
number every later model must beat. **If logistic regression finds nothing, that is
strong evidence the features contain no exploitable signal**, and the correct response
is better features, not a bigger model.

### 2. Gradient boosting (XGBoost / LightGBM)

Justified only if the baseline shows signal *and* there is reason to believe the
relationships are non-linear or interacting. Both handle missing values natively, which
matters given warm-up nulls.

Watch for: overfitting to a specific volatility regime; feature importance dominated by
a single feature (usually a leak); and performance that collapses on the most recent
walk-forward folds.

### 3. Deep learning

**Only** if stages 1 and 2 demonstrate exploitable signal that simpler models
measurably fail to capture.

Financial time series have a low signal-to-noise ratio and a small effective sample
size — 10 years of daily bars is ~2,500 rows, which is tiny for a neural network and
much less independent than it looks. The default expectation is that deep learning
overfits here.

---

## Evaluation protocol

### Walk-forward, never random splits

```
Fold 1:  [====== train ======][purge][ test ]
Fold 2:  [========= train =========][purge][ test ]
Fold 3:  [============ train ============][purge][ test ]
```

- **Chronological only.** A random split puts near-duplicates of test rows into
  training, because adjacent bars are highly correlated. It inflates every metric.
- **Purge gap** at least as long as the forecast horizon. A label built from a 5-day
  forward return overlaps the first 5 days of the test window without it.
- **Expanding or rolling window**, chosen deliberately and stated. Expanding assumes
  old regimes stay relevant; rolling assumes they decay.
- **Hyperparameter search inside the loop**, never around it.

### Metrics that matter

| Metric | Why |
|---|---|
| **Brier score / log loss** | Probability quality, not just ranking. |
| **Reliability curve** | Does "0.6" actually happen 60% of the time? An uncalibrated probability is worse than none, because sizing consumes it as if it were real. |
| **Net-of-cost return** | The only economically meaningful outcome. |
| **Return vs. buy-and-hold** | The benchmark, always. |
| **Precision at the operating threshold** | You act on a small number of high-confidence calls, not on average behaviour. |
| **Per-fold stability** | A strategy that works in 2 of 7 folds found a regime, not an edge. |

Accuracy is deliberately absent. On an imbalanced, low-signal problem it is dominated
by the base rate and says nothing about profitability.

### Baselines every model must beat

1. Buy and hold.
2. The existing rule-based signal.
3. A constant predictor at the base rate.
4. **The same model trained on shuffled labels.** If it still looks good, the pipeline
   leaks — this is the cheapest leak detector available and should be run routinely.

---

## Label construction

Labels are where leakage usually enters.

- Forward returns are computed from bar *t+1* open (or later), never from bar *t*
  close. You cannot trade at a price you learned the bar was closing at.
- The label window must lie **entirely in the future** relative to every feature used.
- Overlapping labels (a 20-day return computed daily) create autocorrelated samples.
  Either sample non-overlapping windows or account for it — treating overlapping
  labels as independent understates the standard error dramatically.
- Threshold labels use the **cost-aware** threshold, not zero.

---

## Architectural readiness

Already in place:

- `Forecaster` protocol takes a **single bar's feature snapshot** and nothing else —
  structurally unable to see the future, the same constraint the rule engine has.
- `ProbabilisticForecast` records `model_name`, `model_version` and `features_used`,
  so a prediction can be reproduced with the code and inputs that produced it.
- The feature registry declares `warmup_bars` per feature, verified by test, so
  training data can be trimmed correctly rather than silently including nulls.
- Feature values are `float | None`, and `None` means "unknown", never zero. Imputation
  must be an explicit, fitted decision.
- `NetEdge` already separates raw prediction from cost-adjusted opportunity, so a model
  slots into the existing gate without redesign.

Still required before training anything:

- [ ] A backtesting engine that satisfies [backtesting.md](backtesting.md)
- [ ] `listed_at` / `delisted_at` for point-in-time universes
- [ ] Corporate-action adjustment (an unadjusted split is a −50% return)
- [ ] Real market data — models trained on the mock provider learn a random number
      generator
- [ ] A labelled dataset builder with purge gaps
- [ ] Experiment tracking: parameters, seed, data range, and code version per run
