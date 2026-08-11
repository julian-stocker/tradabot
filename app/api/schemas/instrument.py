"""Instrument wire schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import AssetType


class InstrumentResponse(BaseModel):
    """An instrument as returned by the API.

    ``from_attributes`` lets this be built straight from the ORM row, but it is a
    separate class on purpose: the database schema is free to change without
    breaking API consumers, and internal columns never leak by accident.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: int
    symbol: str
    name: str
    exchange: str
    currency: str = Field(description="ISO 4217 code.")
    asset_type: AssetType
    isin: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
