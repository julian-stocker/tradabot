"""Answering ``/check`` by asking the layers that already know.

This module orchestrates. It does not analyse.

Every figure in a ``/check`` response was computed by a service that owns it:
trailing revenue, margins, cash generation, the balance sheet, dilution and the
valuation percentile come from :class:`~app.advisor.service.AdvisorService`;
weights, concentration and correlation come from Portfolio Fit. Nothing here
recomputes any of them, and a structural test asserts that this package contains
no such arithmetic.

The reason is not tidiness. A second implementation of trailing revenue would
agree with the first for about a quarter, and then a company would change its
reporting taxonomy and Discord would quietly start disagreeing with the Advisor
about the same company on the same day.

Company analysis only
---------------------
``/check`` answers "what do we know about this company and its stock". It
deliberately does **not** answer "how would this fit my portfolio": that is a
different question with a different owner, and putting both on one card meant a
reader had to separate them by eye every time.

The practical consequence is that ``/check`` needs no broker at all. It cannot
be slowed by an Alpaca timeout, cannot be degraded by an unconfigured account,
and works identically whether or not any paper slot is readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.discord_bot.resolve import Availability, Resolution, Resolved, resolve
from app.discord_bot.timing import Timings

logger = get_logger(__name__)

@dataclass(frozen=True, slots=True)
class StockCheck:
    """Everything one ``/check`` produced, ready to render.

    The two availability flags are independent on purpose. A security may have
    prices without fundamentals or the reverse, and flattening them into a
    single "supported" would lose exactly the distinction a user needs when
    something is missing.
    """

    requested: str
    symbol: str
    resolution: Resolution
    market_data: Availability
    fundamentals: Availability
    as_of: str
    checked_at: datetime
    report: Any = None
    """The Advisor's own report object, unmodified."""
    suggestion: str | None = None
    detail: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def analysable(self) -> bool:
        return self.report is not None or self.market_data is Availability.AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "symbol": self.symbol,
            "resolution": str(self.resolution),
            "market_data": str(self.market_data),
            "fundamentals": str(self.fundamentals),
            "as_of": self.as_of,
            "checked_at": self.checked_at.isoformat(),
            "suggestion": self.suggestion,
            "detail": self.detail,
            "notes": list(self.notes),
        }


class StockAnalyst:
    """Turns a typed ticker into a :class:`StockCheck`.

    Args:
        advisor: the production Advisor. Not a subset of it.
        universe: canonical instrument symbols with price history.
        fundamentals: symbols the fact store holds, for resolution.
        fact_store_ready: whether the fact store is usable.
        as_of: latest session the data covers.
    """

    def __init__(
        self,
        *,
        advisor: Any,
        universe: Sequence[str],
        fundamentals: frozenset[str] | None,
        fact_store_ready: bool,
        as_of: str,
    ) -> None:
        self._advisor = advisor
        self._universe = universe
        self._fundamentals = fundamentals
        self._ready = fact_store_ready
        self._as_of = as_of

    def check(
        self, raw: str, *, now: datetime | None = None, timings: Timings | None = None
    ) -> StockCheck:
        """Analyse one symbol. **Never raises.**

        A command handler that could throw would leave the user staring at
        Discord's "the application did not respond", which says nothing about
        what went wrong.
        """
        moment = now or datetime.now(UTC)
        clock = timings or Timings()
        with clock.stage("resolve"):
            found = resolve(
                raw,
                universe=self._universe,
                fundamentals=self._fundamentals,
                fact_store_ready=self._ready,
            )
        if found.resolution in (
            Resolution.UNKNOWN_SYMBOL,
            Resolution.MALFORMED_SYMBOL,
        ):
            return self._empty(found, moment)

        report = None
        notes: list[str] = []
        if found.fundamentals is Availability.AVAILABLE or self._ready:
            try:
                with clock.stage("advisor"):
                    report = self._advisor.analyse(found.symbol, as_of=self._as_of)
            except Exception as exc:
                logger.warning(
                    "advisor analysis failed",
                    symbol=found.symbol,
                    reason=type(exc).__name__,
                )
                return StockCheck(
                    requested=found.requested,
                    symbol=found.symbol,
                    resolution=Resolution.ANALYSIS_FAILED,
                    market_data=found.market_data,
                    fundamentals=found.fundamentals,
                    as_of=self._as_of,
                    checked_at=moment,
                    detail="The analysis could not be completed for this symbol.",
                )

        if found.resolution is Resolution.DATA_NOT_SYNCED:
            notes.append(
                "Company fundamentals are unavailable until the fact store is synced."
            )
        return StockCheck(
            requested=found.requested,
            symbol=found.symbol,
            resolution=found.resolution,
            market_data=found.market_data,
            fundamentals=found.fundamentals,
            as_of=self._as_of,
            checked_at=moment,
            report=report,
            suggestion=found.suggestion,
            detail=found.detail,
            notes=tuple(notes),
        )

    # ------------------------------------------------------------- internals
    def _empty(self, found: Resolved, moment: datetime) -> StockCheck:
        return StockCheck(
            requested=found.requested,
            symbol=found.symbol,
            resolution=found.resolution,
            market_data=found.market_data,
            fundamentals=found.fundamentals,
            as_of=self._as_of,
            checked_at=moment,
            suggestion=found.suggestion,
            detail=found.detail,
        )
