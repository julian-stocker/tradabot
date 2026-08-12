"""Overview suppression and episode clustering.

Two defences against the same failure: presenting repetition as information. The
Discord one is cosmetic and obvious; the statistical one is neither, and it is
the reason a 6.7-point apparent edge turns out to be roughly 2.3 once correlated
observations are collapsed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.notifications.policy import evaluate_overview
from app.research.episodes import MAX_EPISODE_GAP, EpisodeKey, assign_episodes, collapse
from app.scanner.enums import SessionPhase

OPEN = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)


class Obs:
    """Minimal observation: symbol, direction, time, qualification."""

    def __init__(
        self, symbol: str, direction: int, minutes: int, *, qualified: bool = True
    ) -> None:
        self.symbol = symbol
        self.direction = direction
        self.timestamp = OPEN + timedelta(minutes=minutes)
        self.qualified = qualified


# ---------------------------------------------------------------------------
# 1-2. Market-closed and repeated-silence suppression
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "session", [SessionPhase.CLOSED, SessionPhase.WEEKEND, SessionPhase.HOLIDAY]
)
def test_a_closed_market_sends_no_overview(session: SessionPhase) -> None:
    """**The reported defect.**

    "No qualified opportunities." every hour of every weekend. Nothing has
    changed since the close and nothing can until the open.
    """
    decision = evaluate_overview(candidate_count=0, session=session)

    assert not decision.should_publish
    assert "market" in decision.reason


def test_a_closed_market_stays_silent_even_with_candidates() -> None:
    """Stale candidates from the last session are not news either."""
    decision = evaluate_overview(candidate_count=5, session=SessionPhase.WEEKEND)

    assert not decision.should_publish


def test_zero_opportunities_is_not_news() -> None:
    """It is the overwhelmingly common state: 385 qualified of 116,844."""
    decision = evaluate_overview(candidate_count=0, session=SessionPhase.REGULAR)

    assert not decision.should_publish
    assert decision.reason == "no qualified opportunities"


def test_extended_hours_cannot_qualify_so_it_stays_silent() -> None:
    """The scanner refuses to promote setups on pre/post-market IEX prints, so an
    overview then can only ever say zero. Announcing a foregone conclusion is
    still noise."""
    for session in (SessionPhase.PRE_MARKET, SessionPhase.AFTER_HOURS):
        assert not evaluate_overview(candidate_count=3, session=session).should_publish


def test_extended_hours_can_be_enabled_deliberately() -> None:
    """The policy follows the scanner's own configuration rather than hardcoding."""
    decision = evaluate_overview(
        candidate_count=3, session=SessionPhase.PRE_MARKET, require_regular_session=False
    )

    assert decision.should_publish


def test_an_open_market_with_candidates_publishes() -> None:
    """Suppression must not become silence-always."""
    decision = evaluate_overview(candidate_count=4, session=SessionPhase.REGULAR)

    assert decision.should_publish
    assert "4 qualified" in decision.reason


def test_the_decision_is_truthy_for_convenient_use() -> None:
    assert evaluate_overview(candidate_count=1, session=SessionPhase.REGULAR)
    assert not evaluate_overview(candidate_count=0, session=SessionPhase.REGULAR)


# ---------------------------------------------------------------------------
# 8-10. Episode clustering
# ---------------------------------------------------------------------------
def test_a_continuous_run_is_one_episode() -> None:
    """**The core of the statistical correction.**

    NVDA qualifying at 10:00, 10:15, 10:30 and 10:45 is one opportunity observed
    four times, not four independent pieces of evidence.
    """
    run = [Obs("NVDA", 1, minute) for minute in (0, 15, 30, 45)]

    keys = assign_episodes(run)

    assert len({key.as_str() for key in keys}) == 1
    assert all(key.index == 1 for key in keys)


def test_a_lapse_longer_than_the_gap_starts_a_new_episode() -> None:
    gap_minutes = int(MAX_EPISODE_GAP.total_seconds() // 60)
    run = [Obs("NVDA", 1, 0), Obs("NVDA", 1, gap_minutes + 60)]

    keys = assign_episodes(run)

    assert keys[0].index == 1
    assert keys[1].index == 2


def test_a_lapse_within_the_gap_continues_the_episode() -> None:
    """An overnight break is the same opportunity resuming, not a new one."""
    run = [Obs("NVDA", 1, 0), Obs("NVDA", 1, 60 * 18)]

    keys = assign_episodes(run)

    assert keys[0].as_str() == keys[1].as_str()


def test_a_direction_reversal_is_always_a_new_episode() -> None:
    """A bullish and a bearish setup in the same name are different opportunities
    however close together they occur."""
    run = [Obs("NVDA", 1, 0), Obs("NVDA", -1, 15)]

    keys = assign_episodes(run)

    assert keys[0].as_str() != keys[1].as_str()
    assert keys[0].direction != keys[1].direction


def test_different_symbols_never_share_an_episode() -> None:
    keys = assign_episodes([Obs("NVDA", 1, 0), Obs("AAPL", 1, 0)])

    assert keys[0].as_str() != keys[1].as_str()


def test_non_qualifying_observations_do_not_start_an_episode() -> None:
    """Episodes exist to de-duplicate *opportunities*; a score of 40 is not one."""
    run = [Obs("NVDA", 1, 0, qualified=False), Obs("NVDA", 1, 15, qualified=False)]

    keys = assign_episodes(run, qualified_only=True)

    assert all(key.index == 0 for key in keys)


def test_a_qualifying_observation_after_quiet_ones_opens_the_episode() -> None:
    run = [Obs("NVDA", 1, 0, qualified=False), Obs("NVDA", 1, 15, qualified=True)]

    keys = assign_episodes(run)

    assert keys[0].index == 0
    assert keys[1].index == 1


def test_assignment_is_deterministic() -> None:
    """Episode identity is derived, never stored, so it cannot drift."""
    run = [Obs("NVDA", 1, m) for m in (0, 15, 30)]

    first = [key.as_str() for key in assign_episodes(run)]
    second = [key.as_str() for key in assign_episodes(run)]

    assert first == second


# ---------------------------------------------------------------------------
# 11. Episode-level statistics
# ---------------------------------------------------------------------------
def test_collapsing_reduces_four_observations_to_one_row() -> None:
    run = [Obs("NVDA", 1, m) for m in (0, 15, 30, 45)]
    keys = assign_episodes(run)

    episodes = collapse(run, keys, scores=[76.0, 79.0, 88.0, 87.0], returns=[0.01] * 4)

    assert len(episodes) == 1
    assert episodes[0].observations == 4
    assert episodes[0].peak_score == 88.0


def test_an_episode_is_scored_where_it_fired_not_at_its_peak() -> None:
    """**Using the peak would be look-ahead dressed up as aggregation.**

    A human acts on the alert when it fires, not at the point that turned out to
    be optimal.
    """
    run = [Obs("NVDA", 1, m) for m in (0, 15, 30)]
    keys = assign_episodes(run)

    episodes = collapse(run, keys, scores=[76.0, 90.0, 88.0], returns=[0.001, 0.050, 0.040])

    assert episodes[0].first_score == 76.0
    assert episodes[0].first_return == pytest.approx(0.001)
    assert episodes[0].peak_score == 90.0, "the peak is recorded, just not used as the outcome"


def test_never_qualifying_observations_are_excluded_from_episodes() -> None:
    run = [Obs("NVDA", 1, 0, qualified=False)]
    keys = assign_episodes(run)

    episodes = collapse(run, keys, scores=[40.0], returns=[0.02])

    assert episodes == []


def test_two_separate_moves_produce_two_episodes() -> None:
    gap_minutes = int(MAX_EPISODE_GAP.total_seconds() // 60) + 60
    run = [Obs("NVDA", 1, 0), Obs("NVDA", 1, 15), Obs("NVDA", 1, gap_minutes)]
    keys = assign_episodes(run)

    episodes = collapse(run, keys, scores=[76.0, 77.0, 80.0], returns=[0.01, 0.02, 0.03])

    assert len(episodes) == 2
    assert sorted(episode.observations for episode in episodes) == [1, 2]


def test_the_episode_key_renders_stably() -> None:
    assert EpisodeKey("NVDA", 1, 3).as_str() == "NVDA:+1:3"
    assert EpisodeKey("NVDA", -1, 1).as_str() == "NVDA:-1:1"


# ---------------------------------------------------------------------------
# 9. Closed-market regression through the real scheduled path
# ---------------------------------------------------------------------------
def test_the_scheduled_overview_job_consults_the_policy() -> None:
    """**Regression for the reported defect, at the call site.**

    The hourly launchd job used to publish unconditionally. Asserting the policy
    in isolation would not have caught that -- the rule was correct, the job
    simply never asked. This pins the wiring.
    """
    import inspect

    from app.cli import _scanner_overview

    source = inspect.getsource(_scanner_overview)

    assert "evaluate_overview" in source, "the scheduled job bypasses the policy"
    assert "session_phase" in source, "the job does not consider the session"
    assert source.index("evaluate_overview") < source.index("NotificationService"), (
        "the policy must be consulted before a notification service is built"
    )


def test_the_scheduled_job_returns_success_when_it_stays_silent() -> None:
    """Suppression is a normal outcome, not a failure.

    A non-zero exit would make launchd log an error every hour of every weekend --
    replacing Discord noise with log noise.
    """
    import inspect

    from app.cli import _scanner_overview

    source = inspect.getsource(_scanner_overview)
    suppressed = source[source.index("should_publish") :]

    assert "return 0" in suppressed[: suppressed.index("service =")]


@pytest.mark.parametrize(
    ("session", "candidates"),
    [
        (SessionPhase.WEEKEND, 0),
        (SessionPhase.HOLIDAY, 0),
        (SessionPhase.CLOSED, 0),
        (SessionPhase.PRE_MARKET, 0),
        (SessionPhase.AFTER_HOURS, 0),
        (SessionPhase.REGULAR, 0),
        (SessionPhase.WEEKEND, 5),
        (SessionPhase.CLOSED, 12),
    ],
)
def test_no_repetitive_zero_opportunity_message_is_ever_produced(
    session: SessionPhase, candidates: int
) -> None:
    """Every combination that used to emit "No qualified opportunities."."""
    assert not evaluate_overview(candidate_count=candidates, session=session).should_publish


def test_the_only_case_that_speaks_is_an_open_market_with_candidates() -> None:
    speaks = [
        (session, count)
        for session in SessionPhase
        for count in (0, 3)
        if evaluate_overview(candidate_count=count, session=session).should_publish
    ]

    assert speaks == [(SessionPhase.REGULAR, 3)]
