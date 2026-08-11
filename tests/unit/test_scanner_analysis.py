"""Structure metrics, multi-timeframe agreement, lifecycle and ranking.

Pure functions, so no database and no network. The price series are hand-built
zigzags with known swing points, because a test whose expected answer is computed
by the code under test asserts only self-consistency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import NotificationSettings, ScannerSettings
from app.domain.enums import Timeframe
from app.scanner.enums import DataQuality, SignalLifecycle, StructureState, TrendState
from app.scanner.lifecycle import (
    SETUP_UNKNOWN,
    SignalIdentity,
    direction_label,
    evaluate_lifecycle,
    has_expired,
    setup_for,
)
from app.scanner.ranking import RankedCandidate, rank_candidates, rank_score
from app.scanner.structure import (
    MIN_BARS_FOR_STRUCTURE,
    analyse_structure,
    swing_points,
)
from app.scanner.timeframes import (
    TIMEFRAME_ROLES,
    MultiTimeframeContext,
    TimeframeAssessment,
    classify_trend,
)
from app.scanner.universe import INITIAL_UNIVERSE, SECTORS, by_sector, universe_symbols

T0 = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


def zigzag(*, legs: int = 6, step: float = 3.0, amplitude: float = 8.0, rising: bool = True):
    """A series with unambiguous local maxima and minima.

    Each leg goes trough → peak → trough, so both swing highs and swing lows are
    genuine local extremes rather than artefacts of a monotonic ramp.

    ``amplitude`` deliberately exceeds ``step``. With a smaller amplitude a
    *descending* zigzag has no local maxima at all -- each leg's peak sits below
    the previous leg's trough -- so the fixture would silently test nothing.
    """
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for leg in range(legs):
        base = 100.0 + (leg * step if rising else -leg * step)
        for value in (base, base + amplitude, base, base - amplitude / 2, base):
            closes.append(value)
            highs.append(value + 0.5)
            lows.append(value - 0.5)
    return highs, lows, closes


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_swing_points_finds_local_extremes() -> None:
    highs, lows, _ = zigzag()

    swings = swing_points(highs, lows)

    assert swings.highs, "a zigzag has swing highs"
    assert swings.lows, "a zigzag has swing lows"


def test_swing_points_ignores_the_unconfirmed_tail() -> None:
    """A swing needs bars *after* it to be confirmed.

    Reporting the final bar as a swing high would be look-ahead: it might still
    be exceeded by the next bar.
    """
    highs, lows, _ = zigzag()

    swings = swing_points(highs, lows, window=2)

    assert all(index < len(highs) - 2 for index, _ in swings.highs)


def test_mismatched_series_lengths_are_refused() -> None:
    with pytest.raises(ValueError, match="same length"):
        swing_points([1.0, 2.0, 3.0], [1.0])


def test_higher_highs_and_higher_lows_in_an_uptrend() -> None:
    highs, lows, closes = zigzag(rising=True)

    metrics = analyse_structure(highs=highs, lows=lows, closes=closes)

    assert metrics.higher_highs is True
    assert metrics.higher_lows is True
    assert metrics.is_uptrend_structure


def test_lower_highs_and_lower_lows_in_a_downtrend() -> None:
    highs, lows, closes = zigzag(rising=False)

    metrics = analyse_structure(highs=highs, lows=lows, closes=closes)

    assert metrics.lower_highs is True
    assert metrics.lower_lows is True
    assert metrics.is_downtrend_structure


def test_a_breakout_is_a_close_at_the_range_high() -> None:
    """The breakout bar closes at its high, as a real one does.

    A synthetic bar whose high always sits above its close can never break out by
    definition, which makes for a fixture that tests the arithmetic and not the
    rule.
    """
    closes = [100.0] * 25 + [115.0]
    highs = [c + 0.5 for c in closes[:-1]] + [115.0]
    lows = [c - 0.5 for c in closes]

    assert analyse_structure(highs=highs, lows=lows, closes=closes).state is StructureState.BREAKOUT


def test_a_breakdown_is_a_close_at_the_range_low() -> None:
    """Mirror of the breakout: the bar closes at its low."""
    closes = [100.0] * 25 + [85.0]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes[:-1]] + [85.0]

    assert (
        analyse_structure(highs=highs, lows=lows, closes=closes).state is StructureState.BREAKDOWN
    )


def test_a_contracting_range_is_consolidation() -> None:
    """Narrow is not consolidating; *contracting* is. The change is the observation."""
    # 20 wide bars then 10 tight ones, so the comparison window (half the
    # lookback either side) actually spans the contraction.
    wide = [100.0 + (5.0 if i % 2 else -5.0) for i in range(20)]
    tight = [100.0 + (0.4 if i % 2 else -0.4) for i in range(10)]
    closes = wide + tight
    highs = [c + 0.2 for c in closes]
    lows = [c - 0.2 for c in closes]

    metrics = analyse_structure(highs=highs, lows=lows, closes=closes, lookback=20)

    assert metrics.state is StructureState.CONSOLIDATION


def test_too_little_history_yields_unknown_not_a_guess() -> None:
    short = [100.0] * (MIN_BARS_FOR_STRUCTURE - 1)

    metrics = analyse_structure(highs=short, lows=short, closes=short)

    assert metrics.state is StructureState.UNKNOWN
    assert metrics.higher_highs is None, "unknown, not False"
    assert metrics.support is None


def test_distance_to_high_and_range_position_are_reported() -> None:
    closes = [90.0 + i for i in range(25)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]

    metrics = analyse_structure(highs=highs, lows=lows, closes=closes)

    assert metrics.distance_to_high_pct is not None
    assert metrics.range_position is not None
    assert 0.0 <= metrics.range_position <= 1.0


# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------
def assessment(
    timeframe: Timeframe, trend: TrendState, quality: DataQuality = DataQuality.OK
) -> TimeframeAssessment:
    return TimeframeAssessment(
        timeframe=timeframe,
        role=TIMEFRAME_ROLES[timeframe],
        quality=quality,
        trend=trend,
    )


def context(**states: TrendState) -> MultiTimeframeContext:
    mapping = {
        Timeframe.D1: states.get("d1", TrendState.UNKNOWN),
        Timeframe.H1: states.get("h1", TrendState.UNKNOWN),
        Timeframe.M15: states.get("m15", TrendState.UNKNOWN),
        Timeframe.M5: states.get("m5", TrendState.UNKNOWN),
    }
    return MultiTimeframeContext(
        symbol="NVDA",
        assessments={tf: assessment(tf, trend) for tf, trend in mapping.items()},
    )


def test_full_alignment_scores_perfect_agreement() -> None:
    aligned = context(d1=TrendState.UP, h1=TrendState.UP, m15=TrendState.UP, m5=TrendState.UP)

    assert aligned.direction == 1
    assert aligned.agreement == pytest.approx(1.0)
    assert aligned.aligned


def test_a_bounce_against_the_macro_trend_is_not_bullish() -> None:
    """The distinction the whole module exists for.

    Two of four timeframes bullish, but the daily trend is down. That is a bounce
    in a downtrend -- the most common way a short-timeframe signal loses money --
    and reporting it as a bullish setup is exactly the mistake being prevented.
    """
    bounce = context(
        d1=TrendState.DOWN, h1=TrendState.SIDEWAYS, m15=TrendState.UP, m5=TrendState.UP
    )

    assert bounce.direction == 0, "the macro timeframe is not outvoted"
    assert not bounce.aligned


def test_unknown_timeframes_are_excluded_from_the_denominator() -> None:
    """Missing data must not count as a neutral vote.

    Counting it would let an instrument with less history look more 'agreed'
    than one with more.
    """
    partial = context(
        d1=TrendState.UP, h1=TrendState.UP, m15=TrendState.UNKNOWN, m5=TrendState.DOWN
    )

    assert partial.agreement == pytest.approx(2 / 3)


def test_agreement_is_zero_when_nothing_is_usable() -> None:
    assert context().agreement == 0.0
    assert context().direction == 0


def test_a_three_way_split_is_not_agreement() -> None:
    split = context(d1=TrendState.UP, h1=TrendState.DOWN, m15=TrendState.SIDEWAYS)

    assert split.direction == 0
    assert split.agreement == 0.0


def test_context_quality_is_the_worst_timeframe() -> None:
    """A stale 1h read is not rescued by a fresh 5m one."""
    mixed = MultiTimeframeContext(
        symbol="NVDA",
        assessments={
            Timeframe.D1: assessment(Timeframe.D1, TrendState.UP),
            Timeframe.H1: assessment(Timeframe.H1, TrendState.UP, DataQuality.STALE),
            Timeframe.M5: assessment(Timeframe.M5, TrendState.UP),
        },
    )

    assert mixed.quality is DataQuality.STALE


def test_every_timeframe_state_is_persistable() -> None:
    """A future model must inspect the context, not just the collapsed score."""
    payload = context(d1=TrendState.UP, h1=TrendState.UP).as_dict()

    assert set(payload["timeframes"]) == {"1d", "1h", "15m", "5m"}
    assert payload["timeframes"]["1d"]["role"] == "macro"
    assert payload["timeframes"]["1h"]["role"] == "primary"


@pytest.mark.parametrize(
    ("spread", "expected"),
    [
        (3.0, TrendState.UP),
        (-3.0, TrendState.DOWN),
        (0.0, TrendState.SIDEWAYS),
        (None, TrendState.UNKNOWN),
    ],
)
def test_trend_classification_follows_ema_separation(
    spread: float | None, expected: TrendState
) -> None:
    assert classify_trend(ema_spread_pct=spread, structure=None, quality=DataQuality.OK) is expected


def test_unusable_data_yields_an_unknown_trend() -> None:
    assert (
        classify_trend(ema_spread_pct=5.0, structure=None, quality=DataQuality.STALE)
        is TrendState.UNKNOWN
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@pytest.fixture
def thresholds() -> NotificationSettings:
    return NotificationSettings()


def test_the_documented_lifecycle_progression(thresholds: NotificationSettings) -> None:
    expected = [
        (60.0, SignalLifecycle.DISCOVERED),
        (78.0, SignalLifecycle.QUALIFIED),
        (88.0, SignalLifecycle.STRONG),
        (80.0, SignalLifecycle.WEAKENED),
        (88.0, SignalLifecycle.STRONG),
        (70.0, SignalLifecycle.INVALIDATED),
    ]

    current = None
    for score, lifecycle in expected:
        transition = evaluate_lifecycle(current=current, score=score, settings=thresholds)
        assert transition.lifecycle is lifecycle, f"at {score}: {transition.reason}"
        current = transition.lifecycle


def test_a_terminal_signal_is_not_resurrected(thresholds: NotificationSettings) -> None:
    """A recovering score starts a new signal, keeping the invalidation on record."""
    transition = evaluate_lifecycle(
        current=SignalLifecycle.INVALIDATED, score=95.0, settings=thresholds
    )

    assert transition.lifecycle is SignalLifecycle.INVALIDATED
    assert not transition.changed


def test_unusable_data_cannot_promote_a_signal(thresholds: NotificationSettings) -> None:
    """A setup that only looks qualified on stale data is not qualified."""
    transition = evaluate_lifecycle(
        current=SignalLifecycle.DISCOVERED, score=95.0, settings=thresholds, actionable=False
    )

    assert transition.lifecycle is SignalLifecycle.DISCOVERED


def test_unusable_data_can_still_invalidate(thresholds: NotificationSettings) -> None:
    """A setup breaking on bad data has still broken; only promotion is blocked."""
    transition = evaluate_lifecycle(
        current=SignalLifecycle.QUALIFIED, score=10.0, settings=thresholds, actionable=False
    )

    assert transition.lifecycle is SignalLifecycle.INVALIDATED


def test_a_signal_below_threshold_stays_discovered(thresholds: NotificationSettings) -> None:
    transition = evaluate_lifecycle(
        current=SignalLifecycle.DISCOVERED, score=40.0, settings=thresholds
    )

    assert transition.lifecycle is SignalLifecycle.DISCOVERED
    assert not transition.changed, "no transition means nothing to announce"


def test_expiry_is_measured_from_the_last_evaluation() -> None:
    settings = ScannerSettings(signal_expiry_hours=48)

    assert not has_expired(last_evaluated_at=T0, now=T0 + timedelta(hours=47), settings=settings)
    assert has_expired(last_evaluated_at=T0, now=T0 + timedelta(hours=49), settings=settings)


def test_identity_requires_every_field_to_match() -> None:
    base = SignalIdentity(1, "LONG", "1h", "5d", "BREAKOUT")

    assert base.matches(SignalIdentity(1, "LONG", "1h", "5d", "BREAKOUT"))
    assert not base.matches(SignalIdentity(1, "SHORT", "1h", "5d", "BREAKOUT"))
    assert not base.matches(SignalIdentity(1, "LONG", "1d", "5d", "BREAKOUT"))
    assert not base.matches(SignalIdentity(1, "LONG", "1h", "5d", "BREAKDOWN"))


def test_only_falsifiable_structures_become_a_setup() -> None:
    """RANGING is not a premise that could break, so it is not a setup identity."""
    assert setup_for(StructureState.BREAKOUT) == "BREAKOUT"
    assert setup_for(StructureState.RANGING) == SETUP_UNKNOWN
    assert setup_for(StructureState.UNKNOWN) == SETUP_UNKNOWN


def test_direction_labels() -> None:
    assert direction_label(1) == "LONG"
    assert direction_label(-1) == "SHORT"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def candidate(symbol: str, **overrides: float | None) -> RankedCandidate:
    values: dict[str, float | None] = {
        "score": 80.0,
        "confidence": 0.7,
        "agreement": 0.75,
        "net_edge_bps": 60.0,
        "spread_bps": 10.0,
        "relative_volume": 1.5,
    }
    values.update(overrides)
    value, contributions = rank_score(**values)  # type: ignore[arg-type]
    return RankedCandidate(
        symbol=symbol,
        evaluation_id=None,
        tracked_signal_id=None,
        rank_score=value,
        contributions=contributions,
        **values,  # type: ignore[arg-type]
    )


def test_a_higher_score_ranks_higher() -> None:
    ordered = rank_candidates([candidate("LOW", score=60.0), candidate("HIGH", score=95.0)])

    assert [c.symbol for c in ordered] == ["HIGH", "LOW"]


def test_agreement_breaks_a_score_tie() -> None:
    ordered = rank_candidates([candidate("AAA"), candidate("BBB", agreement=1.0)])

    assert ordered[0].symbol == "BBB"


def test_ranking_is_totally_deterministic() -> None:
    """Identical inputs must produce an identical order, or a change means nothing."""
    identical = [candidate("CCC"), candidate("AAA"), candidate("BBB")]

    first = [c.symbol for c in rank_candidates(list(identical))]
    second = [c.symbol for c in rank_candidates(list(reversed(identical)))]

    assert first == second == ["AAA", "BBB", "CCC"]


def test_an_unmeasured_spread_contributes_zero_not_an_average() -> None:
    """An unmeasured cost is not a low cost."""
    assert candidate("X", spread_bps=None).contributions["liquidity"] == 0.0


def test_the_ranking_limit_is_respected() -> None:
    many = [candidate(f"S{i:02d}", score=50.0 + i) for i in range(12)]

    assert len(rank_candidates(many, limit=5)) == 5


def test_fewer_candidates_than_the_limit_returns_what_exists() -> None:
    """If only two qualify, show two. Never manufacture activity."""
    assert len(rank_candidates([candidate("A"), candidate("B")], limit=5)) == 2


def test_no_candidates_ranks_to_an_empty_list() -> None:
    assert rank_candidates([], limit=5) == []


def test_a_candidate_explains_its_own_rank() -> None:
    explanation = candidate("NVDA").explain()

    assert "NVDA" in explanation
    assert "score" in explanation


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def test_the_universe_spans_multiple_sectors() -> None:
    """A technology-only watchlist is one bet with fifty tickers on it."""
    grouped = by_sector()

    assert len(grouped) == len(SECTORS)
    assert all(symbols for symbols in grouped.values()), "every sector has members"


def test_the_universe_is_roughly_fifty_liquid_names() -> None:
    minimum, maximum = 45, 60
    assert minimum <= len(INITIAL_UNIVERSE) <= maximum


def test_universe_symbols_are_unique() -> None:
    symbols = universe_symbols()

    assert len(symbols) == len(set(symbols))
