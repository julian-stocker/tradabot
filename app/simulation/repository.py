"""Simulation-profile and trade-decision persistence."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.db.models import (
    BrokerCostProfile,
    RiskProfile,
    SimulationProfile,
    TradeDecisionRow,
)
from app.domain.enums import TradeDecisionType
from app.simulation.decisions import TradeDecision
from app.simulation.models import BrokerCostConfig, RiskConfig, SimulationProfileConfig

logger = get_logger(__name__)


class SimulationProfileRepository:
    """Data access for the three profile tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_profiles(self, *, enabled_only: bool = True) -> list[SimulationProfileConfig]:
        """Every simulation profile, with its risk and cost configuration.

        The relationships are eager-loaded (``lazy="joined"``), so this is one
        query rather than one per profile -- signal fan-out evaluates every
        profile on every signal, so an N+1 here would be felt immediately.
        """
        stmt = select(SimulationProfile).order_by(SimulationProfile.name)
        if enabled_only:
            stmt = stmt.where(SimulationProfile.enabled.is_(True))
        rows = (await self._session.execute(stmt)).unique().scalars().all()
        return [_profile_to_domain(row) for row in rows]

    async def get_profile(self, name: str) -> SimulationProfileConfig:
        """One profile by name.

        Raises:
            NotFoundError: no profile with that name.
        """
        stmt = select(SimulationProfile).where(SimulationProfile.name == name)
        row = (await self._session.execute(stmt)).unique().scalar_one_or_none()
        if row is None:
            raise NotFoundError("simulation_profile", name)
        return _profile_to_domain(row)

    async def upsert_cost_profile(self, config: BrokerCostConfig) -> int:
        """Insert or update a broker cost profile by name. Returns its id."""
        stmt = select(BrokerCostProfile).where(BrokerCostProfile.name == config.name)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = BrokerCostProfile(name=config.name)
            self._session.add(row)

        row.description = config.description
        row.order_fee = config.order_fee
        row.variable_fee_rate = config.variable_fee_rate
        row.slippage_spread_multiple = config.slippage_spread_multiple
        row.default_spread_bps = config.default_spread_bps
        row.min_order_notional = config.min_order_notional
        await self._session.flush()
        return row.id

    async def upsert_risk_profile(self, config: RiskConfig) -> int:
        """Insert or update a risk profile by name. Returns its id."""
        stmt = select(RiskProfile).where(RiskProfile.name == config.name)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = RiskProfile(name=config.name)
            self._session.add(row)

        row.description = config.description
        row.risk_per_trade = config.risk_per_trade
        row.max_position_percent = config.max_position_percent
        row.max_total_exposure = config.max_total_exposure
        row.max_open_positions = config.max_open_positions
        row.max_daily_loss = config.max_daily_loss
        row.max_drawdown = config.max_drawdown
        row.min_signal_score = config.min_signal_score
        row.min_confidence = config.min_confidence
        row.require_positive_net_edge = config.require_positive_net_edge
        row.allow_short = config.allow_short
        row.stop_loss_atr_multiple = config.stop_loss_atr_multiple
        row.take_profit_r_multiple = config.take_profit_r_multiple
        row.max_holding_bars = config.max_holding_bars
        row.require_stop_loss = config.require_stop_loss
        row.allow_pyramiding = config.allow_pyramiding
        row.max_quote_age_seconds = config.max_quote_age_seconds
        await self._session.flush()
        return row.id

    async def upsert_profile(self, config: SimulationProfileConfig) -> int:
        """Insert or update a simulation profile and its dependencies.

        The risk and cost profiles are upserted first and referenced by id, so
        nine portfolios sharing three risk profiles store three risk rows -- not
        nine copies.
        """
        risk_id = await self.upsert_risk_profile(config.risk)
        cost_id = await self.upsert_cost_profile(config.costs)

        stmt = select(SimulationProfile).where(SimulationProfile.name == config.name)
        row = (await self._session.execute(stmt)).unique().scalar_one_or_none()
        if row is None:
            row = SimulationProfile(name=config.name)
            self._session.add(row)

        row.description = config.description
        row.initial_capital = config.initial_capital
        row.currency = config.currency
        row.risk_profile_id = risk_id
        row.broker_cost_profile_id = cost_id
        row.enabled = config.enabled
        await self._session.flush()
        return row.id

    async def upsert_many(self, configs: Sequence[SimulationProfileConfig]) -> int:
        for config in configs:
            await self.upsert_profile(config)
        logger.info("upserted simulation profiles", count=len(configs))
        return len(configs)

    async def count_risk_profiles(self) -> int:
        return len((await self._session.execute(select(RiskProfile.id))).scalars().all())

    async def count_profiles(self) -> int:
        return len((await self._session.execute(select(SimulationProfile.id))).scalars().all())


class TradeDecisionRepository:
    """Data access for :class:`~app.db.models.TradeDecisionRow`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        decision: TradeDecision,
        signal_id: int,
        simulation_profile_id: int,
        instrument_id: int,
    ) -> int:
        """Persist one decision, replacing any prior verdict for the same pair.

        Re-evaluating a signal for a profile overwrites rather than appends: the
        table answers "what did this profile decide about this signal", which has
        exactly one answer.
        """
        stmt = select(TradeDecisionRow).where(
            TradeDecisionRow.signal_id == signal_id,
            TradeDecisionRow.simulation_profile_id == simulation_profile_id,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = TradeDecisionRow(
                signal_id=signal_id,
                simulation_profile_id=simulation_profile_id,
                instrument_id=instrument_id,
            )
            self._session.add(row)

        row.decided_at = decision.decided_at
        row.decision = decision.decision
        row.reason = decision.reason
        row.reason_detail = decision.reason_detail
        row.side = decision.side
        row.signal_score = decision.signal_score
        row.signal_classification = decision.signal_classification
        row.signal_confidence = decision.signal_confidence
        row.expected_move_bps = decision.expected_move_bps
        row.reference_price = decision.reference_price
        row.bid = decision.bid
        row.ask = decision.ask
        row.spread_bps = decision.spread_bps
        row.available_capital = decision.available_capital
        row.position_quantity = decision.position_quantity
        row.position_notional = decision.position_notional
        row.estimated_fees = decision.estimated_fees
        row.estimated_spread_cost = decision.estimated_spread_cost
        row.estimated_slippage = decision.estimated_slippage
        row.estimated_total_cost = decision.estimated_total_cost
        row.cost_bps_at_size = decision.cost_bps_at_size
        row.net_edge_bps_at_size = decision.net_edge_bps_at_size
        await self._session.flush()
        return row.id

    async def list_for_signal(self, signal_id: int) -> Sequence[TradeDecisionRow]:
        """Every profile's verdict on one signal -- the fan-out, read back."""
        stmt = (
            select(TradeDecisionRow)
            .where(TradeDecisionRow.signal_id == signal_id)
            .order_by(TradeDecisionRow.simulation_profile_id)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def list_for_profile(
        self,
        *,
        simulation_profile_id: int,
        decision: TradeDecisionType | None = None,
        limit: int = 200,
    ) -> Sequence[TradeDecisionRow]:
        """A profile's decision history, newest first.

        ``decision=SKIP`` is as useful a query as ``TRADE``: the skipped
        opportunities are the counterfactual sample.
        """
        stmt = (
            select(TradeDecisionRow)
            .where(TradeDecisionRow.simulation_profile_id == simulation_profile_id)
            .order_by(TradeDecisionRow.decided_at.desc())
            .limit(limit)
        )
        if decision is not None:
            stmt = stmt.where(TradeDecisionRow.decision == decision)
        return (await self._session.execute(stmt)).scalars().all()


def _profile_to_domain(row: SimulationProfile) -> SimulationProfileConfig:
    return SimulationProfileConfig(
        id=row.id,
        name=row.name,
        description=row.description,
        initial_capital=row.initial_capital,
        currency=row.currency,
        enabled=row.enabled,
        risk=RiskConfig(
            id=row.risk_profile.id,
            name=row.risk_profile.name,
            description=row.risk_profile.description,
            risk_per_trade=row.risk_profile.risk_per_trade,
            max_position_percent=row.risk_profile.max_position_percent,
            max_total_exposure=row.risk_profile.max_total_exposure,
            max_open_positions=row.risk_profile.max_open_positions,
            max_daily_loss=row.risk_profile.max_daily_loss,
            max_drawdown=row.risk_profile.max_drawdown,
            min_signal_score=row.risk_profile.min_signal_score,
            min_confidence=row.risk_profile.min_confidence,
            require_positive_net_edge=row.risk_profile.require_positive_net_edge,
            allow_short=row.risk_profile.allow_short,
            stop_loss_atr_multiple=row.risk_profile.stop_loss_atr_multiple,
            take_profit_r_multiple=row.risk_profile.take_profit_r_multiple,
            max_holding_bars=row.risk_profile.max_holding_bars,
            require_stop_loss=row.risk_profile.require_stop_loss,
            allow_pyramiding=row.risk_profile.allow_pyramiding,
            max_quote_age_seconds=row.risk_profile.max_quote_age_seconds,
        ),
        costs=BrokerCostConfig(
            id=row.broker_cost_profile.id,
            name=row.broker_cost_profile.name,
            description=row.broker_cost_profile.description,
            order_fee=row.broker_cost_profile.order_fee,
            variable_fee_rate=row.broker_cost_profile.variable_fee_rate,
            slippage_spread_multiple=row.broker_cost_profile.slippage_spread_multiple,
            default_spread_bps=row.broker_cost_profile.default_spread_bps,
            min_order_notional=row.broker_cost_profile.min_order_notional,
        ),
    )
