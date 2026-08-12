"""Which timeframes a replay actually had, and what that makes it.

The problem this exists to prevent
----------------------------------
tradabot's production scanner reads four timeframes: 5m, 15m, 1h, 1d. The stored
history does not go back equally far -- 1h and 1d reach 2020-07-27, 15m reaches
2024-08-01, 5m only 2025-02-03. A replay of 2021 therefore **cannot** be the
production scanner. It is a different, coarser thing wearing the same name.

The analyser already degrades honestly: a timeframe with no bars is recorded as
``INSUFFICIENT`` rather than raising, and :attr:`MultiTimeframeContext.agreement`
excludes unusable timeframes from its denominator. So a coarse replay produces
*valid* observations. What it does not produce is *comparable* ones, and the way
that goes wrong is silent: two runs land in one table, someone groups by score
band, and half the rows were scored with two timeframes and half with four.

So the mode is **derived from the data, never declared by the caller**. A
parameter could be passed wrongly; a measurement of which timeframes actually
have bars covering the window cannot.

What differs, precisely
-----------------------
The score itself is computed by the signal engine from the **primary timeframe
alone** (1h). That is the whole reason a coarse replay is worth running: score
semantics are unchanged, so score bands remain comparable across modes.

What does change, and must never be compared across modes:

* ``qualified`` -- gated on :attr:`DataQuality.is_actionable`, which is ``OK``
  only. Context quality is the **worst** across all four timeframes, so with 5m
  and 15m absent it is ``INSUFFICIENT`` and ``qualified`` is *structurally false*
  for every coarse observation. It is not a rare outcome there; it is impossible.
* ``aligned`` -- requires macro, primary **and confirmation (15m)** to agree.
  Structurally false without 15m.
* ``agreement`` -- renormalised over usable timeframes, so a coarse run computes
  it over two rather than four. Trivially 1.0 whenever both agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.domain.enums import Timeframe
from app.scanner.timeframes import SCANNER_TIMEFRAMES

COARSE_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (Timeframe.H1, Timeframe.D1)
"""The two timeframes with history back to the provider floor."""

MIN_COVERAGE: Final = 0.90
"""Fraction of the window a timeframe must span to count as available.

Not 1.0: a symbol listed mid-window, or a feed gap of a few days, should not
demote an otherwise complete four-timeframe run. Not 0.5 either -- a timeframe
present for only the tail of the window would let a run call itself
production-faithful on the strength of its last few months.
"""


class ReplayMode(StrEnum):
    """What a historical replay actually was."""

    PRODUCTION_FAITHFUL = "PRODUCTION_FAITHFUL"
    """All four scanner timeframes covered the window. Directly comparable to live."""

    COARSE_HISTORICAL = "COARSE_HISTORICAL"
    """Only 1h and 1d. Scores comparable; ``qualified``/``aligned`` are not."""

    MIXED = "MIXED"
    """The window straddles a boundary where a timeframe's history begins.

    Deliberately its own mode rather than being rounded to one of the others.
    Rounding down would discard real 5m/15m context for part of the window;
    rounding up would claim production fidelity for months that never had it.
    A run that lands here should be split at the boundary and re-run.
    """


@dataclass(frozen=True, slots=True)
class ModeResolution:
    """The measured verdict, with the evidence that produced it."""

    mode: ReplayMode
    available: tuple[Timeframe, ...]
    missing: tuple[Timeframe, ...]
    detail: str

    @property
    def comparable_fields(self) -> tuple[str, ...]:
        """Observation fields that mean the same thing in every mode."""
        return ("score", "confidence", "classification", "direction")

    @property
    def incomparable_fields(self) -> tuple[str, ...]:
        """Fields whose meaning depends on which timeframes existed.

        Named explicitly so an analysis that touches one has to decide, rather
        than discovering the problem in a result it already believes.
        """
        if self.mode is ReplayMode.PRODUCTION_FAITHFUL:
            return ()
        return ("qualified", "aligned", "agreement", "data_quality")


def resolve_mode(
    coverage: dict[Timeframe, tuple[datetime | None, datetime | None]],
    *,
    start: datetime,
    end: datetime,
) -> ModeResolution:
    """Classify a window from the history that actually exists.

    Args:
        coverage: earliest and latest stored bar per timeframe, across the
            symbols being replayed. ``(None, None)`` means no bars at all.
        start: replay window start.
        end: replay window end.

    Measured, not asserted: the caller supplies observed coverage and this
    decides. There is no parameter to get wrong.
    """
    span = (end - start).total_seconds()
    available: list[Timeframe] = []
    missing: list[Timeframe] = []
    partial: list[Timeframe] = []

    for timeframe in SCANNER_TIMEFRAMES:
        earliest, latest = coverage.get(timeframe, (None, None))
        if earliest is None or latest is None:
            missing.append(timeframe)
            continue
        covered = (min(latest, end) - max(earliest, start)).total_seconds()
        fraction = max(0.0, covered / span) if span > 0 else 0.0
        if fraction >= MIN_COVERAGE:
            available.append(timeframe)
        elif fraction <= 0.0:
            # No overlap at all: the timeframe simply did not exist here.
            missing.append(timeframe)
        else:
            # **Present for part of the window.** This is the dangerous case and
            # it gets its own bucket: calling it "missing" would label the run
            # COARSE_HISTORICAL and claim these bars contributed nothing, when in
            # fact some months were scored with them and some without -- inside a
            # single run, invisibly. That is precisely the mixing this module
            # exists to make impossible.
            partial.append(timeframe)

    if partial:
        return ModeResolution(
            mode=ReplayMode.MIXED,
            available=tuple(available),
            missing=tuple(missing + partial),
            detail=(
                f"{', '.join(t.value for t in partial)} covers only part of the window; "
                "split at the boundary where that history begins and re-run each side"
            ),
        )

    if not missing:
        return ModeResolution(
            mode=ReplayMode.PRODUCTION_FAITHFUL,
            available=tuple(available),
            missing=(),
            detail="all four scanner timeframes cover the window",
        )

    if set(available) == set(COARSE_TIMEFRAMES):
        return ModeResolution(
            mode=ReplayMode.COARSE_HISTORICAL,
            available=tuple(available),
            missing=tuple(missing),
            detail=(
                "1h and 1d only; qualified/aligned are structurally false and "
                "must not be compared with a production-faithful run"
            ),
        )

    return ModeResolution(
        mode=ReplayMode.MIXED,
        available=tuple(available),
        missing=tuple(missing),
        detail=(
            f"partial coverage ({', '.join(t.value for t in missing)} incomplete); "
            "split the window at the boundary and re-run each side"
        ),
    )
