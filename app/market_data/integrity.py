"""Price-series integrity: does every discontinuity have an explanation?

The question this answers
------------------------
A stored price series can disagree with the stored corporate actions in two
directions, and both are silent:

* **A split with no action.** The series drops 50% overnight, nothing in
  ``corporate_actions`` says why, and every downstream consumer reads it as a
  market move. Phase 9A fixed the *adjustment* but not the *coverage*: QQQ and
  SMH were registered after the one-shot action sync, so SMH sat for a whole
  phase carrying an unadjusted 2-for-1 -- a -49% bar in the semiconductor sector
  reference.
* **An action with no split.** HON is reported as a 1-for-2 reverse split whose
  observed price ratio is 1.02. Adjusting for it would *create* a +100% jump.

Counting stored actions cannot detect either. ADBE, AMD, BA and BRK.B genuinely
have zero actions, so "zero" is not a signal; and a stored action is not evidence
its event happened. Only the prices can arbitrate, which is what this module
asks them to do.

Why a scan rather than a constraint
-----------------------------------
The check is deliberately a *report*, not an exception thrown during ingest. A
newly listed instrument, a provider revision and a genuine limit-down day all
produce discontinuities, and a hard failure at write time would either block
legitimate data or train an operator to bypass it. Surfacing a classified list
that a human or a CI job reads is the honest shape for a heuristic.

What separates a split from a crash
-----------------------------------
Nothing, with certainty, from prices alone -- so this module does not pretend
otherwise. It classifies by magnitude and by whether a stored action explains
the bar, and it says ``UNEXPLAINED`` rather than ``MISSING_SPLIT``: the finding
is "this needs a reason", not "this is definitely a split". A 30% earnings gap
is reported as a market gap and left alone, because suppressing real moves is
the failure mode that would quietly destroy research.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.corporate_actions.models import CorporateAction
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Candle, Instrument
from app.domain.enums import CorporateActionType, Timeframe

logger = get_logger(__name__)

SPLIT_LIKE_RATIO: Final = 1.70
"""Bar-to-bar ratio beyond which a move is *shaped* like a share-count change.

Calibrated against the measured record, after a first attempt at 1.45 flagged
Netflix's real 2022-04-20 crash (-35.2%, ratio 1.543) as an unexplained split.
In this database the largest genuine single-bar move is that 1.543 and the
smallest real split is SMH's 2-for-1 at 1.958, so the boundary sits in the gap
between them.

**The blind spot this leaves is deliberate and worth naming.** A 3-for-2 split
lands at 1.50, inside the range where real crashes live, and no rule reading
prices alone can separate the two -- a stock that falls 33% and a stock that
splits 3-for-2 produce the same series. Rather than guess, this classifier
reports such bars as ``MARKET_GAP`` and leaves them. The primary defence against
a missing split is fetching corporate actions for every instrument; this scan is
the backstop for the large events, not a replacement for coverage.
"""

MARKET_GAP_RATIO: Final = 1.18
"""Below this, a move is unremarkable and not worth reporting at all.

Between this and :data:`SPLIT_LIKE_RATIO` sits the honest grey zone: large,
real, and reported as ``MARKET_GAP`` so a reader can see the scan considered it.
"""

MAX_CORROBORATION_GAP: Final = timedelta(days=10)
"""How far apart the bars bracketing a split may be and still arbitrate it.

Identical to ``app.research.adjustments.MAX_CORROBORATION_GAP`` and pinned by
test. Beyond this the ratio between two bars measures price drift rather than a
share-count change.
"""

CORROBORATION_BAND: Final = math.log(1.35)
"""How far an observed jump may sit from a declared ratio, in log space.

Identical to ``app.research.adjustments.CORROBORATION_BAND`` and pinned to it by
test. TSLA's 2020 5-for-1 shows an observed 4.44 because the stock moved 12%
that session, so the band has to be generous; what does the real work is
requiring the declared ratio to explain the jump *better than no split does*.
"""


class DiscontinuityKind(StrEnum):
    """Why a bar-to-bar jump exists, as far as the data can say."""

    EXPLAINED = "EXPLAINED"
    """A stored split accounts for it. Nothing to do."""

    UNEXPLAINED = "UNEXPLAINED"
    """Split-shaped, with no stored action. **Needs a reason before it is trusted.**"""

    CONTRADICTED = "CONTRADICTED"
    """A stored action claims a ratio the prices do not show. Adjusting would
    invent a jump; the adjustment layer already skips these."""

    MARKET_GAP = "MARKET_GAP"
    """Large but not split-shaped. Reported so the scan is auditable, and
    deliberately left alone -- suppressing real moves is the worse failure."""


@dataclass(frozen=True, slots=True)
class Discontinuity:
    """One bar-to-bar jump and its classification."""

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    previous_close: float
    close: float
    observed_ratio: float
    kind: DiscontinuityKind
    declared_ratio: float | None = None

    @property
    def move_pct(self) -> float:
        return (self.close / self.previous_close - 1.0) * 100.0

    def describe(self) -> str:
        declared = f" declared x{self.declared_ratio:g}" if self.declared_ratio else ""
        return (
            f"{self.symbol:<7}{self.timeframe.value:<5}{self.timestamp:%Y-%m-%d}  "
            f"{self.previous_close:>10.2f} -> {self.close:<10.2f}"
            f"{self.move_pct:>8.1f}%  x{self.observed_ratio:.3f}{declared}  {self.kind.value}"
        )


def _splits(actions: Sequence[CorporateAction]) -> list[CorporateAction]:
    return [a for a in actions if a.action_type is CorporateActionType.SPLIT]


def _explains(observed: float, declared: float) -> bool:
    """Whether ``declared`` accounts for ``observed`` better than "no split" does.

    Scale-free, and the same rule ``app.research.adjustments`` applies. A plain
    percentage tolerance would have to be loose enough for TSLA's 5-for-1 on a
    12%-move day, which is loose enough to admit HON's phantom 1-for-2.
    """
    if observed <= 0 or declared <= 0:
        return False
    residual = abs(math.log(observed / declared))
    return residual < CORROBORATION_BAND and residual < abs(math.log(observed))


def classify_series(
    symbol: str,
    timeframe: Timeframe,
    timestamps: Sequence[datetime],
    closes: Sequence[float],
    actions: Sequence[CorporateAction],
) -> list[Discontinuity]:
    """Classify every notable jump in one instrument's series.

    Pure: no database, no I/O. ``timestamps`` and ``closes`` must be ascending
    and the same length.

    The ratio convention is ``previous / current`` so that a 4-for-1 split reads
    as 4.0 -- matching how ``to_shares / from_shares`` is stored, so the two are
    directly comparable without a mental inversion at every call site.
    """
    splits = _splits(actions)
    found: list[Discontinuity] = []

    for index in range(1, len(closes)):
        previous, current = closes[index - 1], closes[index]
        if previous <= 0 or current <= 0:
            continue

        observed = previous / current
        magnitude = max(observed, 1.0 / observed)
        if magnitude < MARKET_GAP_RATIO:
            continue

        stamp = timestamps[index]
        # A split effective at or before this bar and after the previous one is
        # the only action that could account for *this* discontinuity.
        declared = next(
            (s for s in splits if timestamps[index - 1] < s.effective_at <= stamp),
            None,
        )

        if declared is not None:
            ratio = float(declared.split_ratio)
            kind = (
                DiscontinuityKind.EXPLAINED
                if _explains(observed, ratio)
                else DiscontinuityKind.CONTRADICTED
            )
            found.append(
                Discontinuity(symbol, timeframe, stamp, previous, current, observed, kind, ratio)
            )
            continue

        kind = (
            DiscontinuityKind.UNEXPLAINED
            if magnitude >= SPLIT_LIKE_RATIO
            else DiscontinuityKind.MARKET_GAP
        )
        found.append(Discontinuity(symbol, timeframe, stamp, previous, current, observed, kind))

    return found


def contradicted_actions(
    timestamps: Sequence[datetime],
    closes: Sequence[float],
    actions: Sequence[CorporateAction],
) -> list[tuple[CorporateAction, float]]:
    """Stored splits this series actively contradicts, with the observed ratio.

    The mirror of :func:`classify_series`, and the half that catches HON. That
    function walks *bars* looking for jumps, so an action whose event never
    happened is invisible to it: HON's declared 1-for-2 moved the price by 2%,
    which is not a discontinuity at all. Only walking the *actions* finds it.

    "Contradicted" is narrower than "unconfirmed". An action whose bracketing
    bars straddle a gap wider than :data:`MAX_CORROBORATION_GAP` is
    indeterminate, not contradicted, and is omitted -- the same rule
    ``app.research.adjustments`` applies when deciding whether to adjust. NVDA's
    real 10-for-1 spent a phase being rejected because that distinction was
    missing.
    """
    contradicted: list[tuple[CorporateAction, float]] = []
    for split in _splits(actions):
        after = next((i for i, t in enumerate(timestamps) if t >= split.effective_at), None)
        if after is None or after == 0 or closes[after] <= 0:
            continue
        if timestamps[after] - timestamps[after - 1] > MAX_CORROBORATION_GAP:
            continue
        observed = closes[after - 1] / closes[after]
        if not _explains(observed, float(split.split_ratio)):
            contradicted.append((split, observed))
    return contradicted


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The result of a full scan."""

    findings: list[Discontinuity]
    instruments_scanned: int
    bars_scanned: int

    def of(self, kind: DiscontinuityKind) -> list[Discontinuity]:
        return [f for f in self.findings if f.kind is kind]

    @property
    def healthy(self) -> bool:
        """No jump lacks an explanation, and no stored action is contradicted.

        ``MARKET_GAP`` findings do not affect this: a real 20% move is data
        working correctly, and treating it as a fault would push someone toward
        suppressing genuine market behaviour.
        """
        return not self.of(DiscontinuityKind.UNEXPLAINED) and not self.of(
            DiscontinuityKind.CONTRADICTED
        )


async def scan_price_series(
    session: AsyncSession,
    *,
    timeframes: Sequence[Timeframe] = (Timeframe.D1, Timeframe.H1),
    symbols: Sequence[str] | None = None,
) -> IntegrityReport:
    """Scan stored series for discontinuities the corporate actions do not explain.

    Reads only. Defaults to daily and hourly because those are the series every
    research and production consumer actually reads; the intraday timeframes are
    derived from the same events and would triple the scan for no new finding.
    """
    stmt = select(Instrument.id, Instrument.symbol).order_by(Instrument.symbol)
    if symbols:
        stmt = stmt.where(Instrument.symbol.in_([s.upper() for s in symbols]))
    rows = (await session.execute(stmt)).all()

    action_repository = CorporateActionRepository(session)
    findings: list[Discontinuity] = []
    bars_scanned = 0

    for instrument_id, symbol in rows:
        # No `known_as_of`: this is a data-quality audit of what is stored now,
        # not a point-in-time reconstruction. Hiding later actions would make
        # every historical split look unexplained.
        actions = await action_repository.list_for_instrument(
            instrument_id=instrument_id, symbol=symbol
        )
        for timeframe in timeframes:
            candles = (
                await session.execute(
                    select(Candle.timestamp, Candle.close)
                    .where(Candle.instrument_id == instrument_id, Candle.timeframe == timeframe)
                    .order_by(Candle.timestamp)
                )
            ).all()
            if len(candles) < 2:  # noqa: PLR2004
                continue
            stamps = [c.timestamp for c in candles]
            closes = [float(c.close) for c in candles]
            bars_scanned += len(candles)
            findings.extend(classify_series(symbol, timeframe, stamps, closes, actions))

            # The action walk. A declared split whose event never happened moves
            # no price, so the bar walk above cannot see it -- HON's phantom
            # 1-for-2 is invisible there and caught only here.
            seen = {(f.timestamp, f.kind) for f in findings}
            for split, observed in contradicted_actions(stamps, closes, actions):
                at = next((t for t in stamps if t >= split.effective_at), split.effective_at)
                if (at, DiscontinuityKind.CONTRADICTED) in seen:
                    continue
                index = stamps.index(at) if at in stamps else 0
                findings.append(
                    Discontinuity(
                        symbol=symbol,
                        timeframe=timeframe,
                        timestamp=at,
                        previous_close=closes[index - 1] if index else closes[0],
                        close=closes[index] if index < len(closes) else closes[-1],
                        observed_ratio=observed,
                        kind=DiscontinuityKind.CONTRADICTED,
                        declared_ratio=float(split.split_ratio),
                    )
                )

    report = IntegrityReport(findings, instruments_scanned=len(rows), bars_scanned=bars_scanned)
    logger.info(
        "price series integrity scan",
        instruments=report.instruments_scanned,
        bars=report.bars_scanned,
        unexplained=len(report.of(DiscontinuityKind.UNEXPLAINED)),
        contradicted=len(report.of(DiscontinuityKind.CONTRADICTED)),
        market_gaps=len(report.of(DiscontinuityKind.MARKET_GAP)),
    )
    return report
