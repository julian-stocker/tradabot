# ML readiness

**No model is trained in phase 5, and none should be until the dataset is large
enough to falsify one.** This document records what exists, what is missing, and
the validation rules any future model must obey.

## Why a random train/test split is invalid here

This is the most important thing on the page, because scikit-learn's default
would produce a beautiful number and a worthless model.

`train_test_split(X, y)` assumes rows are exchangeable. These rows are not:

1. **Consecutive observations are near-duplicates.** The scanner evaluates every
   symbol every 15 minutes. Two adjacent rows for NVDA share almost all their
   feature values, so a random split puts near-copies of a test row into training.
   The model memorises rather than generalises, and the test score measures the
   memorisation.

2. **Label windows overlap.** A `5d` label computed at 10:00 and another at 11:00
   share four and a half days of future bars. The *answer* to the test question is
   partly contained in a training row's label.

3. **Time flows one way.** A random split trains on August and tests on June. A
   model that knows the future regime is not a model that could have traded.

4. **Cross-sectional correlation.** Fifty-two large-cap US equities at the same
   instant are one market observation wearing fifty-two hats. On a big down day
   every row is negative together; the effective sample size is far below the row
   count.

Any of these alone inflates validation scores. Together they can make a coin flip
look like alpha.

## What a future model must do instead

### Walk-forward validation

Train on `[t0, t1)`, validate on `[t1, t2)`, test on `[t2, t3)`, then roll the
whole window forward. Every split boundary is a timestamp; no row from after a
boundary may influence a model evaluated before it.

### Purging

Drop training rows whose **label window** overlaps the test period. A row stamped
`t` with horizon `h` occupies `[t, t+h]`, and if that interval touches the test
window it must be removed from training even though its feature timestamp is
safely in the past. This is what `label_end_timestamp` is exported for.

### Embargo

After each test window, discard a further gap of training rows. Serial
correlation means the bars immediately following a test period still carry
information about it. Embargo by whole sessions using `session_date`.

### Grouping

Cluster by `symbol` and `session_date` when estimating uncertainty. Fifty-two
correlated rows are not fifty-two independent observations, and a confidence
interval that assumes they are will be several times too narrow.

## Columns provided for this

| Column | Used for |
|---|---|
| `reference_timestamp` | ordering; split boundaries |
| `label_end_timestamp` | purging overlapping label windows |
| `session_date` | embargo; session-level grouping |
| `symbol`, `sector` | cross-sectional grouping |
| `tracked_signal_id` | identifying one continuing setup across many rows |
| `label_status` | excluding unlabelled rows explicitly |
| `feature_set_version`, `signal_model_version`, `scanner_policy_version`, `label_policy_version`, `cost_model_version` | refusing to pool incompatible rows |

## Version compatibility

Five version fields travel with every row. Rows produced under different versions
describe different systems and **must not be pooled silently** -- a feature set
that changed mid-window makes a single trained model a model of two things. Check
the manifest before combining exports.

## What is missing before training is reasonable

1. **Sample size.** Deep intraday history is weeks, not years (see
   docs/research-dataset.md). After purging and embargo the usable independent
   sample is far smaller than the row count suggests.
2. **Survivorship-correct universe.** No `listed_at`/`delisted_at`, so the
   historical universe is today's survivors. Any model trained on it inherits the
   bias.
3. **Observed transaction costs.** Every historical cost is `MODELLED`. A model
   optimising net return is optimising against an assumption.
4. **A calibration harness.** `ProbabilisticForecast` requires *calibrated*
   probabilities; reliability curves and Brier scores are not built yet, and an
   uncontrolled probability is worse than none because position sizing consumes it
   as if it were real.

## Target shape

When a model is eventually built, it should implement the existing
`Forecaster` protocol and return a `ProbabilisticForecast` -- probabilistic,
horizon-explicit, cost-aware. `prob_exceeds_threshold` should default its
threshold to the round-trip cost: **P(profitable) is the question, P(up) is not**.
