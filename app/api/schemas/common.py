"""Shared API response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    model_config = ConfigDict(extra="forbid")

    error: str = Field(description="Machine-readable error code, e.g. 'instrument_not_found'.")
    detail: str = Field(description="Human-readable explanation.")


class PageMeta(BaseModel):
    """Pagination metadata."""

    model_config = ConfigDict(extra="forbid")

    limit: int
    offset: int
    count: int = Field(description="Items in this response, not the total available.")


class Page(BaseModel, Generic[T]):
    """A page of results."""

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    meta: PageMeta


class HealthResponse(BaseModel):
    """Service liveness and dependency status."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="'ok' or 'degraded'.")
    version: str
    environment: str
    database: str = Field(description="'ok' or an error summary.")
    market_data_provider: str
    timestamp: datetime
