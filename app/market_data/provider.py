"""Market-data provider abstraction.

The rest of the application talks to this interface only. Provider SDKs, HTTP
clients, API keys and vendor quirks stay inside ``app.market_data.providers.*``
(coding rule 12: avoid vendor lock-in).

A :class:`typing.Protocol` is used rather than an abstract base class: providers
are independent adapters that share no implementation, so inheritance would buy
nothing and structural typing keeps test doubles trivial.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.time import ensure_utc, utc_now
from app.corporate_actions.models import CorporateAction
from app.domain.enums import AssetType, Timeframe
from app.domain.quotes import Quote


class InstrumentInfo(BaseModel):
    """Instrument metadata as reported by a provider.

    A transport DTO, not the ORM model: providers must never hand us database
    rows, and the database must never leak provider quirks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    exchange: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=3, max_length=3)
    asset_type: AssetType = AssetType.STOCK
    isin: str | None = Field(default=None, min_length=12, max_length=12)
    is_active: bool = True

    listed_at: datetime | None = Field(
        default=None, description="First instant the instrument was tradable, if known."
    )
    delisted_at: datetime | None = Field(
        default=None, description="First instant it was no longer tradable, if known."
    )

    @field_validator("symbol", "currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("listed_at", "delisted_at")
    @classmethod
    def _normalise_lifecycle(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _reconcile_lifecycle(self) -> InstrumentInfo:
        """Force ``is_active`` false for an instrument already delisted.

        The implication runs one way only::

            delisted_at in the past  =>  is_active is False

        The converse is deliberately *not* enforced. An instrument can be
        inactive without a delisting date -- suspended, halted, or simply flagged
        inactive by the provider -- and overwriting that to True would discard
        information the provider gave us.

        What this does prevent is the contradiction: a row that carries a past
        delisting date while claiming to be tradable. That state is meaningless
        and should never reach the database.

        Note this compares against *now*, so it answers "is it active today". The
        historical question is :meth:`~app.db.models.Instrument.is_tradable_at`,
        which uses the dates and ignores this flag entirely.
        """
        if (
            self.listed_at is not None
            and self.delisted_at is not None
            and self.delisted_at <= self.listed_at
        ):
            msg = (
                f"{self.symbol}: delisted_at ({self.delisted_at.isoformat()}) must be "
                f"after listed_at ({self.listed_at.isoformat()})"
            )
            raise ValueError(msg)

        already_delisted = self.delisted_at is not None and self.delisted_at <= utc_now()
        if already_delisted and self.is_active:
            object.__setattr__(self, "is_active", False)
        return self


class CandleData(BaseModel):
    """One OHLCV bar as delivered by a provider.

    Validated on construction (coding rule 6): providers are external systems and
    do send malformed bars. Rejecting here means the database CHECK constraints
    are a second line of defence rather than the first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime = Field(description="Bar open time (left edge), UTC.")
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)

    @field_validator("timestamp")
    @classmethod
    def _normalise_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _check_ohlc_consistency(self) -> CandleData:
        if self.high < self.low:
            msg = f"bar at {self.timestamp.isoformat()}: high {self.high} < low {self.low}"
            raise ValueError(msg)
        if self.high < max(self.open, self.close):
            msg = (
                f"bar at {self.timestamp.isoformat()}: high {self.high} is below "
                f"open/close (o={self.open}, c={self.close})"
            )
            raise ValueError(msg)
        if self.low > min(self.open, self.close):
            msg = (
                f"bar at {self.timestamp.isoformat()}: low {self.low} is above "
                f"open/close (o={self.open}, c={self.close})"
            )
            raise ValueError(msg)
        return self


@runtime_checkable
class MarketDataProvider(Protocol):
    """What tradabot needs from any market-data source.

    Implementations must guarantee:

    * candles are returned in ascending timestamp order;
    * the returned window is ``[start, end)`` -- half-open, so consecutive
      requests tile without duplicating a bar;
    * only *closed* bars are returned. A forming bar has a close price that will
      still change, and feeding it into a feature is look-ahead bias with extra
      steps;
    * all timestamps are timezone-aware UTC.
    """

    @property
    def name(self) -> str:
        """Stable identifier, e.g. ``"mock"`` or ``"alpaca"``."""
        ...

    async def get_instruments(self) -> list[InstrumentInfo]:
        """Every instrument this provider can serve."""
        ...

    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[CandleData]:
        """Closed bars for ``symbol`` in ``[start, end)``, ascending.

        Raises:
            ProviderError: the symbol is unknown or the request failed.
        """
        ...

    async def get_latest_quote(self, symbol: str) -> Quote:
        """Most recent top-of-book quote.

        Raises:
            ProviderError: the symbol is unknown or no quote is available.
        """
        ...

    async def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Every known corporate action for ``symbol``, ascending by effective time.

        Returning an empty list is a valid answer meaning "this provider supplies
        no corporate-action data". It is **not** the same as "this instrument had
        none", and the two are indistinguishable to a caller -- which is exactly
        why ingestion logs a warning when a provider never returns any. Silently
        adjusting prices with an incomplete action set produces a series that
        looks continuous and is wrong.

        Raises:
            ProviderError: the symbol is unknown or the request failed.
        """
        ...


@runtime_checkable
class BatchMarketDataProvider(MarketDataProvider, Protocol):
    """Optional extension for providers that fetch many symbols in one request.

    Separate for the same reason streaming is: forcing every provider to stub a
    capability it lacks is noise, and a caller can fall back to per-symbol
    requests. Consumers check ``isinstance(provider, BatchMarketDataProvider)``.

    Batching is not a micro-optimisation at scale. A 52-symbol universe across
    four timeframes is 208 sequential requests -- enough to approach Alpaca's
    Basic-plan rate ceiling and to take longer than the interval the sync runs
    at. Batched, it is four.
    """

    async def get_historical_candles_batch(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[CandleData]]:
        """Bars for many symbols, keyed by symbol.

        Every requested symbol appears in the result, mapping to an empty list
        when the provider returned nothing -- so "asked and got nothing" stays
        distinguishable from "never asked".
        """
        ...


class StreamingMarketDataProvider(MarketDataProvider, Protocol):
    """Optional extension for providers with a realtime feed.

    Deliberately separate: streaming is a genuinely different capability, and
    forcing every provider to stub out ``stream_quotes`` would be noise. Consumers
    check ``isinstance(provider, StreamingMarketDataProvider)`` when they need it.
    Not implemented in phase 1.
    """

    def stream_quotes(self, symbols: list[str]) -> object:
        """Return an async iterator of :class:`Quote` updates."""
        ...
