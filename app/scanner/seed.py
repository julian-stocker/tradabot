"""Seeding the watchlist.

Turns the :mod:`app.scanner.universe` fixture into database rows. Idempotent, so
re-running is safe and is the normal way to pick up universe changes.

Instruments must exist before they can be watched. Rather than inventing them --
which would break the rule that nothing creates an instrument as a side effect --
the seeder asks the provider for its universe and reports symbols it could not
find, so a missing symbol is a visible message rather than a silently shorter
watchlist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import MarketDataProvider
from app.scanner.repository import WatchlistRepository
from app.scanner.universe import INITIAL_UNIVERSE, UniverseEntry

logger = get_logger(__name__)


@dataclass
class SeedReport:
    """What seeding did, and what it could not do."""

    requested: int = 0
    instruments_created: int = 0
    watchlist_added: int = 0
    missing: list[str] = field(default_factory=list)
    """Symbols the provider does not know. Named, never silently dropped."""

    @property
    def ok(self) -> bool:
        return not self.missing

    def summary(self) -> str:
        line = (
            f"requested {self.requested}, instruments {self.instruments_created}, "
            f"watchlist {self.watchlist_added}"
        )
        if self.missing:
            line += f", missing {len(self.missing)}: {', '.join(self.missing[:8])}"
            if len(self.missing) > 8:  # noqa: PLR2004 -- display cap
                line += " …"
        return line


async def seed_watchlist(
    session: AsyncSession,
    provider: MarketDataProvider,
    *,
    entries: Sequence[UniverseEntry] = INITIAL_UNIVERSE,
) -> SeedReport:
    """Ensure the universe exists as instruments and is on the watchlist.

    Args:
        session: caller owns the transaction.
        provider: consulted for instrument metadata.
        entries: what to seed. Defaults to the initial development universe.
    """
    report = SeedReport(requested=len(entries))
    wanted = {entry.symbol.upper(): entry for entry in entries}

    infos = [info for info in await provider.get_instruments() if info.symbol.upper() in wanted]
    instruments = InstrumentRepository(session)
    if infos:
        report.instruments_created = await instruments.upsert_many(infos, provider=provider.name)
        await session.flush()

    watchlist = WatchlistRepository(session)
    for symbol, entry in sorted(wanted.items()):
        instrument = await instruments.get_by_symbol(symbol)
        if instrument is None:
            report.missing.append(symbol)
            continue
        await watchlist.add(instrument.id, priority=entry.priority, tags=entry.tags)
        report.watchlist_added += 1

    if report.missing:
        # A provider whose universe is a configured watchlist (Alpaca) will not
        # know a symbol until it is added there. Naming them is the fix; a
        # shorter watchlist with no explanation is not.
        logger.warning(
            "symbols not available from provider",
            provider=provider.name,
            count=len(report.missing),
            symbols=",".join(report.missing[:20]),
        )

    logger.info("watchlist seeded", **{"summary": report.summary()})
    return report
