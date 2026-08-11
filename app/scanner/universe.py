"""The initial development universe.

Roughly fifty liquid US equities spread across nine sectors. **Data, not logic**:
the scanner reads the watchlist table, and this module only seeds it. Changing
the universe is a database operation, never a code change.

> **Inclusion here is not an investment recommendation.** These are development
> fixtures, chosen for liquidity and sector spread so the scanner is exercised
> against varied behaviour. Nothing in tradabot reads this list to decide
> anything, and no view about any company is expressed or implied.

Why fifty and not five hundred
------------------------------
The multiple-comparisons hazard recorded in :mod:`app.scanner.models` since phase
1: scanning a large universe for "score > 75" returns hits every day whether or
not the signal predicts anything. Fifty instruments keeps the base rate
interpretable and a scan cycle fast enough to run every fifteen minutes on a
laptop. Growing the universe is a later decision that should be made *after*
phase 5 can measure whether the hits mean anything.

Why multiple sectors
--------------------
A technology-only watchlist is one bet with fifty tickers on it. When the sector
moves, every name qualifies at once, and a scanner that lights up entirely on
such a day looks like it found fifty opportunities when it found one. Sector
spread makes that failure visible instead of flattering.
"""

from __future__ import annotations

from dataclasses import dataclass

TECHNOLOGY = "technology"
SEMICONDUCTORS = "semiconductors"
COMMUNICATION = "communication"
CONSUMER_DISCRETIONARY = "consumer-discretionary"
FINANCIALS = "financials"
HEALTHCARE = "healthcare"
INDUSTRIALS = "industrials"
ENERGY = "energy"
CONSUMER_STAPLES = "consumer-staples"


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One seeded instrument."""

    symbol: str
    sector: str
    priority: int = 0
    """Higher is scanned first when a cycle is time-bounded. All zero by default:
    ranking instruments before they are evaluated would be a prior nobody has
    justified."""

    @property
    def tags(self) -> tuple[str, ...]:
        return (self.sector,)


INITIAL_UNIVERSE: tuple[UniverseEntry, ...] = (
    # Large-cap technology
    UniverseEntry("AAPL", TECHNOLOGY),
    UniverseEntry("MSFT", TECHNOLOGY),
    UniverseEntry("GOOGL", TECHNOLOGY),
    UniverseEntry("ORCL", TECHNOLOGY),
    UniverseEntry("CRM", TECHNOLOGY),
    UniverseEntry("ADBE", TECHNOLOGY),
    # Semiconductors -- separated from technology because they behave like a
    # distinct, far more cyclical asset class.
    UniverseEntry("NVDA", SEMICONDUCTORS),
    UniverseEntry("AMD", SEMICONDUCTORS),
    UniverseEntry("INTC", SEMICONDUCTORS),
    UniverseEntry("AVGO", SEMICONDUCTORS),
    UniverseEntry("QCOM", SEMICONDUCTORS),
    UniverseEntry("TXN", SEMICONDUCTORS),
    UniverseEntry("MU", SEMICONDUCTORS),
    # Communication services
    UniverseEntry("META", COMMUNICATION),
    UniverseEntry("NFLX", COMMUNICATION),
    UniverseEntry("DIS", COMMUNICATION),
    UniverseEntry("T", COMMUNICATION),
    UniverseEntry("VZ", COMMUNICATION),
    # Consumer discretionary
    UniverseEntry("AMZN", CONSUMER_DISCRETIONARY),
    UniverseEntry("TSLA", CONSUMER_DISCRETIONARY),
    UniverseEntry("HD", CONSUMER_DISCRETIONARY),
    UniverseEntry("MCD", CONSUMER_DISCRETIONARY),
    UniverseEntry("NKE", CONSUMER_DISCRETIONARY),
    UniverseEntry("SBUX", CONSUMER_DISCRETIONARY),
    # Financials
    UniverseEntry("JPM", FINANCIALS),
    UniverseEntry("BAC", FINANCIALS),
    UniverseEntry("GS", FINANCIALS),
    UniverseEntry("MS", FINANCIALS),
    UniverseEntry("V", FINANCIALS),
    UniverseEntry("MA", FINANCIALS),
    UniverseEntry("BRK.B", FINANCIALS),
    # Healthcare
    UniverseEntry("UNH", HEALTHCARE),
    UniverseEntry("JNJ", HEALTHCARE),
    UniverseEntry("LLY", HEALTHCARE),
    UniverseEntry("PFE", HEALTHCARE),
    UniverseEntry("ABBV", HEALTHCARE),
    UniverseEntry("MRK", HEALTHCARE),
    # Industrials
    UniverseEntry("CAT", INDUSTRIALS),
    UniverseEntry("BA", INDUSTRIALS),
    UniverseEntry("HON", INDUSTRIALS),
    UniverseEntry("GE", INDUSTRIALS),
    UniverseEntry("UPS", INDUSTRIALS),
    UniverseEntry("LMT", INDUSTRIALS),
    # Energy
    UniverseEntry("XOM", ENERGY),
    UniverseEntry("CVX", ENERGY),
    UniverseEntry("COP", ENERGY),
    UniverseEntry("SLB", ENERGY),
    # Consumer staples
    UniverseEntry("PG", CONSUMER_STAPLES),
    UniverseEntry("KO", CONSUMER_STAPLES),
    UniverseEntry("PEP", CONSUMER_STAPLES),
    UniverseEntry("WMT", CONSUMER_STAPLES),
    UniverseEntry("COST", CONSUMER_STAPLES),
)

SECTORS: tuple[str, ...] = (
    TECHNOLOGY,
    SEMICONDUCTORS,
    COMMUNICATION,
    CONSUMER_DISCRETIONARY,
    FINANCIALS,
    HEALTHCARE,
    INDUSTRIALS,
    ENERGY,
    CONSUMER_STAPLES,
)


def universe_symbols() -> tuple[str, ...]:
    return tuple(entry.symbol for entry in INITIAL_UNIVERSE)


def by_sector() -> dict[str, tuple[str, ...]]:
    """The universe grouped by sector, for documentation and the seed command."""
    grouped: dict[str, list[str]] = {sector: [] for sector in SECTORS}
    for entry in INITIAL_UNIVERSE:
        grouped[entry.sector].append(entry.symbol)
    return {sector: tuple(symbols) for sector, symbols in grouped.items()}
