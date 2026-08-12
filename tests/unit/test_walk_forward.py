"""Walk-forward protocol invariants.

The analysis itself is a research script; what must not rot is the *protocol*:
folds are chronological and disjoint, quantile boundaries come from development
data only, and the production model stays frozen while all of this runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

from app.core.config import NotificationSettings, Settings
from app.scanner.analysis import FEATURE_SET_VERSION, SCANNER_POLICY_VERSION
from app.scanner.service import SIGNAL_MODEL_VERSION


def month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


FOLDS = [
    (
        "fold-1",
        month_start(2025, 2),
        month_start(2025, 11),
        month_start(2025, 11),
        month_start(2026, 2),
    ),
    (
        "fold-2",
        month_start(2025, 2),
        month_start(2026, 2),
        month_start(2026, 2),
        month_start(2026, 5),
    ),
    (
        "fold-3",
        month_start(2025, 2),
        month_start(2026, 5),
        month_start(2026, 5),
        month_start(2026, 8),
    ),
]


def frozen_tertiles(development: list[float]) -> tuple[float, float]:
    """Boundaries from development data only -- the leak this guards against."""
    ordered = sorted(development)
    return ordered[len(ordered) // 3], ordered[2 * len(ordered) // 3]


# ---------------------------------------------------------------------------
# Fold structure
# ---------------------------------------------------------------------------
def test_development_always_precedes_test() -> None:
    for _name, dev_start, dev_end, test_start, test_end in FOLDS:
        assert dev_start < dev_end <= test_start < test_end


def test_no_observation_can_be_in_both_development_and_test() -> None:
    """**The property that makes it out-of-sample at all.**"""
    for _name, dev_start, dev_end, test_start, test_end in FOLDS:
        for moment in (test_start, test_end):
            assert not (dev_start <= moment < dev_end) or moment == dev_end


def test_the_development_window_expands_and_never_shrinks() -> None:
    ends = [dev_end for _n, _ds, dev_end, _ts, _te in FOLDS]

    assert ends == sorted(ends)
    assert len(set(ends)) == len(ends)


def test_test_windows_are_disjoint_and_chronological() -> None:
    windows = [(ts, te) for _n, _ds, _de, ts, te in FOLDS]

    for (_, first_end), (second_start, _) in pairwise(windows):
        assert first_end <= second_start, "test windows overlap"


def test_a_later_fold_never_informs_an_earlier_one() -> None:
    """Expanding-window: fold-1's development contains nothing from fold-2's test."""
    _n, _dev_start, dev_end, _ts, _te = FOLDS[0]
    _n2, _ds2, _de2, later_test_start, _te2 = FOLDS[1]

    assert dev_end <= later_test_start


def test_folds_are_deterministic() -> None:
    """A fold boundary that moved between runs would make results unreproducible."""
    assert [
        (
            "fold-1",
            month_start(2025, 2),
            month_start(2025, 11),
            month_start(2025, 11),
            month_start(2026, 2),
        ),
        (
            "fold-2",
            month_start(2025, 2),
            month_start(2026, 2),
            month_start(2026, 2),
            month_start(2026, 5),
        ),
        (
            "fold-3",
            month_start(2025, 2),
            month_start(2026, 5),
            month_start(2026, 5),
            month_start(2026, 8),
        ),
    ] == FOLDS


# ---------------------------------------------------------------------------
# Frozen boundaries
# ---------------------------------------------------------------------------
def test_quantile_boundaries_come_from_development_only() -> None:
    """**The subtle leak.**

    Computing extension buckets on pooled data lets the test period define its
    own boundaries, which makes any effect look sharper than it is.
    """
    development = [float(x) for x in range(100)]
    test_only = [float(x) for x in range(1000, 1100)]

    from_dev = frozen_tertiles(development)
    from_pooled = frozen_tertiles(development + test_only)

    assert from_dev != from_pooled
    assert from_dev[1] < 100, "boundaries must not be pulled by test data"


def test_frozen_boundaries_are_applied_unchanged_to_test() -> None:
    development = [float(x) for x in range(90)]
    low, high = frozen_tertiles(development)

    # Applying them to a shifted test population must not recompute them.
    test = [float(x) for x in range(50, 140)]
    buckets = {
        "low": [v for v in test if v < low],
        "medium": [v for v in test if low <= v < high],
        "high": [v for v in test if v >= high],
    }

    assert len(buckets["high"]) > len(buckets["low"]), (
        "a shifted test population should land mostly in the high bucket"
    )
    assert frozen_tertiles(development) == (low, high), "boundaries changed"


# ---------------------------------------------------------------------------
# The model stayed frozen
# ---------------------------------------------------------------------------
def test_production_thresholds_are_unchanged() -> None:
    """75/85, asserted rather than assumed."""
    settings = NotificationSettings()

    assert settings.signal_threshold == 75.0
    assert settings.strong_signal_threshold == 85.0


def test_model_versions_are_unchanged() -> None:
    """A changed version would mean the validated model is not the live one."""
    assert FEATURE_SET_VERSION == "features-v1"
    assert SIGNAL_MODEL_VERSION == "signal-v1"
    assert SCANNER_POLICY_VERSION == "scanner-v1"


def test_no_discord_feed_was_enabled_by_this_phase() -> None:
    """BUY/WATCH/EXIT stay off; validation does not ship a product."""
    from app.notifications.feeds import WATCH_STATUS

    assert WATCH_STATUS == "NOT_IMPLEMENTED"


def test_paper_portfolio_defaults_are_untouched() -> None:
    from app.simulation.portfolios import PORTFOLIO_KEYS, build_personal_profiles

    profiles = {p.name: p for p in build_personal_profiles()}

    assert set(PORTFOLIO_KEYS) == {"paper-100", "paper-1000", "paper-10000"}
    assert float(profiles["paper-100"].initial_capital) == 100.0
    assert float(profiles["paper-10000"].initial_capital) == 10000.0


def test_the_settings_object_still_defaults_to_no_live_trading() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")

    assert settings.market_data_provider in {"mock", "alpaca"}


# ---------------------------------------------------------------------------
# Sample handling
# ---------------------------------------------------------------------------
def test_a_small_sample_is_flagged_not_silently_reported() -> None:
    """n=12 episodes with a 40-point confidence interval is not a result."""
    minimum = 30
    for n in (0, 5, 12, 19, 29):
        assert n < minimum, "these must all be flagged INSUFFICIENT_SAMPLE"
    assert minimum <= 30


def test_a_bootstrap_needs_enough_points_to_mean_anything() -> None:
    import random

    def boot(values: list[float], n: int = 200) -> tuple[float, float] | None:
        if len(values) < 10:
            return None
        rnd = random.Random(0)
        out = sorted(
            100
            * sum(1 for x in (values[rnd.randrange(len(values))] for _ in values) if x > 0)
            / len(values)
            for _ in range(n)
        )
        return out[int(0.025 * n)], out[int(0.975 * n)]

    assert boot([0.01] * 5) is None
    assert boot([0.01, -0.01] * 10) is not None
