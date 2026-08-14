"""Real market and sector reference instruments.

Phase 6 measured market context with an **equal-weight proxy built from the
watchlist itself** (``app.research.featureset.market_proxy``): at each bar it
averaged the returns of whatever the 52 symbols were doing, and called that "the
market". Sector context was the same construction within a watchlist tag. That
was an honest stand-in -- there was no ETF in the universe to use instead -- and
it is the family that produced the largest separation phase 6 found (5.5pp,
``REGIME_DEPENDENT``).

That result is the reason this module exists. A 5.5pp spread from a proxy which
is *definitionally correlated with its own constituents* is not evidence about
the market; it may only be evidence that a stock tends to move with the average
of 51 other stocks measured at the same instant. Distinguishing the two requires
a reference series that is **not** built from the universe under test.

What is deliberately not done here
----------------------------------
These instruments are registered into ``instruments`` and **never onto the
watchlist**. ``WatchlistRepository.add`` enables an entry unconditionally, and
the scanner universe is exactly ``WatchlistEntry.enabled.is_(True)`` -- so
watchlisting SPY would put an ETF through signal-v1, change qualified-signal
volume, and alter what Discord says. Benchmarks are *context*, not candidates.
:func:`watchlisted_benchmarks` exists so that invariant can be asserted rather
than assumed.

Sector mapping follows the tags this database actually uses
-----------------------------------------------------------
Not GICS. The watchlist tags ``semiconductors`` separately from ``technology``
and files GOOGL under ``technology`` rather than communication services. Mapping
to a textbook taxonomy would produce sector returns that do not describe the
groups the research code actually forms, so the mapping below is keyed on the
stored tag strings and will raise on an unrecognised one rather than silently
returning no benchmark.

These funds split too
---------------------
Bars are stored **RAW** -- Alpaca is asked for unadjusted prices on purpose
(``Adjustment.RAW`` in the provider) because tradabot adjusts on read, which
``app.research.adjustments`` now does for the research frames. It matters here:
SOXX split 3-for-1 on 2024-03-07, and XLE, XLK and XLY all split 2-for-1 on
2025-12-05 -- inside the research windows these funds exist to provide context
for. An unadjusted sector reference would have carried a -50% bar in exactly the
column this phase was built to measure. See ``docs/market-context.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Instrument, WatchlistEntry
from app.domain.enums import AssetType, Timeframe
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import InstrumentInfo

logger = get_logger(__name__)

BENCHMARK_TIMEFRAMES: Final[tuple[Timeframe, ...]] = (
    Timeframe.D1,
    Timeframe.H1,
    Timeframe.M15,
    Timeframe.M5,
)
"""The four timeframes the universe already stores, in coarse-to-fine order."""

BACKFILL_START: Final[dict[Timeframe, str]] = {
    Timeframe.D1: "2020-07-27",
    Timeframe.H1: "2020-07-27",
    Timeframe.M15: "2024-08-01",
    Timeframe.M5: "2025-02-03",
}
"""First date to request per timeframe, mirroring the stored stock coverage.

These are the *measured* first bars of the 52-symbol universe, not a wish. A
benchmark series that started later than the stocks it contextualises would
silently drop the early part of every study to a NULL join; one that started
earlier would download bars no observation can ever reference. Kept as an
explicit table so a mismatch is a visible diff rather than a quiet
recomputation.
"""


class ContextRole(StrEnum):
    """What a context instrument is a reference *for*.

    The scanner never sees these. They exist so one module can answer "which
    series is this stock's market" and "which is its sector" without any caller
    re-deriving the answer from a ticker string.
    """

    MARKET = "MARKET"
    SECTOR = "SECTOR"
    ALTERNATE = "ALTERNATE"
    """Registered and backfilled, but mapped to nothing.

    Kept so a selection made in phase 9A can be revisited against stored data
    rather than re-downloaded -- SOXX is here because SMH beat it on measured
    intraday continuity, not because SOXX is unusable.
    """


@dataclass(frozen=True, slots=True)
class ContextInstrument:
    """One reference instrument and the role it plays.

    ``sector`` is the **watchlist tag** this fund stands in for, or ``None`` for
    market and alternate references. ``parent`` names a broader context this one
    sits inside -- semiconductors are a subset of technology, and part D of the
    phase-9A brief asks for that hierarchy rather than a choice between the two.
    """

    symbol: str
    name: str
    exchange: str
    role: ContextRole
    sector: str | None = None
    parent: str | None = None

    @property
    def is_market(self) -> bool:
        return self.role is ContextRole.MARKET


MARKET_BENCHMARK: Final = ContextInstrument(
    symbol="SPY",
    name="SPDR S&P 500 ETF Trust",
    exchange="ARCX",
    role=ContextRole.MARKET,
)
"""The primary whole-market reference.

SPY rather than an index level: this project stores traded bars from one
provider, and an index quote is neither traded nor available on the market-data
plan in use. SPY tracks the S&P 500 with a tracking difference far smaller than
any effect phase 6 was looking for, and unlike ``^GSPC`` it has real volume,
real session boundaries and the same granularity as everything else in the
candle table.
"""

SECONDARY_MARKET_BENCHMARK: Final = ContextInstrument(
    symbol="QQQ",
    name="Invesco QQQ Trust, Series 1",
    exchange="XNAS",
    role=ContextRole.MARKET,
)
"""The second market reference, and not a redundant one.

This universe is heavily large-cap technology: 13 of 52 names are tagged
``technology`` or ``semiconductors``. SPY and QQQ diverge most in exactly that
regime, so carrying both is what allows "was this a market move or a Nasdaq
move" to be asked at all. Whether it *answers* anything is part J's problem.
"""

MARKET_BENCHMARKS: Final[tuple[ContextInstrument, ...]] = (
    MARKET_BENCHMARK,
    SECONDARY_MARKET_BENCHMARK,
)

SECTOR_BENCHMARKS: Final[tuple[ContextInstrument, ...]] = (
    ContextInstrument(
        "XLK", "Technology Select Sector SPDR Fund", "ARCX", ContextRole.SECTOR, "technology"
    ),
    ContextInstrument(
        "SMH",
        "VanEck Semiconductor ETF",
        "XNAS",
        ContextRole.SECTOR,
        "semiconductors",
        parent="XLK",
    ),
    ContextInstrument(
        "XLC",
        "Communication Services Select Sector SPDR Fund",
        "ARCX",
        ContextRole.SECTOR,
        "communication",
    ),
    ContextInstrument(
        "XLY",
        "Consumer Discretionary Select Sector SPDR Fund",
        "ARCX",
        ContextRole.SECTOR,
        "consumer-discretionary",
    ),
    ContextInstrument(
        "XLP",
        "Consumer Staples Select Sector SPDR Fund",
        "ARCX",
        ContextRole.SECTOR,
        "consumer-staples",
    ),
    ContextInstrument(
        "XLF", "Financial Select Sector SPDR Fund", "ARCX", ContextRole.SECTOR, "financials"
    ),
    ContextInstrument(
        "XLV", "Health Care Select Sector SPDR Fund", "ARCX", ContextRole.SECTOR, "healthcare"
    ),
    ContextInstrument(
        "XLI", "Industrial Select Sector SPDR Fund", "ARCX", ContextRole.SECTOR, "industrials"
    ),
    ContextInstrument(
        "XLE", "Energy Select Sector SPDR Fund", "ARCX", ContextRole.SECTOR, "energy"
    ),
)
"""One canonical fund per watchlist sector tag.

**SMH, not SOXX, and not XLK.** Measured through this provider over
2020-07-27 onwards, the two semiconductor funds are equivalent on session
coverage (1,519 daily bars each, none missing against SPY) but not on intraday
continuity: SOXX has 45 sessions with fewer than six hourly bars against SMH's
13. Thin sessions are what turn into NULL context joins, so SMH is the cleaner
series for hourly research.

XLK does not carry ``semiconductors`` because the watchlist splits chipmakers
out of ``technology`` and XLK would double-count them -- several of its largest
holdings are the same names tagged ``semiconductors`` here. Instead SMH declares
XLK as its ``parent``, which is the hierarchy the brief asks for: a chipmaker
has both a semiconductor context and a broader technology one.

XLU, XLRE and XLB are deliberately absent. All three are available and complete
through this provider, and **no stock in the 52-symbol universe maps to them**.
Registering an unmapped sector fund would add a series nothing joins to.
"""

ALTERNATE_BENCHMARKS: Final[tuple[ContextInstrument, ...]] = (
    ContextInstrument("SOXX", "iShares Semiconductor ETF", "XNAS", ContextRole.ALTERNATE),
)

BENCHMARKS: Final[tuple[ContextInstrument, ...]] = (
    *MARKET_BENCHMARKS,
    *SECTOR_BENCHMARKS,
    *ALTERNATE_BENCHMARKS,
)
"""The CONTEXT_UNIVERSE: every instrument that describes the market rather than
being a candidate in it. The TRADE_UNIVERSE is the enabled watchlist, and the
two are disjoint by construction -- see :func:`watchlisted_benchmarks`.
"""

BENCHMARK_SYMBOLS: Final[tuple[str, ...]] = tuple(b.symbol for b in BENCHMARKS)

_BY_SECTOR: Final[dict[str, ContextInstrument]] = {
    b.sector: b for b in SECTOR_BENCHMARKS if b.sector is not None
}

_BY_SYMBOL: Final[dict[str, ContextInstrument]] = {b.symbol: b for b in BENCHMARKS}


def market_benchmark() -> ContextInstrument:
    """The primary whole-market reference instrument."""
    return MARKET_BENCHMARK


def parent_benchmark(sector: str) -> ContextInstrument | None:
    """The broader context a sector sits inside, if it has one.

    Only semiconductors currently does. Returns ``None`` rather than falling
    back to the market reference: "this sector has no parent" and "its parent is
    the whole market" are different statements, and collapsing them would make
    every sector look hierarchical.
    """
    benchmark = _BY_SECTOR.get(sector)
    if benchmark is None or benchmark.parent is None:
        return None
    return _BY_SYMBOL.get(benchmark.parent)


def sector_benchmark(sector: str) -> ContextInstrument:
    """The reference fund for one watchlist sector tag.

    Raises:
        KeyError: if the tag has no mapping. Deliberately not ``None``: a new
            watchlist tag arriving without a benchmark must fail loudly, because
            the quiet alternative is a whole sector silently losing its context
            column and looking like missing data.
    """
    try:
        return _BY_SECTOR[sector]
    except KeyError:
        known = ", ".join(sorted(_BY_SECTOR))
        msg = f"no sector benchmark for tag {sector!r}; known tags: {known}"
        raise KeyError(msg) from None


def is_benchmark(symbol: str) -> bool:
    """Whether a ticker is a reference instrument rather than a candidate.

    Research code uses this to keep benchmarks out of the cross-section: leaving
    SPY in a breadth calculation would let the reference vote on itself.
    """
    return symbol.upper() in set(BENCHMARK_SYMBOLS)


def benchmark_infos() -> list[InstrumentInfo]:
    """The catalogue as provider-shaped metadata, ready to upsert.

    ``listed_at`` is left NULL rather than guessed. Every fund here listed years
    before the earliest stored bar, so a lifecycle bound would exclude nothing;
    inventing a date to fill the column would put a number in the database that
    no source supports.
    """
    return [
        InstrumentInfo(
            symbol=b.symbol,
            name=b.name,
            exchange=b.exchange,
            currency="USD",
            asset_type=AssetType.ETF,
        )
        for b in BENCHMARKS
    ]


@dataclass(frozen=True, slots=True)
class RegistrationReport:
    """What :func:`register_benchmarks` did."""

    registered: tuple[str, ...]
    already_present: tuple[str, ...]

    def summary(self) -> str:
        lines = [
            f"benchmarks    : {len(self.registered) + len(self.already_present)}",
            f"newly created : {', '.join(self.registered) if self.registered else '(none)'}",
            f"already stored: "
            f"{', '.join(self.already_present) if self.already_present else '(none)'}",
        ]
        return "\n".join(lines)


async def register_benchmarks(session: AsyncSession, *, provider: str) -> RegistrationReport:
    """Ensure every benchmark exists in ``instruments``. Idempotent.

    Writes only to ``instruments``. The watchlist is not touched, so the scanner
    universe is unchanged by construction rather than by care.
    """
    repository = InstrumentRepository(session)

    existing: list[str] = []
    for benchmark in BENCHMARKS:
        if await repository.get_by_symbol(benchmark.symbol) is not None:
            existing.append(benchmark.symbol)

    await repository.upsert_many(benchmark_infos(), provider=provider)
    await session.flush()

    created = tuple(s for s in BENCHMARK_SYMBOLS if s not in set(existing))
    logger.info("registered benchmarks", created=len(created), existing=len(existing))
    return RegistrationReport(registered=created, already_present=tuple(existing))


async def watchlisted_benchmarks(session: AsyncSession) -> Sequence[str]:
    """Benchmark symbols that have leaked onto the **enabled** watchlist.

    Must always be empty. Returned rather than asserted so both the CLI and the
    test suite can report the offending tickers instead of a bare failure.
    """
    stmt = (
        select(Instrument.symbol)
        .join(WatchlistEntry, WatchlistEntry.instrument_id == Instrument.id)
        .where(WatchlistEntry.enabled.is_(True))
        .where(Instrument.symbol.in_(BENCHMARK_SYMBOLS))
    )
    return (await session.execute(stmt)).scalars().all()
