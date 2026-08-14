"""Split-adjusted bar reads for consumers that do not compute features.

Why this exists
---------------
``FeatureService`` already composes candles with corporate actions and defaults
to ``SPLIT_ADJUSTED``, which is why the scanner, the backtester, paper replay
and the API were all safe. But two production consumers do not want features --
they want raw-ish bars and their own small calculation -- so they went straight
to ``CandleRepository`` and, in doing so, straight past the adjustment:

* ``app.market_data.volatility_service`` computes ATR% over a trailing window.
  A split inside that window inflates true range enormously; phase 9A measured
  ``atr_pct`` overstated 24x at the tail in the research frames, and the same
  arithmetic drives volatility-v1's regime.
* ``app.notifications.trends_service`` computes 1-day and 5-day percentage
  change for #market-trends. A split would post a -50% "mover" to Discord.

The fix belongs here rather than in either caller. Two independent patches would
have meant two places to forget next time, and the next consumer that needs
"just the recent bars" would start the cycle again. This is the lowest layer
where "a bar" and "a corporate action" are both available.

What it deliberately does not do
--------------------------------
It does not touch stored candles. Storage stays raw and authoritative -- see
``docs/data-adjustments.md`` for why adjusting on read beats rewriting history --
and this returns adjusted copies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.corporate_actions.adjust import AdjustedCandle, adjust_candles
from app.corporate_actions.repository import CorporateActionRepository
from app.domain.enums import CorporateActionType, PriceSeriesAdjustment, Timeframe
from app.market_data.integrity import contradicted_actions
from app.market_data.repository import CandleRepository

logger = get_logger(__name__)

DEFAULT_ADJUSTMENT: Final = PriceSeriesAdjustment.SPLIT_ADJUSTED
"""Matches ``app.features.service.DEFAULT_ADJUSTMENT``.

Stated as its own constant rather than imported so this module does not depend
on the feature package, and pinned to it by test so the two cannot drift.
"""


@dataclass(frozen=True, slots=True)
class AdjustedSeries:
    """Adjusted bars plus what shaped them."""

    bars: list[AdjustedCandle]
    adjustment: PriceSeriesAdjustment
    splits_applied: int
    """How many splits touched this window. Zero is the overwhelmingly common
    case and means the series is byte-identical to the raw one."""

    def __len__(self) -> int:
        return len(self.bars)

    def __bool__(self) -> bool:
        return bool(self.bars)


class AdjustedCandleReader:
    """Reads recent bars with corporate actions applied. **Reads only.**"""

    def __init__(self, session: AsyncSession) -> None:
        self._candles = CandleRepository(session)
        self._actions = CorporateActionRepository(session)

    async def latest(
        self,
        *,
        instrument_id: int,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        as_of: datetime | None = None,
        adjustment: PriceSeriesAdjustment = DEFAULT_ADJUSTMENT,
    ) -> AdjustedSeries:
        """The newest ``limit`` bars, adjusted.

        Args:
            as_of: restrict corporate actions to those already effective at this
                instant. Live callers leave it ``None``; a replay must pass it,
                because adjusting a 2021 window with a 2024 split would be
                exactly the backward leak this project forbids.

        Returns an empty series rather than raising when there is no history:
        a newly added ticker having no bars is ordinary, and the callers here
        already treat "no data" as a per-symbol skip.
        """
        bars = await self._candles.get_latest(
            instrument_id=instrument_id, timeframe=timeframe, limit=limit
        )
        if not bars:
            return AdjustedSeries([], adjustment, 0)

        actions = await self._actions.list_for_instrument(
            instrument_id=instrument_id,
            symbol=symbol,
            known_as_of=as_of,
            action_types=[CorporateActionType.SPLIT],
        )

        # Corroborate before applying. The research layer has always done this;
        # production had not, and wiring these callers into the adjustment layer
        # without it would have applied HON's phantom 1-for-2 -- doubling every
        # bar before 2026-06-29 and inventing exactly the discontinuity this
        # module exists to remove. A stored action is not evidence its event
        # happened.
        stamps = [b.timestamp for b in bars]
        closes = [float(b.close) for b in bars]
        denied = {id(a) for a, _ in contradicted_actions(stamps, closes, actions)}
        if denied:
            logger.warning(
                "ignoring corporate actions the price series contradicts",
                symbol=symbol,
                timeframe=timeframe.value,
                ignored=len(denied),
            )
        trusted = [a for a in actions if id(a) not in denied]

        # Only splits inside the returned window can change it. Counting them
        # here makes the common no-op case observable rather than assumed.
        window_start = bars[0].timestamp
        relevant = [a for a in trusted if a.effective_at > window_start]
        if relevant:
            logger.info(
                "adjusting window for splits",
                symbol=symbol,
                timeframe=timeframe.value,
                splits=len(relevant),
            )

        return AdjustedSeries(
            bars=adjust_candles(bars, trusted, adjustment),
            adjustment=adjustment,
            splits_applied=len(relevant),
        )
