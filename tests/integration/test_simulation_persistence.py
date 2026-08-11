"""Signal, profile and trade-decision persistence, and the multi-profile fan-out."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.costs.models import NetEdge
from app.db.models import RiskProfile, SimulationProfile
from app.domain.enums import (
    Classification,
    DecisionReason,
    Horizon,
    PriceSeriesAdjustment,
    Timeframe,
    TradeDecisionType,
)
from app.instruments.repository import InstrumentRepository
from app.market_data.provider import InstrumentInfo
from app.signals.models import SignalResult
from app.signals.repository import SignalRepository
from app.simulation.defaults import (
    DEFAULT_CAPITAL_SIZES,
    DEFAULT_RISK_PROFILES,
    build_default_profiles,
)
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository
from app.simulation.service import SimulationEvaluationService

NOW = datetime(2024, 6, 3, 12, 0, tzinfo=UTC)


async def make_instrument(session, symbol: str = "TEST") -> int:
    repo = InstrumentRepository(session)
    await repo.upsert_many(
        [InstrumentInfo(symbol=symbol, name=f"{symbol} Inc.", exchange="XNAS", currency="USD")]
    )
    await session.flush()
    instrument = await repo.get_by_symbol(symbol)
    assert instrument is not None
    return instrument.id


def make_signal(
    *,
    score: float = 70.0,
    expected_move_bps: str = "120",
    price: str = "100",
    symbol: str = "TEST",
) -> SignalResult:
    return SignalResult(
        symbol=symbol,
        timestamp=NOW,
        generated_at=NOW,
        timeframe=Timeframe.D1,
        horizon=Horizon.D5,
        score=score,
        classification=Classification.STRONG_BULLISH,
        confidence=0.8,
        components=(),
        feature_snapshot={"rsi_14": 62.5, "sma_20": 98.0, "not_warmed": None},
        reference_price=Decimal(price),
        spread_bps=Decimal("8"),
        net_edge=NetEdge(
            expected_move_bps=Decimal(expected_move_bps),
            cost_bps=Decimal("19"),
            net_edge_bps=Decimal(expected_move_bps) - Decimal("19"),
        ),
        bars_used=200,
        engine_version="test-v1",
    )


class TestProfilePersistence:
    async def test_defaults_install(self, session):
        repo = SimulationProfileRepository(session)
        await repo.upsert_many(build_default_profiles())
        await session.flush()
        assert await repo.count_profiles() == 9

    async def test_risk_profiles_are_not_duplicated_per_portfolio(self, session):
        """The normalisation requirement, verified against the database.

        Nine portfolios must store three risk rows. Storing nine would mean
        editing "conservative" required nine consistent updates.
        """
        repo = SimulationProfileRepository(session)
        await repo.upsert_many(build_default_profiles())
        await session.flush()

        assert await repo.count_profiles() == len(DEFAULT_CAPITAL_SIZES) * len(
            DEFAULT_RISK_PROFILES
        )
        assert await repo.count_risk_profiles() == len(DEFAULT_RISK_PROFILES)

    async def test_portfolios_share_one_risk_row_by_id(self, session):
        await SimulationProfileRepository(session).upsert_many(build_default_profiles())
        await session.flush()

        balanced = (
            await session.execute(select(RiskProfile).where(RiskProfile.name == "balanced"))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(SimulationProfile).where(
                        SimulationProfile.risk_profile_id == balanced.id
                    )
                )
            )
            .unique()
            .scalars()
            .all()
        )
        assert len(rows) == len(DEFAULT_CAPITAL_SIZES)
        assert {r.initial_capital for r in rows} == set(DEFAULT_CAPITAL_SIZES)

    async def test_upsert_is_idempotent(self, session):
        repo = SimulationProfileRepository(session)
        for _ in range(3):
            await repo.upsert_many(build_default_profiles())
        await session.flush()
        assert await repo.count_profiles() == 9
        assert await repo.count_risk_profiles() == 3

    async def test_round_trip_preserves_configuration(self, session):
        repo = SimulationProfileRepository(session)
        await repo.upsert_many(build_default_profiles())
        await session.flush()

        loaded = await repo.get_profile("500eur-balanced")
        assert loaded.initial_capital == Decimal("500")
        assert loaded.risk.name == "balanced"
        assert loaded.costs.name == "flat-fee-retail"
        assert loaded.max_position_notional == Decimal("150.00")  # 500 x 30%

    async def test_unknown_profile_raises(self, session):
        from app.core.errors import NotFoundError

        await SimulationProfileRepository(session).upsert_many(build_default_profiles())
        await session.flush()
        with pytest.raises(NotFoundError):
            await SimulationProfileRepository(session).get_profile("nope")

    async def test_editing_a_risk_profile_moves_every_portfolio(self, session):
        """The payoff of normalising: one update, nine portfolios affected."""
        repo = SimulationProfileRepository(session)
        await repo.upsert_many(build_default_profiles())
        await session.flush()

        balanced = (
            await session.execute(select(RiskProfile).where(RiskProfile.name == "balanced"))
        ).scalar_one()
        balanced.min_signal_score = Decimal("99")
        await session.flush()
        session.expunge_all()

        for capital in ("50", "500", "5000"):
            loaded = await repo.get_profile(f"{capital}eur-balanced")
            assert loaded.risk.min_signal_score == Decimal("99")


class TestSignalPersistence:
    async def test_signal_round_trips(self, session):
        instrument_id = await make_instrument(session)
        repo = SignalRepository(session)

        signal_id = await repo.record(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        )
        stored = await repo.get(signal_id)
        assert stored is not None
        assert stored.score == 70.0
        assert stored.classification is Classification.STRONG_BULLISH
        assert stored.price_adjustment is PriceSeriesAdjustment.SPLIT_ADJUSTED

    async def test_feature_snapshot_survives_as_json_including_nulls(self, session):
        """`None` means "not warmed up" and must not become 0.0 in storage."""
        instrument_id = await make_instrument(session)
        repo = SignalRepository(session)
        signal_id = await repo.record(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        )
        stored = await repo.get(signal_id)
        assert stored is not None
        assert stored.feature_snapshot["rsi_14"] == 62.5
        assert stored.feature_snapshot["not_warmed"] is None

    async def test_recording_the_same_signal_twice_updates_one_row(self, session):
        instrument_id = await make_instrument(session)
        repo = SignalRepository(session)
        first = await repo.record(
            result=make_signal(score=70.0),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        )
        second = await repo.record(
            result=make_signal(score=71.0),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        )
        assert first == second
        assert await repo.count() == 1
        stored = await repo.get(first)
        assert stored is not None
        assert stored.score == 71.0

    async def test_different_adjustment_is_a_different_signal(self, session):
        """Raw and adjusted features genuinely differ; both records are wanted."""
        instrument_id = await make_instrument(session)
        repo = SignalRepository(session)
        adjusted = await repo.record(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
        )
        raw = await repo.record(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.RAW,
        )
        assert adjusted != raw
        assert await repo.count() == 2


class TestTradeDecisionFanOut:
    async def build(self, session) -> tuple[SimulationEvaluationService, int]:
        instrument_id = await make_instrument(session)
        await SimulationProfileRepository(session).upsert_many(build_default_profiles())
        await session.flush()
        service = SimulationEvaluationService(
            SignalRepository(session),
            SimulationProfileRepository(session),
            TradeDecisionRepository(session),
        )
        return service, instrument_id

    async def test_one_signal_produces_a_decision_per_profile(self, session):
        """The fan-out, end to end through the database."""
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        await session.flush()

        assert len(result.decisions) == 9
        stored = await TradeDecisionRepository(session).list_for_signal(result.signal_id)
        assert len(stored) == 9

    async def test_profiles_disagree_about_the_same_signal(self, session):
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(expected_move_bps="120"),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        assert not result.is_unanimous
        assert result.trades
        assert result.skips

    async def test_skips_are_persisted_with_their_reason(self, session):
        """The counterfactual sample. A system that stores only trades cannot
        measure what it missed."""
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(expected_move_bps="120"),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        await session.flush()

        stored = await TradeDecisionRepository(session).list_for_signal(result.signal_id)
        skips = [d for d in stored if d.decision is TradeDecisionType.SKIP]
        assert skips
        for skip in skips:
            assert skip.reason_detail, "every skip must say why"
            assert skip.reason is not DecisionReason.ACCEPTED
            # The economics are recorded even for a rejection, at the size that
            # was under consideration -- that is what the counterfactual needs.
            assert skip.available_capital > 0
            assert skip.reference_price > 0

    async def test_economic_skips_retain_the_hypothetical_size(self, session):
        """A skip at the cost gate keeps the size that *would* have been taken.

        This is what makes the counterfactual answerable: to ask "what would that
        rejected trade have returned, net of the fees that caused the rejection",
        the size and cost must be on the record. A skip rejected earlier, on
        conviction, never got that far and correctly stores zero.
        """
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(expected_move_bps="120"),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        await session.flush()

        stored = await TradeDecisionRepository(session).list_for_signal(result.signal_id)
        economic_skips = [
            d
            for d in stored
            if d.decision is TradeDecisionType.SKIP and d.reason is DecisionReason.NEGATIVE_NET_EDGE
        ]
        assert economic_skips, "expected the small portfolios to fail the cost gate"
        for skip in economic_skips:
            assert skip.position_quantity > 0
            assert skip.estimated_total_cost > 0
            assert skip.net_edge_bps_at_size < 0

    async def test_decision_records_size_specific_economics(self, session):
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        await session.flush()

        stored = await TradeDecisionRepository(session).list_for_signal(result.signal_id)
        costs = {d.simulation_profile_id: d.cost_bps_at_size for d in stored}
        assert len(set(costs.values())) > 1, "cost must differ across portfolio sizes"

    async def test_re_evaluating_updates_rather_than_duplicating(self, session):
        service, instrument_id = await self.build(session)
        for _ in range(3):
            result = await service.evaluate_signal(
                result=make_signal(),
                instrument_id=instrument_id,
                adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
                now=NOW,
            )
        await session.flush()
        stored = await TradeDecisionRepository(session).list_for_signal(result.signal_id)
        assert len(stored) == 9

    async def test_decisions_are_queryable_by_profile_and_verdict(self, session):
        service, instrument_id = await self.build(session)
        await service.evaluate_signal(
            result=make_signal(expected_move_bps="120"),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            now=NOW,
        )
        await session.flush()

        profiles = SimulationProfileRepository(session)
        small = await profiles.get_profile("50eur-balanced")
        assert small.id is not None

        decisions = TradeDecisionRepository(session)
        skips = await decisions.list_for_profile(
            simulation_profile_id=small.id, decision=TradeDecisionType.SKIP
        )
        assert len(skips) == 1
        trades = await decisions.list_for_profile(
            simulation_profile_id=small.id, decision=TradeDecisionType.TRADE
        )
        assert len(trades) == 0

    async def test_available_capital_override_changes_the_verdict(self, session):
        """Sizing follows free capital, not just the profile's initial figure."""
        service, instrument_id = await self.build(session)
        result = await service.evaluate_signal(
            result=make_signal(),
            instrument_id=instrument_id,
            adjustment=PriceSeriesAdjustment.SPLIT_ADJUSTED,
            available_capital={"5000eur-balanced": Decimal("10")},
            now=NOW,
        )
        constrained = next(d for d in result.decisions if d.profile_name == "5000eur-balanced")
        assert constrained.decision is TradeDecisionType.SKIP
        assert constrained.available_capital == Decimal("10")
