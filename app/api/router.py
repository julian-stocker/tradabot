"""Versioned API router aggregation.

Route modules are ordered so that literal paths are registered before the
parameterised ``/{symbol}`` catch-all in ``instruments``. Starlette matches in
registration order, so ``/instruments/{symbol}`` would otherwise shadow any
sibling literal route added later.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin,
    candles,
    corporate_actions,
    features,
    instruments,
    paper,
    signals,
    simulation,
)

api_router = APIRouter()
api_router.include_router(candles.router)
api_router.include_router(features.router)
api_router.include_router(signals.router)
api_router.include_router(corporate_actions.router)
api_router.include_router(paper.router)
api_router.include_router(paper.overview_router)
api_router.include_router(simulation.router)
api_router.include_router(instruments.router)
api_router.include_router(admin.router)
