"""A descriptive market-context block for #market-trends.

**Built, tested, and deliberately not enabled.** Phase 9A classified real ETF
context as ``NO_ADDITIONAL_INFORMATION`` for direction, so nothing here may
imply an action. What it *did* establish is that volatility is sector-structured
-- a stock's ATR% tracks its own sector fund at r = 0.75 against r = 0.66 for
SPY, and the sector is the closer reference for 33 of 52 names -- which is a
description worth showing and not a prediction.

The rendering is therefore restricted to three statements of fact:

* what the market reference did,
* what the stock's sector reference did,
* the arithmetic difference between the stock and each.

No score, no ranking, no "strong", no forward-looking verb. A difference in
percentage points is an observation about the last session; calling it strength
would be the claim the research declined to support.

:func:`~app.notifications.trends.assert_no_recommendation_language` runs over the
rendered **lines**, matching how ``volatility_events`` applies it. The disclaimer
is exempt for the same reason it is there: it is the one string allowed to name
what this is not, and the guard is a plain substring check that cannot tell
"recommendation" from "not a recommendation".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

CONTEXT_TITLE: Final = "MARKET CONTEXT"

CONTEXT_DISCLAIMER: Final = "Relative movement already observed — not a trade recommendation."

MAX_CONTEXT_SYMBOLS: Final = 5
"""How many stocks get a comparison block.

Small on purpose. The brief's own instruction is one compact section, and a
52-line context dump would bury the movers list this is appended to.
"""


@dataclass(frozen=True, slots=True)
class ReferenceMove:
    """One reference instrument's last-session move."""

    symbol: str
    return_pct: float
    trend_state: str | None = None

    def render(self) -> str:
        state = f"   {self.trend_state}" if self.trend_state else ""
        return f"{self.symbol:<6}{self.return_pct:>+7.1f}%{state}"


@dataclass(frozen=True, slots=True)
class StockContext:
    """One stock against its market and sector references."""

    symbol: str
    return_pct: float
    market_symbol: str
    versus_market_pp: float
    sector_symbol: str | None = None
    versus_sector_pp: float | None = None

    def render(self) -> list[str]:
        lines = [f"{self.symbol:<6}{self.return_pct:>+7.1f}%"]
        if self.sector_symbol is not None and self.versus_sector_pp is not None:
            lines.append(f"  vs {self.sector_symbol:<5}{self.versus_sector_pp:>+6.1f}pp")
        lines.append(f"  vs {self.market_symbol:<5}{self.versus_market_pp:>+6.1f}pp")
        return lines


def build_context_block(
    references: list[ReferenceMove],
    stocks: list[StockContext],
    *,
    limit: int = MAX_CONTEXT_SYMBOLS,
) -> dict[str, Any] | None:
    """The payload block, or ``None`` when there is nothing factual to say.

    Returns ``None`` rather than an empty block for the same reason the movers
    list does: a section header with no content reads as a failure, and a
    reader cannot tell it apart from one.
    """
    if not references:
        return None

    lines = [reference.render() for reference in references]
    if stocks:
        lines.append("")
        for stock in stocks[:limit]:
            lines.extend(stock.render())

    return {
        "title": CONTEXT_TITLE,
        "lines": lines,
        "disclaimer": CONTEXT_DISCLAIMER,
    }
