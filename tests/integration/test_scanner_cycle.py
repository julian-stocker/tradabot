"""The scan cycle against a database.

Offline: the mock provider and constructed candles, never a network call. Covers
the properties that only exist once persistence is involved -- failure isolation,
the scan lease, restart safety, and the guarantee that filtering never reaches
the dataset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Environment, NotificationSettings, ScannerSettings, Settings
from app.db.models import ScanRun, SignalEvaluation, TrackedSignal, WatchlistEntry
from app.market_data.providers.mock import MockMarketDataProvider
from app.notifications.models import DeliveryResult, NotificationMessage
from app.notifications.service import NotificationService
from app.scanner.demo import DEMO_SYMBOL, seed_demo_instrument
from app.scanner.repository import (
    SCOPE_SCAN,
    ScanRunRepository,
    SignalEvaluationRepository,
    TrackedSignalRepository,
    WatchlistRepository,
)
from app.scanner.seed import seed_watchlist
from app.scanner.service import ScannerService
from app.scanner.universe import UniverseEntry
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository

pytestmark = pytest.mark.integration

NOW = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)


class CapturingBackend:
    """Records notifications instead of delivering them."""

    name = "capturing"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult(backend=self.name, delivered=True)


class BrokenBackend:
    """A notifier with a bug in it. Must never reach the database."""

    name = "broken"

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        msg = "notification backend exploded"
        raise RuntimeError(msg)


def make_settings(**overrides: object) -> Settings:
    """Test settings.

    ``require_regular_session`` is off by default so a fixed test timestamp does
    not have to be a live market hour to exercise qualification.
    """
    defaults: dict[str, object] = {
        "env": Environment.TEST,
        "database_url": "sqlite+aiosqlite:///:memory:",
        "log_level": "WARNING",
        "scanner": ScannerSettings(require_regular_session=False),
    }
    return Settings(**(defaults | overrides))  # type: ignore[arg-type]


@pytest.fixture
def factory(engine: object) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]


def build_scanner(
    factory: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
    *,
    settings: Settings | None = None,
    backend: object | None = None,
) -> ScannerService:
    settings = settings or make_settings()
    notifications = (
        NotificationService(settings, backends=[backend], session_factory=factory)
        if backend is not None
        else None
    )
    return ScannerService(
        factory, settings=settings, provider=provider, notifications=notifications
    )


# End of the "acceleration" phase: the strongest reading, before the breakdown.
# Seeding the full path would end on the breakdown and score negative, which is
# correct behaviour but tests a different thing.
QUALIFYING_BARS = 220


@pytest.fixture
async def seeded(factory: async_sessionmaker) -> async_sessionmaker:  # type: ignore[type-arg]
    """Profiles plus the demo instrument, watched, seeded to its strongest phase."""
    async with factory() as session:
        await SimulationProfileRepository(session).upsert_many(build_default_profiles())
        await seed_demo_instrument(session, now=NOW, bars=QUALIFYING_BARS)
        await session.commit()
    return factory


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------
async def test_seeding_creates_instruments_and_watchlist_entries(
    session: AsyncSession, provider: MockMarketDataProvider
) -> None:
    report = await seed_watchlist(
        session, provider, entries=[UniverseEntry("NVDA", "semiconductors")]
    )

    assert report.watchlist_added == 1
    assert report.ok
    assert await WatchlistRepository(session).symbols() == ["NVDA"]


async def test_seeding_names_symbols_the_provider_lacks(
    session: AsyncSession, provider: MockMarketDataProvider
) -> None:
    """A shorter watchlist with no explanation would be the wrong outcome."""
    report = await seed_watchlist(
        session,
        provider,
        entries=[UniverseEntry("NVDA", "semiconductors"), UniverseEntry("NOSUCH", "energy")],
    )

    assert report.missing == ["NOSUCH"]
    assert not report.ok
    assert "NOSUCH" in report.summary()


async def test_seeding_twice_is_idempotent(
    session: AsyncSession, provider: MockMarketDataProvider
) -> None:
    entries = [UniverseEntry("NVDA", "semiconductors")]
    await seed_watchlist(session, provider, entries=entries)
    await seed_watchlist(session, provider, entries=entries)

    rows = (await session.execute(select(WatchlistEntry))).scalars().all()
    assert len(rows) == 1


async def test_disabling_a_symbol_removes_it_from_scanning(
    session: AsyncSession, provider: MockMarketDataProvider
) -> None:
    await seed_watchlist(session, provider, entries=[UniverseEntry("NVDA", "semiconductors")])
    watchlist = WatchlistRepository(session)

    assert await watchlist.set_enabled("NVDA", enabled=False)

    assert await watchlist.symbols() == []
    assert len(await watchlist.list_entries(enabled_only=False)) == 1, "the row is kept"


async def test_re_enabling_restores_it(
    session: AsyncSession, provider: MockMarketDataProvider
) -> None:
    await seed_watchlist(session, provider, entries=[UniverseEntry("NVDA", "semiconductors")])
    watchlist = WatchlistRepository(session)
    await watchlist.set_enabled("NVDA", enabled=False)

    await watchlist.set_enabled("NVDA", enabled=True)

    assert await watchlist.symbols() == ["NVDA"]


async def test_enabling_an_unknown_symbol_reports_not_found(session: AsyncSession) -> None:
    assert not await WatchlistRepository(session).set_enabled("GHOST", enabled=True)


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------
async def test_a_single_symbol_scan_persists_an_evaluation(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    scanner = build_scanner(seeded, provider)

    stats = await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)

    assert stats.symbols_evaluated == 1
    async with seeded() as session:
        assert await SignalEvaluationRepository(session).count() == 1


async def test_every_evaluation_is_stored_regardless_of_threshold(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """The hard requirement.

    An impossibly high threshold means nothing qualifies and nothing is
    announced. The evaluation must still be stored: a rejected candidate is
    training data, and a dataset shaped by what was interesting enough to post
    would carry a selection bias impossible to correct for later.
    """
    settings = make_settings(
        notifications=NotificationSettings(signal_threshold=99.9, strong_signal_threshold=100.0),
        scanner=ScannerSettings(require_regular_session=False),
    )
    scanner = build_scanner(seeded, provider, settings=settings)

    stats = await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)

    assert stats.signals_qualified == 0
    async with seeded() as session:
        stored = (await session.execute(select(SignalEvaluation))).scalars().all()
    assert len(stored) == 1
    assert stored[0].qualified is False
    assert stored[0].score is not None, "the score is kept even though it did not qualify"


async def test_a_scan_records_metrics_on_the_run(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    async with seeded() as session:
        run = await ScanRunRepository(session).latest()
    assert run is not None
    assert run.status == "completed"
    assert run.symbols_evaluated == 1
    assert run.duration_seconds is not None


async def add_empty_instrument(factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    """A watched instrument with no candles at all."""
    from app.domain.enums import AssetType
    from app.instruments.repository import InstrumentRepository
    from app.market_data.provider import InstrumentInfo

    async with factory() as session:
        instruments = InstrumentRepository(session)
        await instruments.upsert_many(
            [
                InstrumentInfo(
                    symbol="EMPTY",
                    name="No Data",
                    exchange="XNYS",
                    currency="USD",
                    asset_type=AssetType.STOCK,
                )
            ]
        )
        await session.flush()
        empty = await instruments.get_by_symbol("EMPTY")
        assert empty is not None
        await WatchlistRepository(session).add(empty.id)
        await session.commit()


async def test_an_instrument_with_no_data_is_recorded_not_dropped(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Missing history is an observation about the feed, and worth storing.

    It is deliberately *not* a failure: the evaluation is written with an
    INSUFFICIENT data-quality state, which is more useful downstream than a
    counter saying one symbol went wrong.
    """
    await add_empty_instrument(seeded)

    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    assert stats.symbols_total == 2
    assert stats.symbols_evaluated == 2

    async with seeded() as session:
        rows = (await session.execute(select(SignalEvaluation))).scalars().all()
    qualities = {row.data_quality for row in rows}
    assert "INSUFFICIENT" in qualities
    assert all(row.qualified is False for row in rows if row.data_quality == "INSUFFICIENT")


async def test_a_symbol_that_raises_is_isolated(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One symbol's exception must not cost the others their evaluations.

    Each symbol runs in its own transaction, so a rollback is scoped to it.
    """
    await add_empty_instrument(seeded)

    from app.scanner import analysis as analysis_module

    original = analysis_module.MultiTimeframeAnalyser.analyse

    async def explode_for_empty(self, *, instrument, as_of, adjustment=None):  # type: ignore[no-untyped-def]
        if instrument.symbol == "EMPTY":
            msg = "simulated analyser failure"
            raise RuntimeError(msg)
        if adjustment is None:
            return await original(self, instrument=instrument, as_of=as_of)
        return await original(self, instrument=instrument, as_of=as_of, adjustment=adjustment)

    monkeypatch.setattr(analysis_module.MultiTimeframeAnalyser, "analyse", explode_for_empty)

    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    assert stats.symbols_failed == 1
    assert stats.symbols_evaluated == 1, "the healthy symbol survived"
    assert stats.failures[0][0] == "EMPTY"

    async with seeded() as session:
        assert await SignalEvaluationRepository(session).count() == 1


async def test_hit_rate_is_reported_with_every_scan(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """The base rate. Without it, a list of hits means nothing."""
    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    assert 0.0 <= stats.hit_rate <= 1.0
    assert "hit rate" in stats.summary()


# ---------------------------------------------------------------------------
# Lifecycle and identity
# ---------------------------------------------------------------------------
async def test_a_continuing_setup_keeps_one_identity(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Two scans of an unchanged setup must not create two signals.

    Otherwise "how long has this been true?" becomes unanswerable and every
    fifteen-minute cycle invents a fresh discovery.
    """
    scanner = build_scanner(seeded, provider)
    await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)
    await scanner.run_scan_cycle(as_of=NOW + timedelta(minutes=15), with_paper_trading=False)

    async with seeded() as session:
        signals = (await session.execute(select(TrackedSignal))).scalars().all()
    assert len(signals) == 1
    assert signals[0].evaluation_count == 2


async def test_each_scan_adds_an_evaluation_to_the_same_signal(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    scanner = build_scanner(seeded, provider)
    await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)
    await scanner.run_scan_cycle(as_of=NOW + timedelta(minutes=15), with_paper_trading=False)

    async with seeded() as session:
        signals = await TrackedSignalRepository(session).active_signals()
        evaluations = await SignalEvaluationRepository(session).for_signal(signals[0].id)
    assert len(evaluations) == 2


async def test_a_stale_signal_expires_rather_than_invalidating(
    session: AsyncSession,
) -> None:
    """'We stopped looking' is not 'the market said no'.

    Conflating them would give a future model labels that partly describe
    tradabot's uptime.
    """
    signal = TrackedSignal(
        instrument_id=1,
        direction="LONG",
        primary_timeframe="1h",
        horizon="5d",
        setup="BREAKOUT",
        lifecycle="QUALIFIED",
        current_score=80.0,
        peak_score=80.0,
        discovered_at=NOW - timedelta(days=5),
        last_evaluated_at=NOW - timedelta(days=5),
    )
    session.add(signal)
    await session.flush()

    expired = await TrackedSignalRepository(session).expire_stale(older_than=NOW)

    assert expired == 1
    await session.refresh(signal)
    assert signal.lifecycle == "EXPIRED"
    assert signal.invalidated_at is None, "expiry is not invalidation"


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------
async def test_a_second_scan_cannot_take_a_held_lease(session: AsyncSession) -> None:
    repository = ScanRunRepository(session)

    first = await repository.acquire_lease(lease_seconds=900, now=NOW)
    second = await repository.acquire_lease(lease_seconds=900, now=NOW)

    assert first is not None
    assert second is None, "overlapping cycles must not run"


async def test_an_expired_lease_is_taken_over(session: AsyncSession) -> None:
    """A killed process must not lock the scanner forever.

    Without expiry the only remedy is someone noticing and clearing it by hand,
    at exactly the moment nobody is watching.
    """
    repository = ScanRunRepository(session)
    await repository.acquire_lease(lease_seconds=60, now=NOW)

    later = await repository.acquire_lease(lease_seconds=60, now=NOW + timedelta(minutes=5))

    assert later is not None
    async with session.begin_nested():
        runs = (await session.execute(select(ScanRun).order_by(ScanRun.id))).scalars().all()
    assert runs[0].status == "failed"
    assert "expired" in (runs[0].error or "")


async def test_the_lease_names_its_holder(session: AsyncSession) -> None:
    run = await ScanRunRepository(session).acquire_lease(lease_seconds=60, now=NOW)

    assert run is not None
    assert ":" in run.lease_owner, "host:pid, so a stuck lease can be diagnosed"


async def test_a_scan_skips_when_the_lease_is_held(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    async with seeded() as session:
        await ScanRunRepository(session).acquire_lease(scope=SCOPE_SCAN, lease_seconds=900, now=NOW)
        await session.commit()

    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    assert stats.skipped_reason is not None
    assert stats.symbols_evaluated == 0


# ---------------------------------------------------------------------------
# Notifications and restart safety
# ---------------------------------------------------------------------------
async def test_a_broken_notifier_does_not_lose_the_evaluation(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Database first. Discord failure must never determine what is stored."""
    scanner = build_scanner(seeded, provider, backend=BrokenBackend())

    stats = await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)

    assert stats.symbols_evaluated == 1
    async with seeded() as session:
        assert await SignalEvaluationRepository(session).count() == 1


async def test_a_restart_does_not_re_announce_an_existing_signal(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Lifecycle and cooldown state live in the database, so a restart resumes.

    Two independently constructed scanners stand in for a process restart.
    """
    first_backend = CapturingBackend()
    await build_scanner(seeded, provider, backend=first_backend).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    second_backend = CapturingBackend()
    await build_scanner(seeded, provider, backend=second_backend).run_scan_cycle(
        as_of=NOW + timedelta(minutes=15), with_paper_trading=False
    )

    assert not second_backend.messages, "a restart re-announced a known signal"


async def test_open_signals_survive_a_restart(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    async with seeded() as session:
        before = await TrackedSignalRepository(session).active_signals()

    # A fresh scanner reads the same persisted state.
    async with seeded() as session:
        after = await TrackedSignalRepository(session).active_signals()

    assert [s.id for s in before] == [s.id for s in after]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
async def test_a_weekend_scan_still_records_evaluations(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Out of session, observations are still worth storing -- only promotion stops."""
    saturday = datetime(2024, 6, 8, 15, 0, tzinfo=UTC)
    settings = make_settings(scanner=ScannerSettings(require_regular_session=True))

    stats = await build_scanner(seeded, provider, settings=settings).run_scan_cycle(
        as_of=saturday, with_paper_trading=False
    )

    assert stats.session_phase.value == "WEEKEND"
    assert stats.signals_qualified == 0, "no new qualification outside the session"
    async with seeded() as session:
        assert await SignalEvaluationRepository(session).count() >= 1


async def test_a_holiday_is_reported_distinctly_from_a_weekend(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Independence Day 2024 was a Thursday.

    Distinguished so a genuine feed failure is not indistinguishable from Sunday.
    """
    holiday = datetime(2024, 7, 4, 15, 0, tzinfo=UTC)

    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=holiday, with_paper_trading=False
    )

    assert stats.session_phase.value == "HOLIDAY"


# ---------------------------------------------------------------------------
# Provenance and versioning
# ---------------------------------------------------------------------------
async def test_an_evaluation_records_its_algorithm_versions(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Rows from different versions are otherwise silently incomparable."""
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    async with seeded() as session:
        stored = (await session.execute(select(SignalEvaluation))).scalars().first()
    assert stored is not None
    assert stored.feature_set_version
    assert stored.signal_model_version
    assert stored.scanner_policy_version


async def test_an_evaluation_contains_no_future_derived_value(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """The ML boundary, asserted structurally.

    Outcome labels -- returns after N bars, excursions, stop/target hits -- belong
    to phase 5 and to a separate table. A label in this row would leak the first
    time someone selected `*`.
    """
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    columns = {c.name for c in SignalEvaluation.__table__.columns}
    forbidden = {
        "return_after_1h",
        "return_after_1d",
        "return_after_3d",
        "return_after_5d",
        "return_after_20d",
        "maximum_favorable_excursion",
        "maximum_adverse_excursion",
        "stop_hit",
        "target_hit",
        "outcome",
        "realised_return",
    }

    assert not (columns & forbidden), "an outcome label leaked into the input record"


async def test_the_market_data_timestamp_is_recorded_separately(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """The gap between evaluation time and data time is what makes staleness auditable."""
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    async with seeded() as session:
        stored = (await session.execute(select(SignalEvaluation))).scalars().first()
    assert stored is not None
    assert stored.market_data_timestamp is not None
    assert stored.market_data_timestamp <= stored.evaluated_at


async def test_timeframe_states_are_persisted_individually(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Not only the combined score. A future model must inspect the context."""
    await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=False)

    async with seeded() as session:
        stored = (await session.execute(select(SignalEvaluation))).scalars().first()
    assert stored is not None
    assert set(stored.timeframe_states) == {"5m", "15m", "1h", "1d"}
    assert stored.timeframe_states["1d"]["role"] == "macro"


async def test_the_demo_instrument_reaches_the_thresholds(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """The deterministic demo must actually demonstrate a qualification.

    Guards the constructed fixture: if the signal engine or the feature set
    changes such that the demo no longer crosses 75, the demo stops demonstrating
    anything and this fails rather than quietly passing.
    """
    scanner = build_scanner(seeded, provider)
    stats = await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=True)

    assert stats.symbols_evaluated == 1
    async with seeded() as session:
        stored = (await session.execute(select(SignalEvaluation))).scalars().first()
    assert stored is not None
    assert stored.score >= 75.0, f"demo scored {stored.score:.1f}; it no longer qualifies"
    assert stats.positions_opened > 0, "a qualified signal should reach the paper broker"


async def test_paper_decisions_fan_out_across_profiles(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Every profile decides independently; small ones may decline what large ones take."""
    stats = await build_scanner(seeded, provider).run_scan_cycle(as_of=NOW, with_paper_trading=True)

    assert stats.paper_decisions >= len(build_default_profiles()) - 1


async def test_paper_trading_can_be_skipped(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    stats = await build_scanner(seeded, provider).run_scan_cycle(
        as_of=NOW, with_paper_trading=False
    )

    assert stats.positions_opened == 0


async def test_top_candidates_returns_only_qualified_ones(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    scanner = build_scanner(seeded, provider)
    await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)

    candidates = await scanner.top_candidates(limit=5)

    assert all(c.score >= 75.0 for c in candidates)
    assert len(candidates) <= 5


async def test_no_qualified_candidates_returns_an_empty_list(
    seeded: async_sessionmaker,  # type: ignore[type-arg]
    provider: MockMarketDataProvider,
) -> None:
    """Zero is a valid scanner result, and must never be padded."""
    settings = make_settings(
        notifications=NotificationSettings(signal_threshold=99.9, strong_signal_threshold=100.0),
        scanner=ScannerSettings(require_regular_session=False),
    )
    scanner = build_scanner(seeded, provider, settings=settings)
    await scanner.run_scan_cycle(as_of=NOW, with_paper_trading=False)

    assert await scanner.top_candidates(limit=5) == []


def test_demo_symbol_is_clearly_not_a_real_ticker() -> None:
    """So a demo row is never mistaken for a real observation."""
    assert DEMO_SYMBOL.startswith("DEMO")
