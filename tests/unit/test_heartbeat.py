"""Liveness can only be inferred from outside, and only from silence.

These tests cover the *definitions* — what UP, LATE, DOWN and RECOVERED mean,
and which transitions are worth alerting on. They do not, and cannot, prove that
the off-host watcher is deployed: that is an operational fact about GitHub
Actions and an external heartbeat endpoint, and asserting it here would be
exactly the local substitute the design forbids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ops.heartbeat import (
    GRACE_SECONDS,
    INTERVAL_SECONDS,
    Liveness,
    declared_policy,
    emit,
    evaluate,
    should_alert,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def _ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


class TestLivenessStates:
    def test_a_recent_heartbeat_is_up(self) -> None:
        assert evaluate(_ago(60), NOW, previous=Liveness.UP) is Liveness.UP

    def test_a_missed_beat_inside_the_grace_period_is_late_not_down(self) -> None:
        """One missed heartbeat is a network blip, not a dead machine."""
        state = evaluate(_ago(INTERVAL_SECONDS + 60), NOW, previous=Liveness.UP)
        assert state is Liveness.LATE
        assert not should_alert(state, Liveness.UP)

    def test_silence_beyond_the_grace_period_is_down(self) -> None:
        """**The gate.** This is the state the whole design exists to detect."""
        state = evaluate(_ago(GRACE_SECONDS + 60), NOW, previous=Liveness.UP)
        assert state is Liveness.DOWN
        assert should_alert(state, Liveness.UP)

    def test_a_heartbeat_after_a_down_is_recovered(self) -> None:
        state = evaluate(_ago(10), NOW, previous=Liveness.DOWN)
        assert state is Liveness.RECOVERED
        assert should_alert(state, Liveness.DOWN)

    def test_never_having_seen_a_heartbeat_is_unknown_not_down(self) -> None:
        """A watchdog configured a minute ago has learned nothing yet."""
        assert evaluate(None, NOW) is Liveness.UNKNOWN

    def test_a_repeated_down_is_suppressed(self) -> None:
        """**The gate.** A host down all day must not alert every fifteen minutes."""
        state = evaluate(_ago(GRACE_SECONDS * 20), NOW, previous=Liveness.DOWN)
        assert state is Liveness.DOWN
        assert not should_alert(state, Liveness.DOWN)

    def test_the_grace_period_is_three_missed_beats(self) -> None:
        assert declared_policy()["missed_beats_before_down"] == 3


class TestEmitter:
    def test_an_unconfigured_heartbeat_is_a_state_not_a_crash(self) -> None:
        result = emit(None, now=NOW)
        assert not result.sent
        assert not result.configured

    def test_a_failing_endpoint_never_raises(self) -> None:
        """**The gate.** A heartbeat that could throw would kill the process it
        exists to prove is healthy."""

        def explode(_request: object, **_kwargs: object) -> object:
            msg = "network is down"
            raise OSError(msg)

        result = emit("https://watchdog.example/ping", now=NOW, opener=explode)
        assert not result.sent
        assert result.attempts > 1
        assert result.error is not None

    def test_a_successful_ping_reports_sent(self) -> None:
        class _Response:
            def __enter__(self) -> object:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        result = emit(
            "https://watchdog.example/ping",
            now=NOW,
            opener=lambda _request, **_kwargs: _Response(),
        )
        assert result.sent
        assert result.attempts == 1

    def test_no_ping_url_appears_in_the_result(self) -> None:
        """A ping URL is a bearer credential."""

        def explode(_request: object, **_kwargs: object) -> object:
            msg = "https://watchdog.example/secret-token failed"
            raise OSError(msg)

        result = emit("https://watchdog.example/secret-token", now=NOW, opener=explode)
        assert "secret-token" not in str(result.error or "")


class TestWatcherIsOffHost:
    def test_the_workflow_exists_and_runs_on_a_schedule(self) -> None:
        from pathlib import Path

        workflow = Path(".github/workflows/watchdog.yml")
        assert workflow.exists()
        text = workflow.read_text()
        assert "schedule:" in text
        assert "runs-on: ubuntu-latest" in text

    def test_the_workflow_does_not_depend_on_the_tradabot_host(self) -> None:
        """**The gate.** An alert that needs the dead machine is not an alert."""
        from pathlib import Path

        text = Path(".github/workflows/watchdog.yml").read_text()
        for forbidden in ("tradabot ops", "app.cli", "ssh ", "localhost", "127.0.0.1"):
            assert forbidden not in text, f"watchdog reaches back to the host via {forbidden}"

    def test_the_workflow_holds_no_secret_literal(self) -> None:
        from pathlib import Path

        text = Path(".github/workflows/watchdog.yml").read_text()
        assert "https://discord.com/api/webhooks" not in text
        assert "secrets.DISCORD_STATUS_WEBHOOK" in text

    @pytest.mark.parametrize("threshold", ["900", "300"])
    def test_the_workflow_declares_the_same_thresholds(self, threshold: str) -> None:
        """Two runtimes, one policy; a drift here would be silent."""
        from pathlib import Path

        assert threshold in Path(".github/workflows/watchdog.yml").read_text()
        assert threshold in {str(GRACE_SECONDS), str(INTERVAL_SECONDS)}
