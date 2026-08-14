"""Capturing and persisting the daily option surface.

Phase 10 built the derivation (``app.market_data.options``) and the tables, but
nothing that fetched, persisted or scheduled. This is that missing half.

The capture window, and why it is not the close
-----------------------------------------------
One capture per regular session, taken **30 minutes before the close**. Late
enough that the session's information is in the surface; early enough to avoid
the closing auction, where quotes widen and the indicative feed is least
representative. Depending on an exact 16:00 print would also make every capture
hostage to a job that ran two minutes late.

Idempotency is by trading day, not by timestamp
-----------------------------------------------
A capture is identified by ``(instrument, capture date)``. The job runs on a
short interval and decides for itself whether today is already done, so a
retry, a machine that was asleep, or a launchd catch-up after downtime all
converge on exactly one snapshot per symbol per session rather than duplicating.
That is why the window is a *range* rather than an instant.

Failure is per symbol
---------------------
One ticker's provider error must not abandon the other 51, and a bad capture
must not poison the surface table. Symbols are fetched and persisted one at a
time, and the run reports what failed rather than raising.

This module cannot place an order. It imports no trading client, and the only
Alpaca surface it touches is the historical option data client, which has no
order methods on it at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redaction import safe_message
from app.core.time import utc_now
from app.db.models import Instrument, OptionQuoteSnapshot, OptionSurfaceSnapshot
from app.market_data.options import (
    ContractQuote,
    SurfaceSummary,
    as_decimal,
    canonical_slice,
    parse_contract,
    summarise,
)

logger = get_logger(__name__)

CAPTURE_WINDOW_START: Final = time(19, 30, tzinfo=UTC)
"""15:30 US Eastern during daylight time -- 30 minutes before the close.

Stored in UTC because the job's clock is UTC. The window is deliberately wide
rather than an instant; see :data:`CAPTURE_WINDOW_END`.
"""

CAPTURE_WINDOW_END: Final = time(21, 0, tzinfo=UTC)
"""End of the window, comfortably after the close.

An hour and a half wide so a late run, a slow provider or a machine that woke up
at 20:15 still captures the session. Because idempotency is keyed on the trading
date, a wide window cannot produce two snapshots.
"""

MAX_IV: Final = 5.0
"""Implied volatilities above 500% are rejected as impossible.

Not clamped -- rejected. A 600% IV on the indicative feed is a broken quote, and
storing it clamped to 5.0 would launder a data error into a plausible number.
"""

MIN_IV: Final = 0.001

MAX_UNDERLYING_AGE: Final = timedelta(hours=2)
"""How stale the underlying trade may be before the capture is refused.

The whole surface is quoted relative to spot. A stale underlying silently
shifts every moneyness and delta calculation, so it fails the symbol rather
than producing a surface that looks fine and is centred on the wrong price.
"""


@dataclass(frozen=True, slots=True)
class QualityFlags:
    """What was wrong with one symbol's chain. All counts, never corrections."""

    contracts: int = 0
    missing_iv: int = 0
    missing_greeks: int = 0
    missing_quote: int = 0
    one_sided: int = 0
    impossible_iv: int = 0
    duplicate_symbols: int = 0
    bad_expiry_or_strike: int = 0

    @property
    def iv_missing_rate(self) -> float:
        return self.missing_iv / self.contracts if self.contracts else 0.0

    def describe(self) -> str:
        return (
            f"contracts={self.contracts} missing_iv={self.missing_iv} "
            f"missing_greeks={self.missing_greeks} one_sided={self.one_sided} "
            f"impossible_iv={self.impossible_iv} dupes={self.duplicate_symbols} "
            f"bad_expiry_or_strike={self.bad_expiry_or_strike}"
        )


@dataclass
class CaptureRun:
    """What one capture cycle did."""

    captured_at: datetime
    capture_date: date
    session: str
    skipped_reason: str | None = None
    symbols_requested: int = 0
    symbols_captured: int = 0
    symbols_skipped_existing: int = 0
    symbols_without_iv: int = 0
    contracts_scanned: int = 0
    contracts_stored: int = 0
    summaries_stored: int = 0
    duration_seconds: float = 0.0
    failures: list[tuple[str, str]] = field(default_factory=list)
    quality: QualityFlags = field(default_factory=QualityFlags)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"skipped ({self.skipped_reason})"
        return (
            f"{self.symbols_captured}/{self.symbols_requested} symbols, "
            f"{self.contracts_scanned:,} scanned, {self.contracts_stored:,} stored, "
            f"{self.summaries_stored} summaries, {len(self.failures)} failed, "
            f"{self.duration_seconds:.1f}s"
        )


def within_capture_window(moment: datetime) -> bool:
    """Whether ``moment`` falls inside the daily capture window."""
    return CAPTURE_WINDOW_START <= moment.timetz() <= CAPTURE_WINDOW_END


def inspect_quality(
    contracts: list[ContractQuote], raw_count: int, seen_symbols: set[str]
) -> QualityFlags:
    """Count what is wrong. **Counts only** -- nothing here repairs anything."""
    missing_iv = sum(1 for c in contracts if c.implied_volatility is None)
    missing_greeks = sum(1 for c in contracts if c.delta is None)
    missing_quote = sum(1 for c in contracts if c.bid is None and c.ask is None)
    one_sided = sum(1 for c in contracts if (c.bid is None) != (c.ask is None))
    impossible = sum(
        1
        for c in contracts
        if c.implied_volatility is not None and not (MIN_IV <= c.implied_volatility <= MAX_IV)
    )
    bad = sum(1 for c in contracts if c.strike <= 0)
    return QualityFlags(
        contracts=raw_count,
        missing_iv=missing_iv,
        missing_greeks=missing_greeks,
        missing_quote=missing_quote,
        one_sided=one_sided,
        impossible_iv=impossible,
        duplicate_symbols=raw_count - len(seen_symbols),
        bad_expiry_or_strike=bad,
    )


def usable(contract: ContractQuote) -> bool:
    """Whether a contract may enter the stored slice.

    An impossible implied volatility disqualifies the contract rather than being
    corrected: the surface is a record of what the feed said, minus what it
    cannot have meant.
    """
    if contract.strike <= 0:
        return False
    iv = contract.implied_volatility
    return iv is None or MIN_IV <= iv <= MAX_IV


class OptionSnapshotService:
    """Fetches option chains and writes point-in-time snapshots. **No orders.**"""

    def __init__(self, session: AsyncSession, provider: Any) -> None:
        self._session = session
        self._provider = provider

    async def already_captured(self, instrument_id: int, capture_date: date) -> bool:
        """Whether this instrument already has a snapshot for this trading day."""
        start = datetime.combine(capture_date, time(0, 0), tzinfo=UTC)
        stmt = (
            select(OptionSurfaceSnapshot.id)
            .where(OptionSurfaceSnapshot.instrument_id == instrument_id)
            .where(OptionSurfaceSnapshot.captured_at >= start)
            .where(OptionSurfaceSnapshot.captured_at < start + timedelta(days=1))
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def capture_symbol(
        self,
        instrument: Instrument,
        *,
        captured_at: datetime,
        persist: bool = True,
    ) -> tuple[SurfaceSummary | None, QualityFlags, int]:
        """Capture one symbol. Returns ``(summary, quality, stored_rows)``."""
        spot, quoted_at = await self._provider.get_underlying_price(instrument.symbol)
        stale = quoted_at is not None and captured_at - quoted_at > MAX_UNDERLYING_AGE
        if stale and persist:
            # Refused only when it would be written. A stale spot silently
            # shifts every moneyness and delta in the surface, so it must never
            # reach the table -- but a dry run writes nothing, and refusing
            # there would make the pipeline unverifiable outside market hours,
            # which is precisely when someone checks it.
            hours = (captured_at - quoted_at).total_seconds() / 3600 if quoted_at else 0.0
            msg = f"underlying trade for {instrument.symbol} is {hours:.1f}h old"
            raise ValueError(msg)
        if stale:
            logger.info(
                "dry run against a stale underlying",
                symbol=instrument.symbol,
                quoted_at=str(quoted_at),
            )

        chain = await self._provider.get_option_chain(instrument.symbol)
        parsed = [c for c in (parse_contract(occ, snap) for occ, snap in chain.items()) if c]
        quality = inspect_quality(parsed, len(chain), set(chain))

        sliced = canonical_slice(parsed, spot=spot, as_of=captured_at.date())
        keep = [c for c in sliced if usable(c)]
        summary = summarise(keep, spot=spot, as_of=captured_at.date())

        if not persist:
            return summary, quality, len(keep)

        # Idempotent by construction: clear anything already written for this
        # instrument and capture instant before inserting. A retry mid-write
        # therefore converges rather than accumulating partial slices.
        day_start = datetime.combine(captured_at.date(), time(0, 0), tzinfo=UTC)
        for table in (OptionQuoteSnapshot, OptionSurfaceSnapshot):
            await self._session.execute(
                delete(table)
                .where(table.instrument_id == instrument.id)
                .where(table.captured_at >= day_start)
                .where(table.captured_at < day_start + timedelta(days=1))
            )

        self._session.add(
            OptionSurfaceSnapshot(
                instrument_id=instrument.id,
                captured_at=captured_at,
                underlying_price=as_decimal(spot),
                atm_iv=as_decimal(summary.atm_iv),
                iv_30d=as_decimal(summary.iv_30d),
                skew_25d=as_decimal(summary.skew_25d),
                term_slope=as_decimal(summary.term_slope),
                expected_move_pct=as_decimal(summary.expected_move_pct),
                contracts_seen=summary.contracts_seen,
                contracts_with_iv=summary.contracts_with_iv,
                feed=self._provider.options_feed,
                provider=self._provider.name,
            )
        )
        for contract in keep:
            self._session.add(
                OptionQuoteSnapshot(
                    instrument_id=instrument.id,
                    captured_at=captured_at,
                    occ_symbol=contract.occ_symbol,
                    expiration=datetime.combine(contract.expiration, time(0, 0), tzinfo=UTC),
                    strike=as_decimal(contract.strike),
                    option_type=contract.option_type,
                    bid=as_decimal(contract.bid),
                    ask=as_decimal(contract.ask),
                    mid=as_decimal(contract.mid),
                    implied_volatility=as_decimal(contract.implied_volatility),
                    delta=as_decimal(contract.delta),
                    gamma=as_decimal(contract.gamma),
                    vega=as_decimal(contract.vega),
                    theta=as_decimal(contract.theta),
                    open_interest=contract.open_interest,
                    feed=self._provider.options_feed,
                )
            )
        await self._session.flush()
        return summary, quality, len(keep)

    async def capture(
        self,
        symbols: list[str],
        *,
        now: datetime | None = None,
        persist: bool = True,
        force: bool = False,
    ) -> CaptureRun:
        """Capture every symbol that is not already done for this trading day."""
        moment = now or utc_now()
        started = moment
        run = CaptureRun(
            captured_at=moment,
            capture_date=moment.date(),
            session="REGULAR",
            symbols_requested=len(symbols),
        )

        found = await self._session.execute(
            select(Instrument).where(Instrument.symbol.in_(symbols))
        )
        instruments = {i.symbol: i for i in found.scalars().all()}

        totals = QualityFlags()
        for symbol in symbols:
            instrument = instruments.get(symbol)
            if instrument is None:
                run.failures.append((symbol, "not in the instrument table"))
                continue
            if persist and not force and await self.already_captured(instrument.id, moment.date()):
                run.symbols_skipped_existing += 1
                continue
            try:
                summary, quality, stored = await self.capture_symbol(
                    instrument, captured_at=moment, persist=persist
                )
            except Exception as exc:
                run.failures.append((symbol, safe_message(exc)))
                logger.warning("option capture failed", symbol=symbol, error=safe_message(exc))
                continue

            run.symbols_captured += 1
            run.contracts_scanned += quality.contracts
            run.contracts_stored += stored
            run.summaries_stored += 1 if persist else 0
            if summary is None or summary.contracts_with_iv == 0:
                run.symbols_without_iv += 1
            totals = QualityFlags(
                contracts=totals.contracts + quality.contracts,
                missing_iv=totals.missing_iv + quality.missing_iv,
                missing_greeks=totals.missing_greeks + quality.missing_greeks,
                missing_quote=totals.missing_quote + quality.missing_quote,
                one_sided=totals.one_sided + quality.one_sided,
                impossible_iv=totals.impossible_iv + quality.impossible_iv,
                duplicate_symbols=totals.duplicate_symbols + quality.duplicate_symbols,
                bad_expiry_or_strike=totals.bad_expiry_or_strike + quality.bad_expiry_or_strike,
            )

        run.quality = totals
        run.duration_seconds = (utc_now() - started).total_seconds()
        logger.info("option surface capture", outcome=run.summary(), quality=totals.describe())
        return run
