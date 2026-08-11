"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.api.routes.health import router as health_router
from app.api.routes.market_data import router as market_data_health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory
from app.market_data.registry import build_provider

logger = get_logger(__name__)

DESCRIPTION = """
Local-first stock-market analysis, signal generation and backtesting platform.

**What this is:** a research tool producing transparent, rule-based, explainable
signals with transaction costs modelled as a first-class concern.

**What this is not:** a price predictor, a trading bot, or financial advice.
Signal scores are ordinal heuristics with no statistical validation yet -- the
baseline weights exist to be falsified by backtesting, not to be trusted.

No order execution exists anywhere in this service, by design.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    Args:
        settings: injected configuration. Tests pass their own instead of
            monkeypatching the environment.
    """
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Create long-lived resources once, dispose them on shutdown."""
        engine = create_engine(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.provider = build_provider(settings)

        logger.info(
            "tradabot starting",
            environment=settings.env.value,
            provider=settings.market_data_provider,
        )
        try:
            yield
        finally:
            await engine.dispose()
            logger.info("tradabot stopped")

    app = FastAPI(
        title="tradabot",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(market_data_health_router)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
