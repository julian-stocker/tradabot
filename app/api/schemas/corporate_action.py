"""Corporate-action and universe wire schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import AssetType, CorporateActionType


class CorporateActionResponse(BaseModel):
    """One corporate action."""

    model_config = ConfigDict(extra="forbid")

    action_type: CorporateActionType
    effective_at: datetime = Field(
        description="Split: when new shares begin trading. Dividend: the ex-date."
    )
    payment_at: datetime | None = None
    from_shares: Decimal | None = Field(default=None, description="2-for-1 split: 1.")
    to_shares: Decimal | None = Field(default=None, description="2-for-1 split: 2.")
    split_ratio: Decimal | None = Field(
        default=None, description="Shares after per share before. Below 1 for a reverse split."
    )
    cash_amount: Decimal | None = None
    currency: str | None = None
    source: str
    description: str = Field(description="Human-readable summary.")


class CorporateActionListResponse(BaseModel):
    """Every known action for one instrument."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    count: int
    actions: list[CorporateActionResponse]


class UniverseMemberResponse(BaseModel):
    """An instrument in a point-in-time universe."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    symbol: str
    name: str
    exchange: str
    currency: str
    asset_type: AssetType
    listed_at: datetime | None
    delisted_at: datetime | None


class UniverseResponse(BaseModel):
    """The instruments tradable at a moment, or across a window.

    ``as_of`` / ``start`` + ``end`` are echoed back deliberately: a universe
    without the date it applies to is exactly the ambiguity that produces
    survivorship bias.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    count: int
    instruments: list[UniverseMemberResponse]
