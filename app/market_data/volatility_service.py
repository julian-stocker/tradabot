"""Loading bars for the volatility engine, and rendering what it produces.

**Computed on demand, never stored.** The decision is deliberate and cheap to
justify: every input already exists in `candles`, an estimate is a pure function
of the trailing window, and 52 symbols x 267 bars is a bounded read that finishes
in well under a second. Persisting it would duplicate candle-derived data --
which the phase brief explicitly warns against -- and would add a table that can
go stale, disagree with the candles it came from, and need a migration to change.

The one thing that *is* frozen rather than recomputed is the calibration, and
that lives in :mod:`app.market_data.volatility` as constants, because it is model
parameters rather than data.

No provider call happens here. The trends job already runs on stored candles, and
adding a fetch would make a descriptive channel able to break the market-data
budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.instruments.repository import InstrumentRepository
from app.market_data.repository import CandleRepository
from app.market_data.volatility import (
    MODEL_VERSION,
    PRIMARY_TIMEFRAME,
    REQUIRED_BARS,
    ExpectedMovement,
    VolatilityRegime,
    estimate,
)

logger = get_logger(__name__)

DISCLAIMER: Final = "Magnitude estimate only — not a direction forecast."
"""Attached to every rendered payload.

Phases 6-8 found no directional edge in this data. A range figure with no such
line beside it reads as a forecast to anyone skimming, and that would be the
product claiming something its own research refused to support.
"""


@dataclass(frozen=True, slots=True)
class VolatilitySnapshot:
    """Expected movement across the watchlist at one instant."""

    calculated_at: datetime
    estimates: list[ExpectedMovement]
    symbols_requested: int
    symbols_failed: int

    @property
    def by_regime(self) -> dict[VolatilityRegime, int]:
        counts: dict[VolatilityRegime, int] = dict.fromkeys(VolatilityRegime, 0)
        for item in self.estimates:
            counts[item.regime] += 1
        return counts

    @property
    def elevated(self) -> list[ExpectedMovement]:
        """HIGH and EXTREME, most unusual first."""
        return sorted(
            (item for item in self.estimates if item.regime.is_elevated),
            key=lambda item: item.percentile,
            reverse=True,
        )

    @property
    def healthy(self) -> bool:
        return bool(self.estimates) and self.symbols_failed == 0

    def stale(self, *, now: datetime | None = None) -> list[ExpectedMovement]:
        moment = now or utc_now()
        return [item for item in self.estimates if item.is_stale(now=moment)]


class VolatilityService:
    """Computes expected movement from stored candles. **Reads only.**"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_symbols(
        self, symbols: list[str], *, now: datetime | None = None
    ) -> VolatilitySnapshot:
        """Estimate every symbol that has enough history.

        One symbol's missing history is recorded and the rest continue: a newly
        added ticker should not blank the whole channel.
        """
        moment = now or utc_now()
        instruments = InstrumentRepository(self._session)
        candles = CandleRepository(self._session)

        estimates: list[ExpectedMovement] = []
        failed = 0

        for symbol in symbols:
            try:
                instrument = await instruments.get_by_symbol(symbol)
                if instrument is None:
                    failed += 1
                    continue
                bars = await candles.get_latest(
                    instrument_id=instrument.id,
                    timeframe=PRIMARY_TIMEFRAME,
                    limit=REQUIRED_BARS,
                )
                if not bars:
                    failed += 1
                    continue
                result = estimate(
                    symbol=symbol,
                    highs=[float(bar.high) for bar in bars],
                    lows=[float(bar.low) for bar in bars],
                    closes=[float(bar.close) for bar in bars],
                    bar_timestamp=bars[-1].timestamp,
                    now=moment,
                )
                if result is None:
                    failed += 1
                    continue
                estimates.append(result)
            # A single symbol's failure is data, not an outage.
            except Exception as exc:
                failed += 1
                logger.debug("volatility estimate failed", symbol=symbol, error=safe_message(exc))

        logger.info(
            "volatility snapshot computed",
            model=MODEL_VERSION,
            estimated=len(estimates),
            failed=failed,
        )
        return VolatilitySnapshot(
            calculated_at=moment,
            estimates=estimates,
            symbols_requested=len(symbols),
            symbols_failed=failed,
        )


def describe(movement: ExpectedMovement, *, now: datetime | None = None) -> dict[str, Any]:
    """One symbol's estimate as a payload. **Magnitude fields only.**

    There is deliberately no price, target or direction key. A downstream
    formatter cannot render what is not here, which is a stronger guarantee than
    a convention that the next contributor has to know about.
    """
    moment = now or utc_now()
    age = movement.data_age(now=moment)
    return {
        "symbol": movement.symbol,
        "regime": movement.regime.value,
        "percentile": round(movement.percentile * 100),
        "typical_range_pct": movement.typical_range_pct,
        "stress_range_pct": movement.stress_range_pct,
        "recent_range_pct": round(movement.recent_range_pct, 2),
        "atr_pct": round(movement.atr_pct, 2),
        "data_age_minutes": int(age.total_seconds() // 60),
        "stale": movement.is_stale(now=moment),
        "model": movement.model_version,
    }


def render_line(movement: ExpectedMovement, *, now: datetime | None = None) -> str:
    """A single readable line for a Discord list.

    Ordered the way someone skims: which symbol, how unusual, how much movement
    that implies, how fresh. A stale estimate says so inline rather than being
    dropped, because silence would look like the symbol became calm.
    """
    moment = now or utc_now()
    age = int(movement.data_age(now=moment).total_seconds() // 60)
    stale = "  ⚠️ stale" if movement.is_stale(now=moment) else ""
    return (
        f"**{movement.symbol}** — {movement.regime.value} "
        f"({movement.percentile * 100:.0f}th pct)\n"
        f"   typical session ~{movement.typical_range_pct:.1f}%  ·  "
        f"stress ~{movement.stress_range_pct:.1f}%  ·  "
        f"recent {movement.recent_range_pct:.1f}%  ·  {age}m old{stale}"
    )


def build_payload(
    snapshot: VolatilitySnapshot, *, limit: int = 5, now: datetime | None = None
) -> dict[str, Any]:
    """The #market-trends payload for elevated-volatility symbols.

    Ranked by **volatility percentile**, which is a statement about how unusual
    the activity is -- not about how attractive it is. Nothing here is ordered by
    expected profit, because no such quantity has been validated.
    """
    moment = now or utc_now()
    top = snapshot.elevated[:limit]
    return {
        "title": "📊 EXPECTED MOVEMENT",
        "movers": [describe(item, now=moment) for item in top],
        "lines": [render_line(item, now=moment) for item in top],
        "disclaimer": DISCLAIMER,
        "model": MODEL_VERSION,
        "evaluated": len(snapshot.estimates),
    }
