"""Notification policy: thresholds, cooldowns and health transitions.

Pure functions, so these are fast and hermetic. The lifecycle test at the top is
the specification: it is the sequence from the phase brief, asserted step by step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import NotificationSettings
from app.core.events import EventType
from app.notifications.policy import (
    HealthState,
    SignalPhase,
    SignalState,
    evaluate_health,
    evaluate_signal,
)

T0 = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> NotificationSettings:
    return NotificationSettings()


def test_the_documented_signal_lifecycle(settings: NotificationSettings) -> None:
    """The exact sequence the phase brief specifies.

    64 → silent, 76 → qualified, 77 → silent, 86 → strengthened, 85 → silent,
    61 → invalidated. Each step feeds the previous step's state forward, so this
    tests the state machine rather than six independent calls.
    """
    expected = [
        (64.0, None),
        (76.0, EventType.MARKET_SIGNAL_QUALIFIED),
        (77.0, None),
        (86.0, EventType.MARKET_SIGNAL_STRENGTHENED),
        (85.0, None),
        (61.0, EventType.MARKET_SIGNAL_INVALIDATED),
    ]

    state = SignalState()
    now = T0
    for score, event_type in expected:
        decision = evaluate_signal(settings=settings, state=state, score=score, now=now)
        assert decision.event_type == event_type, f"score {score}: {decision.reason}"
        assert decision.notify is (event_type is not None)
        state = decision.next_state
        now += timedelta(minutes=15)


def test_a_score_below_the_threshold_is_silent(settings: NotificationSettings) -> None:
    decision = evaluate_signal(settings=settings, state=SignalState(), score=50.0, now=T0)

    assert not decision.notify
    assert decision.next_state.phase is SignalPhase.NONE


def test_crossing_the_threshold_qualifies(settings: NotificationSettings) -> None:
    decision = evaluate_signal(settings=settings, state=SignalState(), score=80.0, now=T0)

    assert decision.event_type is EventType.MARKET_SIGNAL_QUALIFIED
    assert decision.next_state.phase is SignalPhase.QUALIFIED
    assert decision.next_state.notified_at == T0


def test_a_score_straight_into_the_strong_range_qualifies_not_strengthens(
    settings: NotificationSettings,
) -> None:
    """Nothing was announced before, so there is nothing to have strengthened *from*."""
    decision = evaluate_signal(settings=settings, state=SignalState(), score=90.0, now=T0)

    assert decision.event_type is EventType.MARKET_SIGNAL_QUALIFIED
    assert decision.next_state.phase is SignalPhase.STRONG


def test_a_small_move_within_a_phase_is_suppressed(settings: NotificationSettings) -> None:
    """The deduplication that stops a scanner filling the channel."""
    state = SignalState(SignalPhase.QUALIFIED, 76.0, T0)

    decision = evaluate_signal(
        settings=settings, state=state, score=78.0, now=T0 + timedelta(hours=2)
    )

    assert not decision.notify
    assert "moved" in decision.reason


def test_a_large_move_inside_the_cooldown_is_still_suppressed(
    settings: NotificationSettings,
) -> None:
    """Both gates apply: either one alone still produces near-identical messages."""
    state = SignalState(SignalPhase.QUALIFIED, 76.0, T0)

    decision = evaluate_signal(
        settings=settings, state=state, score=83.0, now=T0 + timedelta(minutes=10)
    )

    assert not decision.notify
    assert "cooldown" in decision.reason


def test_a_large_move_after_the_cooldown_notifies(settings: NotificationSettings) -> None:
    state = SignalState(SignalPhase.QUALIFIED, 76.0, T0)

    decision = evaluate_signal(
        settings=settings, state=state, score=83.0, now=T0 + timedelta(hours=2)
    )

    assert decision.notify
    assert decision.event_type is EventType.MARKET_SIGNAL_STRENGTHENED


def test_the_cooldown_does_not_delay_an_invalidation(
    settings: NotificationSettings,
) -> None:
    """A signal collapsing minutes after it qualified is exactly what to interrupt for.

    Silencing it to respect a timer would be the wrong trade: the reader may have
    acted on the original message.
    """
    state = SignalState(SignalPhase.STRONG, 90.0, T0)

    decision = evaluate_signal(
        settings=settings, state=state, score=40.0, now=T0 + timedelta(minutes=5)
    )

    assert decision.notify
    assert decision.event_type is EventType.MARKET_SIGNAL_INVALIDATED


def test_a_zero_cooldown_disables_the_timer(settings: NotificationSettings) -> None:
    relaxed = NotificationSettings(signal_cooldown_minutes=0)
    state = SignalState(SignalPhase.QUALIFIED, 76.0, T0)

    decision = evaluate_signal(
        settings=relaxed, state=state, score=83.0, now=T0 + timedelta(seconds=1)
    )

    assert decision.notify


def test_thresholds_must_be_ordered() -> None:
    """A 'strong' threshold below the notification one would be incoherent."""
    with pytest.raises(ValueError, match="strong_signal_threshold"):
        NotificationSettings(signal_threshold=80.0, strong_signal_threshold=70.0)


def test_state_advances_even_when_suppressed(settings: NotificationSettings) -> None:
    """The score is remembered regardless, or 'minimum change' measures the wrong gap."""
    decision = evaluate_signal(settings=settings, state=SignalState(), score=50.0, now=T0)

    assert decision.next_state.score == 50.0


# ---------------------------------------------------------------------------
# Health transitions
# ---------------------------------------------------------------------------
def test_a_healthy_component_staying_healthy_is_silent() -> None:
    decision = evaluate_health(state=HealthState(healthy=True, since=T0), healthy=True, now=T0)

    assert not decision.notify


def test_going_unhealthy_notifies() -> None:
    decision = evaluate_health(state=HealthState(healthy=True, since=T0), healthy=False, now=T0)

    assert decision.notify
    assert not decision.recovered
    assert not decision.next_state.healthy


def test_staying_unhealthy_does_not_notify_again() -> None:
    """The alert that matters is the transition; repeating it buries the next one."""
    decision = evaluate_health(
        state=HealthState(healthy=False, since=T0), healthy=False, now=T0 + timedelta(hours=3)
    )

    assert not decision.notify


def test_recovery_notifies_with_the_measured_downtime() -> None:
    decision = evaluate_health(
        state=HealthState(healthy=False, since=T0), healthy=True, now=T0 + timedelta(minutes=30)
    )

    assert decision.notify
    assert decision.recovered
    assert decision.downtime_seconds == pytest.approx(1800)


def test_recovery_without_a_known_start_reports_no_downtime() -> None:
    """An unmeasurable duration is omitted, not guessed at."""
    decision = evaluate_health(state=HealthState(healthy=False), healthy=True, now=T0)

    assert decision.notify
    assert decision.downtime_seconds is None
