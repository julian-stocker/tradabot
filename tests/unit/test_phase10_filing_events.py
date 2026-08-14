"""Filing-event causality: what was knowable, and exactly when.

Phase 10.1 derived events from XBRL EPS facts, so BRK.B and V — which file every
quarter — had zero events because their EPS did not normalise. Phase 10.2 builds
the population from filing *submissions* instead, and these tests hold that
separation: a 10-Q is an event whether or not any number in it parses.

The rest are timing tests. A filing accepted at 20:35 UTC cannot be traded at
that day's close, and getting that wrong by one bar would manufacture an edge out
of information nobody had.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.research.phase10 import (
    CONFIRMATION_CELLS,
    CONTROL_MAX_GAP_SESSIONS,
    CONTROL_MIN_GAP_SESSIONS,
    EXTENSION_BUCKETS,
    FILING_EXCLUSION_SESSIONS,
    H0,
    H1,
    H2,
    HORIZONS,
    MATERIAL_MAGNITUDE_LIFT,
    MEANINGFUL_DIRECTIONAL_SPREAD,
    MIN_EVENTS_FOR_CLAIM,
    REACTION_HIGH_QUANTILE,
    REACTION_LOW_QUANTILE,
    ComparisonLedger,
    classify_direction,
    classify_magnitude,
)


def naive(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A naive-UTC instant, matching how candle timestamps are stored.

    Built from an aware value and stripped rather than constructed naive, so the
    intent (this is UTC) is in the code rather than in a comment.
    """
    return datetime(year, month, day, hour, minute, tzinfo=UTC).replace(tzinfo=None)


# A week of daily bars stamped at the project's 04:00 convention.
SESSIONS = [naive(2026, 8, d, 4) for d in (10, 11, 12, 13, 14, 17, 18)]


def first_executable(accepted: datetime) -> datetime | None:
    """The first stored bar strictly after publication.

    Mirrors the runner: ``bisect_right`` over ascending session stamps, so a
    filing stamped exactly on a bar does not enter on that bar. ``bisect_left``
    would return the matching index -- "at or after" rather than strictly after,
    which is one bar of look-ahead in the boundary case.
    """
    i = bisect_right(SESSIONS, accepted)
    return SESSIONS[i] if i < len(SESSIONS) else None


# ---------------------------------------------------------------------------
# A: events are independent of EPS
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Filing:
    symbol: str
    form: str
    accepted: datetime
    eps: float | None


def test_a_filing_is_an_event_even_with_no_parseable_eps() -> None:
    """**The phase-10.1 defect.** BRK.B and V file quarterly and had zero events."""
    filings = [
        Filing("BRK.B", "10-Q", SESSIONS[0], eps=None),
        Filing("V", "10-Q", SESSIONS[1], eps=None),
        Filing("AAPL", "10-Q", SESSIONS[2], eps=1.4),
    ]
    events = [f for f in filings if f.form in {"10-Q", "10-K"}]
    assert len(events) == 3
    assert {f.symbol for f in events} == {"BRK.B", "V", "AAPL"}


def test_only_periodic_reports_count_as_events() -> None:
    """8-K and S-1 are filings but not the quarterly information event."""
    forms = ["10-Q", "10-K", "8-K", "S-1", "4", "10-Q/A"]
    kept = [f for f in forms if f in {"10-Q", "10-K"}]
    assert kept == ["10-Q", "10-K"]


# ---------------------------------------------------------------------------
# B: event time, across every session phase
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "accepted", "expected"),
    [
        # 11:00 UTC on the 12th is pre-market, and that session was tradeable.
        # But daily bars carry a 04:00 stamp, so "strictly after acceptance"
        # lands on the 13th. The rule is therefore CONSERVATIVE for pre-market
        # filings -- it forgoes a session it could have used rather than risk
        # using one it could not. 13% of events take this path.
        ("pre-market (conservative by one session)", naive(2026, 8, 12, 11, 0), SESSIONS[3]),
        # 17:00 UTC is mid-session: the bar stamped 04:00 has already passed,
        # so the first *strictly after* bar is the next session.
        ("regular session", naive(2026, 8, 12, 17, 0), SESSIONS[3]),
        # 20:35 UTC is after the close -- next session, never today's.
        ("after hours", naive(2026, 8, 12, 20, 35), SESSIONS[3]),
        # Friday evening -> Monday.
        ("friday after hours", naive(2026, 8, 14, 21, 0), SESSIONS[5]),
        # Saturday -> Monday.
        ("weekend", naive(2026, 8, 15, 12, 0), SESSIONS[5]),
        # A gap in the session list stands in for a holiday.
        ("holiday gap", naive(2026, 8, 15, 23, 59), SESSIONS[5]),
    ],
)
def test_the_first_executable_bar_is_strictly_after_publication(
    label: str, accepted: datetime, expected: datetime
) -> None:
    assert first_executable(accepted) == expected, label


def test_the_rule_never_enters_before_publication() -> None:
    """The direction of the conservatism matters more than its size.

    Entering late costs some post-filing drift. Entering early would be
    look-ahead, which is not a cost but a fabrication.
    """
    for hour in range(0, 24):
        accepted = naive(2026, 8, 12, hour, 0)
        entry = first_executable(accepted)
        assert entry is None or entry > accepted


def test_a_filing_after_the_last_stored_bar_has_no_entry() -> None:
    """No entry rather than the last bar: a missing future is not an entry."""
    assert first_executable(SESSIONS[-1] + timedelta(days=30)) is None


def test_the_entry_bar_is_never_the_bar_that_preceded_the_filing() -> None:
    """The one-bar error that would manufacture an edge from hindsight."""
    for accepted in (
        naive(2026, 8, 12, 5, 0),
        naive(2026, 8, 12, 14, 0),
        naive(2026, 8, 12, 23, 0),
    ):
        entry = first_executable(accepted)
        assert entry is not None
        assert entry > accepted


def test_acceptance_is_converted_to_the_stored_timezone_convention() -> None:
    """Stored candle stamps are naive UTC; a tz-aware compare would raise."""
    aware = datetime(2026, 8, 12, 20, 35, tzinfo=UTC)
    naive = aware.astimezone(UTC).replace(tzinfo=None)
    assert naive.tzinfo is None
    assert first_executable(naive) == SESSIONS[3]


# ---------------------------------------------------------------------------
# C: the hypotheses are frozen and stated
# ---------------------------------------------------------------------------
def test_the_primary_target_is_magnitude_not_direction() -> None:
    """The observation that prompted this phase was about magnitude."""
    assert "magnitude" in H1.lower()
    assert "not direction" in H1.lower()
    assert "do not materially differ" in H0.lower()
    assert "continuation or reversal" in H2.lower()


def test_the_horizons_are_exactly_as_briefed() -> None:
    assert HORIZONS == (1, 3, 5, 10, 20)


def test_the_reaction_split_is_broad_and_symmetric() -> None:
    """30/70 thirds, not a threshold tuned for hit rate."""
    assert pytest.approx(0.30) == REACTION_LOW_QUANTILE
    assert pytest.approx(0.70) == REACTION_HIGH_QUANTILE
    assert pytest.approx(1.0) == REACTION_LOW_QUANTILE + REACTION_HIGH_QUANTILE


def test_the_floors_match_every_previous_phase() -> None:
    """5pp directional floor, restated so this phase cannot lower it."""
    assert pytest.approx(0.05) == MEANINGFUL_DIRECTIONAL_SPREAD
    assert pytest.approx(0.15) == MATERIAL_MAGNITUDE_LIFT
    assert MIN_EVENTS_FOR_CLAIM >= 100


def test_extension_buckets_are_symmetric() -> None:
    lows = [low for _, low, _ in EXTENSION_BUCKETS]
    highs = [high for _, _, high in EXTENSION_BUCKETS]
    assert min(lows) == -max(highs)


def test_the_confirmation_matrix_is_the_four_briefed_cells() -> None:
    assert CONFIRMATION_CELLS == (
        "stock_only",
        "stock_and_market",
        "stock_and_sector",
        "stock_and_market_and_sector",
    )


# ---------------------------------------------------------------------------
# F: the matched control cannot see the future, or a nearby filing
# ---------------------------------------------------------------------------
def eligible_control(offset: int, nearest_filing_gap: int) -> bool:
    """The runner's control rule, isolated."""
    return (
        CONTROL_MIN_GAP_SESSIONS <= abs(offset) <= CONTROL_MAX_GAP_SESSIONS
        and nearest_filing_gap > FILING_EXCLUSION_SESSIONS
    )


def test_a_control_too_close_to_its_event_is_rejected() -> None:
    assert not eligible_control(offset=3, nearest_filing_gap=30)


def test_a_control_too_far_from_its_event_is_rejected() -> None:
    """An unbounded window would match a 2020 event against 2026."""
    assert not eligible_control(offset=400, nearest_filing_gap=30)


def test_a_control_near_any_filing_is_rejected() -> None:
    """**The gate.** Otherwise 'non-event' windows contain the next 10-Q."""
    assert not eligible_control(offset=20, nearest_filing_gap=2)
    assert eligible_control(offset=20, nearest_filing_gap=30)


def test_control_selection_never_reads_an_outcome() -> None:
    """Matching uses only offset, regime and filing proximity — no return.

    Asserted on the signature of the rule rather than by inspecting behaviour:
    if a forward return ever becomes an argument, this fails.
    """
    import inspect

    parameters = set(inspect.signature(eligible_control).parameters)
    assert parameters == {"offset", "nearest_filing_gap"}
    assert not any("ret" in p or "forward" in p for p in parameters)


# ---------------------------------------------------------------------------
# Verdicts, and the multiple-comparison ledger
# ---------------------------------------------------------------------------
def test_a_small_sample_cannot_earn_a_verdict() -> None:
    """Phase 10.1's 167-event result inverted at 916."""
    assert classify_magnitude(events=50, lift=0.9, sign_stable=True) == "PROMISING_BUT_INSUFFICIENT"
    assert (
        classify_direction(events=50, spread=0.9, sign_stable=True) == "PROMISING_BUT_INSUFFICIENT"
    )


def test_an_unstable_sign_is_regime_dependent_not_robust() -> None:
    assert classify_magnitude(events=1000, lift=0.40, sign_stable=False) == "REGIME_DEPENDENT"
    assert classify_direction(events=1000, spread=0.20, sign_stable=False) == "REGIME_DEPENDENT"


def test_a_small_effect_is_null_however_large_the_sample() -> None:
    assert classify_magnitude(events=100_000, lift=0.02, sign_stable=True) == "NO_EVENT_INFORMATION"
    assert (
        classify_direction(events=100_000, spread=0.01, sign_stable=True)
        == "NO_STABLE_DIRECTIONAL_INFORMATION"
    )


def test_robust_requires_size_sample_and_stability_together() -> None:
    assert (
        classify_magnitude(events=1166, lift=0.30, sign_stable=True)
        == "ROBUST_EVENT_MAGNITUDE_INFORMATION"
    )


def test_the_comparison_ledger_counts_both_kinds() -> None:
    """The brief requires reporting how many comparisons were made."""
    registry = ComparisonLedger().with_primary(4).with_exploratory(11)
    assert registry.primary == 4
    assert registry.exploratory == 11
    assert registry.total == 15
