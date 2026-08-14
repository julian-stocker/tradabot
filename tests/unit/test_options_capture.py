"""The option collector must capture once per session, and never invent one.

The failure modes worth testing here are operational rather than mathematical.
The derivation is covered in ``test_option_surface.py``; what these assert is
that the job cannot produce two snapshots for one day, cannot capture when the
market is shut, cannot let one bad symbol end the run, and cannot reach an order
path.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.enums import AssetType
from app.market_data import options_service
from app.market_data.options import ContractQuote
from app.market_data.options_service import (
    CAPTURE_WINDOW_END,
    CAPTURE_WINDOW_START,
    MAX_IV,
    MAX_UNDERLYING_AGE,
    MIN_IV,
    OptionSnapshotService,
    inspect_quality,
    usable,
    within_capture_window,
)
from app.ops.launchd import scheduled_jobs

WINDOW_DAY = datetime(2026, 8, 14, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return WINDOW_DAY.replace(hour=hour, minute=minute)


def quote(
    *,
    iv: float | None = 0.25,
    delta: float | None = 0.5,
    bid: float | None = 1.0,
    ask: float | None = 1.2,
    strike: float = 100.0,
) -> ContractQuote:
    return ContractQuote(
        occ_symbol=f"TEST260918C{int(strike * 1000):08d}",
        expiration=datetime(2026, 9, 18, tzinfo=UTC).date(),
        strike=strike,
        option_type="C",
        bid=bid,
        ask=ask,
        implied_volatility=iv,
        delta=delta,
        gamma=0.01,
        vega=0.1,
        theta=-0.02,
        open_interest=None,
    )


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------
def test_the_capture_window_is_late_session_not_the_close() -> None:
    """30 minutes before the close, so the auction cannot distort the surface."""
    assert CAPTURE_WINDOW_START.hour == 19
    assert CAPTURE_WINDOW_START.minute == 30
    assert CAPTURE_WINDOW_END > CAPTURE_WINDOW_START


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (at(13, 45), False),  # just after the open
        (at(19, 29), False),
        (at(19, 30), True),
        (at(20, 15), True),  # a late run still captures
        (at(21, 0), True),
        (at(21, 1), False),
    ],
)
def test_only_the_window_captures(moment: datetime, expected: bool) -> None:
    assert within_capture_window(moment) is expected


def test_the_window_is_wide_enough_to_survive_a_late_run() -> None:
    """Narrow windows turn a slow provider into a permanently missing day."""
    start = datetime.combine(WINDOW_DAY.date(), CAPTURE_WINDOW_START)
    end = datetime.combine(WINDOW_DAY.date(), CAPTURE_WINDOW_END)
    assert end - start >= timedelta(hours=1)


def test_the_collector_is_a_scheduled_job_on_a_short_interval() -> None:
    """Short interval plus in-app guards, matching how scan and trends work."""
    jobs = {j.name: j for j in scheduled_jobs()}
    assert "options" in jobs
    assert jobs["options"].args == ("options", "capture")
    assert jobs["options"].interval_seconds <= 30 * 60


# ---------------------------------------------------------------------------
# Quality: counted, never corrected
# ---------------------------------------------------------------------------
def test_quality_counts_every_defect_class() -> None:
    contracts = [
        quote(),
        quote(iv=None, delta=None),
        quote(bid=None),  # one-sided
        quote(iv=99.0),  # impossible
        quote(strike=-5.0),  # bad strike
    ]
    flags = inspect_quality(contracts, raw_count=6, seen_symbols={"a", "b", "c", "d", "e"})

    assert flags.contracts == 6
    assert flags.missing_iv == 1
    assert flags.missing_greeks == 1
    assert flags.one_sided == 1
    assert flags.impossible_iv == 1
    assert flags.bad_expiry_or_strike == 1
    assert flags.duplicate_symbols == 1


def test_an_impossible_iv_is_rejected_not_clamped() -> None:
    """Clamping would launder a broken quote into a plausible number."""
    assert not usable(quote(iv=MAX_IV + 1))
    assert not usable(quote(iv=MIN_IV / 2))
    assert usable(quote(iv=0.25))


def test_a_contract_with_no_iv_is_still_usable() -> None:
    """Missing is not impossible: 21.5% of the feed has no IV and that is data."""
    assert usable(quote(iv=None))


def test_missing_values_are_never_filled_in() -> None:
    """The whole module must contain no default-value substitution for IV."""
    source = inspect.getsource(options_service)
    assert "implied_volatility or 0" not in source
    assert "iv or 0" not in source


# ---------------------------------------------------------------------------
# Capture behaviour
# ---------------------------------------------------------------------------
@dataclass
class StubProvider:
    """Minimal provider double. Has no order method, by construction."""

    name: str = "stub"
    options_feed: str = "indicative"
    chain: dict | None = None
    spot: float = 100.0
    quoted_at: datetime | None = None
    fail_for: str | None = None

    async def get_underlying_price(self, symbol: str) -> tuple[float, datetime | None]:
        return self.spot, self.quoted_at

    async def get_option_chain(self, symbol: str) -> dict:
        if symbol == self.fail_for:
            msg = f"provider exploded for {symbol}"
            raise RuntimeError(msg)
        return self.chain or {}


@pytest.mark.asyncio
async def test_a_stale_underlying_fails_the_symbol(session) -> None:
    """Every moneyness is quoted against spot; a stale one shifts the surface.

    Enforced on the persisting path only. A dry run writes nothing, so refusing
    there would make the pipeline unverifiable outside market hours -- which is
    exactly when an operator checks it.
    """
    from app.db.models import Instrument

    instrument = Instrument(
        symbol="TEST",
        name="Test",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()

    provider = StubProvider(quoted_at=at(19, 30) - MAX_UNDERLYING_AGE - timedelta(minutes=1))
    service = OptionSnapshotService(session, provider)

    with pytest.raises(ValueError, match="old"):
        await service.capture_symbol(instrument, captured_at=at(19, 30), persist=True)

    # ...and the same stale spot is allowed through a dry run, which stores nothing.
    summary, _, _ = await service.capture_symbol(instrument, captured_at=at(19, 30), persist=False)
    assert summary is not None


@pytest.mark.asyncio
async def test_one_symbols_failure_does_not_end_the_run(session) -> None:
    from app.db.models import Instrument

    for symbol in ("GOOD", "BAD"):
        session.add(
            Instrument(
                symbol=symbol,
                name=symbol,
                exchange="XNAS",
                currency="USD",
                asset_type=AssetType.STOCK,
                is_active=True,
            )
        )
    await session.flush()

    service = OptionSnapshotService(session, StubProvider(fail_for="BAD"))
    run = await service.capture(["BAD", "GOOD"], now=at(19, 30), persist=False)

    assert [s for s, _ in run.failures] == ["BAD"]
    assert run.symbols_captured == 1


@pytest.mark.asyncio
async def test_a_second_capture_the_same_day_is_skipped(session) -> None:
    """**The idempotency gate.** A retry must not produce two snapshots."""
    from app.db.models import Instrument

    instrument = Instrument(
        symbol="TEST",
        name="Test",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()

    service = OptionSnapshotService(session, StubProvider())
    first = await service.capture(["TEST"], now=at(19, 30), persist=True)
    second = await service.capture(["TEST"], now=at(20, 15), persist=True)

    assert first.summaries_stored == 1
    assert second.summaries_stored == 0
    assert second.symbols_skipped_existing == 1


@pytest.mark.asyncio
async def test_forcing_a_recapture_replaces_rather_than_duplicates(session) -> None:
    from sqlalchemy import func, select

    from app.db.models import Instrument, OptionSurfaceSnapshot

    instrument = Instrument(
        symbol="TEST",
        name="Test",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()

    service = OptionSnapshotService(session, StubProvider())
    await service.capture(["TEST"], now=at(19, 30), persist=True)
    await service.capture(["TEST"], now=at(20, 30), persist=True, force=True)

    total = await session.execute(select(func.count()).select_from(OptionSurfaceSnapshot))
    assert total.scalar_one() == 1


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(session) -> None:
    """The safe path outside a session: prove the pipeline, fabricate no snapshot."""
    from sqlalchemy import func, select

    from app.db.models import Instrument, OptionSurfaceSnapshot

    session.add(
        Instrument(
            symbol="TEST",
            name="Test",
            exchange="XNAS",
            currency="USD",
            asset_type=AssetType.STOCK,
            is_active=True,
        )
    )
    await session.flush()

    service = OptionSnapshotService(session, StubProvider())
    run = await service.capture(["TEST"], now=at(19, 30), persist=False)

    total = await session.execute(select(func.count()).select_from(OptionSurfaceSnapshot))
    assert total.scalar_one() == 0
    assert run.summaries_stored == 0


@pytest.mark.asyncio
async def test_an_unknown_symbol_is_reported_not_invented(session) -> None:
    service = OptionSnapshotService(session, StubProvider())
    run = await service.capture(["NOPE"], now=at(19, 30), persist=False)
    assert run.failures == [("NOPE", "not in the instrument table")]


# ---------------------------------------------------------------------------
# No order path
# ---------------------------------------------------------------------------
def test_the_collector_cannot_place_an_order() -> None:
    """**The safety gate.** No trading client is reachable from this module."""
    source = inspect.getsource(options_service)
    for forbidden in ("TradingClient", "submit_order", "OrderRequest", "alpaca.trading"):
        assert forbidden not in source
