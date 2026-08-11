"""Instrument lifecycle, historical universe, and corporate-action persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.corporate_actions.models import CorporateAction
from app.corporate_actions.repository import CorporateActionRepository
from app.db.models import Instrument
from app.domain.enums import AssetType, CorporateActionType
from app.instruments.repository import InstrumentRepository
from app.instruments.universe import UniverseService
from app.market_data.provider import InstrumentInfo

LISTED = datetime(2015, 1, 1, tzinfo=UTC)
DELISTED = datetime(2020, 6, 1, tzinfo=UTC)
BEFORE = datetime(2010, 1, 1, tzinfo=UTC)
DURING = datetime(2018, 1, 1, tzinfo=UTC)
AFTER = datetime(2023, 1, 1, tzinfo=UTC)


async def seed(session, *infos: InstrumentInfo) -> None:
    await InstrumentRepository(session).upsert_many(list(infos))
    await session.flush()


def info(
    symbol: str,
    *,
    listed_at: datetime | None = None,
    delisted_at: datetime | None = None,
    exchange: str = "XNAS",
    asset_type: AssetType = AssetType.STOCK,
) -> InstrumentInfo:
    return InstrumentInfo(
        symbol=symbol,
        name=f"{symbol} Inc.",
        exchange=exchange,
        currency="USD",
        asset_type=asset_type,
        listed_at=listed_at,
        delisted_at=delisted_at,
    )


class TestLifecyclePersistence:
    async def test_dates_round_trip(self, session):
        await seed(session, info("ACME", listed_at=LISTED, delisted_at=DELISTED))
        stored = await InstrumentRepository(session).get_by_symbol("ACME")
        assert stored is not None
        assert stored.listed_at == LISTED
        assert stored.delisted_at == DELISTED

    async def test_unknown_lifecycle_stored_as_null(self, session):
        """NULL is the honest representation of "we were never told"."""
        await seed(session, info("NODATE"))
        stored = await InstrumentRepository(session).get_by_symbol("NODATE")
        assert stored is not None
        assert stored.listed_at is None
        assert stored.delisted_at is None

    async def test_delisted_instrument_is_marked_inactive(self, session):
        await seed(session, info("DEAD", listed_at=LISTED, delisted_at=DELISTED))
        stored = await InstrumentRepository(session).get_by_symbol("DEAD")
        assert stored is not None
        assert stored.is_active is False

    async def test_delisted_rows_are_retained(self, session):
        """Deleting them is exactly how survivorship bias enters a backtest."""
        await seed(
            session,
            info("LIVE", listed_at=LISTED),
            info("DEAD", listed_at=LISTED, delisted_at=DELISTED),
        )
        repo = InstrumentRepository(session)
        assert len(await repo.list_all(active_only=True)) == 1
        assert len(await repo.list_all(active_only=False)) == 2

    async def test_inverted_lifecycle_rejected_by_the_database(self, session):
        """A CHECK constraint backs up the DTO validation."""
        session.add(
            Instrument(
                symbol="BAD",
                name="Bad",
                exchange="XNAS",
                currency="USD",
                asset_type=AssetType.STOCK,
                listed_at=DELISTED,
                delisted_at=LISTED,
            )
        )
        with pytest.raises(Exception, match="lifecycle_ordered"):
            await session.flush()


class TestIsTradableAt:
    @pytest.fixture
    async def instrument(self, session) -> Instrument:
        await seed(session, info("ACME", listed_at=LISTED, delisted_at=DELISTED))
        stored = await InstrumentRepository(session).get_by_symbol("ACME")
        assert stored is not None
        return stored

    async def test_before_listing(self, instrument):
        assert not instrument.is_tradable_at(BEFORE)

    async def test_during_listing(self, instrument):
        assert instrument.is_tradable_at(DURING)

    async def test_after_delisting(self, instrument):
        assert not instrument.is_tradable_at(AFTER)

    async def test_listing_boundary_is_inclusive(self, instrument):
        assert instrument.is_tradable_at(LISTED)
        assert not instrument.is_tradable_at(LISTED - timedelta(microseconds=1))

    async def test_delisting_boundary_is_exclusive(self, instrument):
        """Half-open [listed_at, delisted_at): it did not trade *at* delisting."""
        assert instrument.is_tradable_at(DELISTED - timedelta(microseconds=1))
        assert not instrument.is_tradable_at(DELISTED)

    async def test_null_bounds_are_unbounded(self, session):
        await seed(session, info("ALWAYS"))
        stored = await InstrumentRepository(session).get_by_symbol("ALWAYS")
        assert stored is not None
        assert stored.is_tradable_at(BEFORE)
        assert stored.is_tradable_at(AFTER)

    async def test_is_active_flag_does_not_answer_historical_questions(self, session):
        """The whole reason both exist.

        A delisted instrument has is_active=False today and was perfectly
        tradable in 2018. Using the flag for a historical query is the bug this
        design prevents.
        """
        await seed(session, info("DEAD", listed_at=LISTED, delisted_at=DELISTED))
        stored = await InstrumentRepository(session).get_by_symbol("DEAD")
        assert stored is not None
        assert stored.is_active is False
        assert stored.is_tradable_at(DURING) is True


class TestUniverseQueries:
    @pytest.fixture
    async def universe(self, session) -> UniverseService:
        await seed(
            session,
            info("OLD", listed_at=LISTED, delisted_at=DELISTED),
            info("CURRENT", listed_at=LISTED),
            info("LATE", listed_at=datetime(2022, 1, 1, tzinfo=UTC)),
            info("EUROPE", listed_at=LISTED, exchange="XETR"),
            info("FUND", listed_at=LISTED, asset_type=AssetType.ETF),
        )
        return UniverseService(session)

    async def test_tradable_at_excludes_not_yet_listed(self, universe):
        symbols = {i.symbol for i in await universe.tradable_at(DURING)}
        assert "LATE" not in symbols
        assert "CURRENT" in symbols

    async def test_tradable_at_excludes_already_delisted(self, universe):
        symbols = {i.symbol for i in await universe.tradable_at(AFTER)}
        assert "OLD" not in symbols
        assert "CURRENT" in symbols

    async def test_tradable_at_includes_the_delisted_while_they_lived(self, universe):
        """The point of the whole module."""
        symbols = {i.symbol for i in await universe.tradable_at(DURING)}
        assert "OLD" in symbols

    async def test_universe_shrinks_and_grows_over_time(self, universe):
        early = {i.symbol for i in await universe.tradable_at(DURING)}
        late = {i.symbol for i in await universe.tradable_at(datetime(2023, 6, 1, tzinfo=UTC))}
        assert "OLD" in early
        assert "OLD" not in late
        assert "LATE" not in early
        assert "LATE" in late

    async def test_active_between_uses_overlap_not_containment(self, universe):
        """An instrument delisted mid-window belongs in that window's backtest."""
        symbols = {
            i.symbol
            for i in await universe.active_between(
                datetime(2019, 1, 1, tzinfo=UTC), datetime(2023, 1, 1, tzinfo=UTC)
            )
        }
        assert "OLD" in symbols, "delisted mid-window, so it was tradable for part of it"
        assert "LATE" in symbols, "listed mid-window"

    async def test_active_between_excludes_the_wholly_outside(self, universe):
        symbols = {
            i.symbol
            for i in await universe.active_between(
                datetime(2021, 1, 1, tzinfo=UTC), datetime(2021, 6, 1, tzinfo=UTC)
            )
        }
        assert "OLD" not in symbols, "delisted before the window opened"
        assert "LATE" not in symbols, "listed after the window closed"

    async def test_active_between_rejects_an_inverted_window(self, universe):
        with pytest.raises(ValueError, match="must be before"):
            await universe.active_between(AFTER, DURING)

    async def test_filters_apply(self, universe):
        assert {i.symbol for i in await universe.tradable_at(DURING, exchange="XETR")} == {"EUROPE"}
        assert {i.symbol for i in await universe.tradable_at(DURING, asset_type=AssetType.ETF)} == {
            "FUND"
        }

    async def test_was_tradable_for_one_symbol(self, universe):
        assert await universe.was_tradable("OLD", DURING)
        assert not await universe.was_tradable("OLD", AFTER)

    async def test_unknown_symbol_was_not_tradable(self, universe):
        assert not await universe.was_tradable("NOPE", DURING)

    async def test_symbols_helper_matches_the_full_query(self, universe):
        symbols = await universe.symbols_tradable_at(DURING)
        rows = await universe.tradable_at(DURING)
        assert set(symbols) == {i.symbol for i in rows}


class TestCorporateActionPersistence:
    async def make_instrument(self, session) -> Instrument:
        await seed(session, info("ACME", listed_at=LISTED))
        stored = await InstrumentRepository(session).get_by_symbol("ACME")
        assert stored is not None
        return stored

    def split(self, day: str) -> CorporateAction:
        return CorporateAction(
            symbol="ACME",
            action_type=CorporateActionType.SPLIT,
            effective_at=datetime.fromisoformat(day).replace(tzinfo=UTC),
            from_shares=Decimal(1),
            to_shares=Decimal(2),
            source="test",
        )

    def dividend(self, day: str, amount: str = "0.50") -> CorporateAction:
        return CorporateAction(
            symbol="ACME",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=datetime.fromisoformat(day).replace(tzinfo=UTC),
            payment_at=datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(days=7),
            cash_amount=Decimal(amount),
            currency="USD",
            source="test",
        )

    async def test_split_round_trips(self, session):
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        await repo.upsert_many(instrument_id=instrument.id, actions=[self.split("2019-05-01")])
        await session.flush()

        actions = await repo.list_for_instrument(
            instrument_id=instrument.id, symbol=instrument.symbol
        )
        assert len(actions) == 1
        assert actions[0].split_ratio == Decimal(2)
        assert actions[0].action_type is CorporateActionType.SPLIT

    async def test_dividend_round_trips_with_money_intact(self, session):
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id, actions=[self.dividend("2019-05-01", "0.235")]
        )
        await session.flush()

        actions = await repo.list_for_instrument(
            instrument_id=instrument.id, symbol=instrument.symbol
        )
        assert actions[0].cash_amount == Decimal("0.235")
        assert actions[0].currency == "USD"
        assert actions[0].payment_at is not None

    async def test_upsert_is_idempotent(self, session):
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        for _ in range(3):
            await repo.upsert_many(instrument_id=instrument.id, actions=[self.split("2019-05-01")])
        await session.flush()
        assert await repo.count_for_instrument(instrument.id) == 1

    async def test_results_are_chronological(self, session):
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            actions=[
                self.split("2021-01-01"),
                self.dividend("2019-01-01"),
                self.split("2020-01-01"),
            ],
        )
        await session.flush()

        actions = await repo.list_for_instrument(
            instrument_id=instrument.id, symbol=instrument.symbol
        )
        stamps = [a.effective_at for a in actions]
        assert stamps == sorted(stamps)

    async def test_known_as_of_hides_later_actions(self, session):
        """Point-in-time reconstruction must not use a split that had not happened."""
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            actions=[self.split("2019-01-01"), self.split("2021-01-01")],
        )
        await session.flush()

        known = await repo.list_for_instrument(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            known_as_of=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert len(known) == 1
        assert known[0].effective_at.year == 2019

    async def test_filter_by_action_type(self, session):
        instrument = await self.make_instrument(session)
        repo = CorporateActionRepository(session)
        await repo.upsert_many(
            instrument_id=instrument.id,
            actions=[self.split("2019-01-01"), self.dividend("2020-01-01")],
        )
        await session.flush()

        splits = await repo.list_for_instrument(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            action_types=[CorporateActionType.SPLIT],
        )
        assert len(splits) == 1
        assert splits[0].action_type is CorporateActionType.SPLIT

    async def test_split_without_a_ratio_is_rejected_by_the_database(self, session):
        """The CHECK constraint is a second line of defence behind the DTO."""
        from app.db.models import CorporateActionRow

        instrument = await self.make_instrument(session)
        session.add(
            CorporateActionRow(
                instrument_id=instrument.id,
                action_type=CorporateActionType.SPLIT,
                effective_at=datetime(2020, 1, 1, tzinfo=UTC),
                source="test",
            )
        )
        with pytest.raises(Exception, match="split_requires_ratio"):
            await session.flush()
