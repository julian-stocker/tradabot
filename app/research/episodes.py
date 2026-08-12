"""Grouping correlated observations into opportunity episodes.

The problem
-----------
A scanner that re-evaluates every symbol every hour produces, for one continuous
move, a run of near-identical qualifying observations:

    NVDA qualifies 10:00 · still qualifies 11:00 · strong 12:00 · strong 13:00

That is **one opportunity**, observed four times. Counting it as four independent
pieces of evidence inflates every sample size, narrows every confidence interval,
and makes a single lucky move look like a repeatable edge. It is the most common
way a backtest manufactures significance from nothing.

So statistics are reported twice: once per observation, and once per *episode*.
If an effect survives collapsing each run to a single row, it is at least not an
artefact of double-counting. If it does not survive, the observation-level number
was measuring the scanner's cadence rather than the market.

Determinism
-----------
Episode identity is derived, never stored as state, so it is reproducible from
the observations alone and cannot drift. The rules are deliberately simple and
inspectable rather than clever -- see :data:`MAX_EPISODE_GAP`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

MAX_EPISODE_GAP: Final = timedelta(hours=24)
"""How long a setup may stop qualifying before the next one is a new episode.

Twenty-four hours spans an overnight gap, so a setup that qualifies late on
Monday and again on Tuesday morning is treated as one continuing opportunity --
which is what it is. A longer window would start merging genuinely separate moves
in the same name; a shorter one would split every episode at every close.

The value matters less than its being fixed and declared: it is a rule, not a
parameter to be tuned until the answer improves.
"""


class _Observation(Protocol):
    """Structural view of what episode assignment needs."""

    symbol: str
    direction: int
    timestamp: datetime
    qualified: bool


@dataclass(frozen=True, slots=True)
class EpisodeKey:
    """Identity of one opportunity episode."""

    symbol: str
    direction: int
    index: int
    """Nth episode for this (symbol, direction), in chronological order."""

    def as_str(self) -> str:
        return f"{self.symbol}:{self.direction:+d}:{self.index}"


@dataclass(slots=True)
class Episode:
    """A run of correlated observations treated as one opportunity."""

    key: EpisodeKey
    started_at: datetime
    ended_at: datetime
    observations: int = 0
    qualified_observations: int = 0
    peak_score: float = 0.0
    first_score: float = 0.0
    """Score at the instant the episode began -- the one a human would have acted on."""
    first_return: float | None = None
    """Outcome measured from the episode's *first* observation."""

    @property
    def duration_hours(self) -> float:
        return (self.ended_at - self.started_at).total_seconds() / 3600.0


def assign_episodes(
    observations: Sequence[_Observation],
    *,
    max_gap: timedelta = MAX_EPISODE_GAP,
    qualified_only: bool = True,
) -> list[EpisodeKey]:
    """Assign an episode key to each observation, in input order.

    A new episode starts when any of these is true:

    * it is the first observation for this ``(symbol, direction)``;
    * the previous qualifying observation for that pair was more than ``max_gap``
      ago -- the setup lapsed and re-formed;
    * the direction reversed, which is a different opportunity by definition even
      if the symbol and timing are continuous.

    ``qualified_only`` restricts episodes to observations that actually cleared
    the threshold, which is the question episodes exist to answer. Non-qualifying
    observations still receive a key so callers can group them, but they never
    *start* an episode.

    The input must be sorted by timestamp. Sorting here would hide a caller
    passing unordered rows, and the resulting episodes would be silently wrong.
    """
    keys: list[EpisodeKey] = []
    counters: dict[tuple[str, int], int] = {}
    last_seen: dict[tuple[str, int], datetime] = {}

    for observation in observations:
        pair = (observation.symbol, observation.direction)
        counts = observation.qualified or not qualified_only

        previous = last_seen.get(pair)
        starts_new = previous is None or (observation.timestamp - previous) > max_gap

        if counts and starts_new:
            counters[pair] = counters.get(pair, 0) + 1

        if counts:
            last_seen[pair] = observation.timestamp

        keys.append(EpisodeKey(observation.symbol, observation.direction, counters.get(pair, 0)))

    return keys


def collapse(
    observations: Sequence[_Observation],
    keys: Sequence[EpisodeKey],
    *,
    scores: Sequence[float],
    returns: Sequence[float | None],
) -> list[Episode]:
    """Reduce observations to one :class:`Episode` per key.

    The episode's return is taken from its **first** observation, not its best or
    its average. That is the honest choice: a human acting on the alert acts when
    it fires, not at the point that turned out to be optimal. Using the peak would
    be look-ahead dressed up as aggregation.
    """
    episodes: dict[str, Episode] = {}

    for observation, key, score, value in zip(observations, keys, scores, returns, strict=True):
        if key.index == 0:  # never qualified; not part of any episode
            continue
        identity = key.as_str()
        episode = episodes.get(identity)
        if episode is None:
            episodes[identity] = Episode(
                key=key,
                started_at=observation.timestamp,
                ended_at=observation.timestamp,
                observations=1,
                qualified_observations=1 if observation.qualified else 0,
                peak_score=score,
                first_score=score,
                first_return=value,
            )
            continue

        episode.observations += 1
        episode.qualified_observations += 1 if observation.qualified else 0
        episode.ended_at = max(episode.ended_at, observation.timestamp)
        episode.peak_score = max(episode.peak_score, score)

    return list(episodes.values())
