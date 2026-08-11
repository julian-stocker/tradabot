"""FastAPI dependency wiring.

The composition root. Every service is constructed here from explicitly injected
collaborators (coding rule 4), so tests can override any single dependency
without patching module globals.

Engine and provider live on ``app.state``, created once during the lifespan, so a
request never pays to build a connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.corporate_actions.repository import CorporateActionRepository
from app.features.service import FeatureService
from app.instruments.repository import InstrumentRepository
from app.instruments.service import InstrumentService
from app.instruments.universe import UniverseService
from app.market_data.ingest import IngestionService
from app.market_data.provider import MarketDataProvider
from app.market_data.repository import CandleRepository
from app.market_data.service import MarketDataService
from app.paper.repository import PaperTradingRepository
from app.signals.repository import SignalRepository
from app.signals.service import SignalService
from app.simulation.repository import SimulationProfileRepository, TradeDecisionRepository
from app.simulation.service import SimulationEvaluationService


def get_app_settings(request: Request) -> Settings:
    """Settings stored on the app during startup, falling back to the singleton."""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return settings or get_settings()


def get_provider(request: Request) -> MarketDataProvider:
    """The active market-data provider, built once at startup."""
    provider: MarketDataProvider = request.app.state.provider
    return provider


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session with a transaction that commits on success.

    Read endpoints commit a no-op transaction, which is harmless and keeps write
    endpoints from having to manage transactions by hand.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ProviderDep = Annotated[MarketDataProvider, Depends(get_provider)]


def get_instrument_service(session: SessionDep) -> InstrumentService:
    return InstrumentService(InstrumentRepository(session))


InstrumentServiceDep = Annotated[InstrumentService, Depends(get_instrument_service)]


def get_market_data_service(
    session: SessionDep,
    instruments: InstrumentServiceDep,
    provider: ProviderDep,
) -> MarketDataService:
    return MarketDataService(instruments, CandleRepository(session), provider)


def get_feature_service(
    session: SessionDep,
    instruments: InstrumentServiceDep,
) -> FeatureService:
    return FeatureService(
        instruments, CandleRepository(session), CorporateActionRepository(session)
    )


def get_universe_service(session: SessionDep) -> UniverseService:
    return UniverseService(session)


def get_corporate_action_repository(session: SessionDep) -> CorporateActionRepository:
    return CorporateActionRepository(session)


def get_simulation_profile_repository(session: SessionDep) -> SimulationProfileRepository:
    return SimulationProfileRepository(session)


def get_paper_trading_repository(session: SessionDep) -> PaperTradingRepository:
    return PaperTradingRepository(session)


def get_simulation_evaluation_service(session: SessionDep) -> SimulationEvaluationService:
    return SimulationEvaluationService(
        SignalRepository(session),
        SimulationProfileRepository(session),
        TradeDecisionRepository(session),
    )


FeatureServiceDep = Annotated[FeatureService, Depends(get_feature_service)]


def get_signal_service(
    features: FeatureServiceDep,
    provider: ProviderDep,
    settings: SettingsDep,
) -> SignalService:
    return SignalService(features, provider, settings)


def get_ingestion_service(session: SessionDep, provider: ProviderDep) -> IngestionService:
    return IngestionService(session, provider)


UniverseServiceDep = Annotated[UniverseService, Depends(get_universe_service)]
CorporateActionRepositoryDep = Annotated[
    CorporateActionRepository, Depends(get_corporate_action_repository)
]
SimulationProfileRepositoryDep = Annotated[
    SimulationProfileRepository, Depends(get_simulation_profile_repository)
]
PaperTradingRepositoryDep = Annotated[PaperTradingRepository, Depends(get_paper_trading_repository)]
SimulationEvaluationServiceDep = Annotated[
    SimulationEvaluationService, Depends(get_simulation_evaluation_service)
]
MarketDataServiceDep = Annotated[MarketDataService, Depends(get_market_data_service)]
SignalServiceDep = Annotated[SignalService, Depends(get_signal_service)]
IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
