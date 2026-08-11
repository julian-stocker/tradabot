"""Instrument (tradable symbol) ORM model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import ensure_utc
from app.db.base import Base, TimestampMixin
from app.db.types import UTCDateTime
from app.domain.enums import AssetType


class Instrument(Base, TimestampMixin):
    """A tradable instrument, with its listing lifecycle.

    ``symbol`` is globally unique in phase 1. That is a deliberate simplification:
    the same ticker can exist on multiple venues (``VOD`` on LSE vs. Xetra), so a
    real multi-venue setup needs a ``(symbol, exchange)`` composite key. Changing
    it later is a mechanical migration; guessing a venue today would not be.
    See docs/architecture.md#known-simplifications.

    **Two notions of "active" coexist, deliberately:**

    * :attr:`is_active` is the *provider's current view*. Cheap to filter on, and
      what a UI listing wants.
    * :meth:`is_tradable_at` is the *authority for historical questions*. Any
      scanner or backtest asking "was this tradable then?" must use it, never
      :attr:`is_active`, because today's flag says nothing about 2019.

    They cannot silently diverge: :class:`~app.market_data.provider.InstrumentInfo`
    derives ``is_active`` from ``delisted_at`` when one is supplied.
    """

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        doc="Ticker as used by the data provider, upper-cased.",
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    exchange: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        doc="Venue identifier, ideally a MIC such as XNAS or XETR.",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        doc="ISO 4217 currency code the instrument is quoted in.",
    )
    asset_type: Mapped[AssetType] = mapped_column(
        Enum(AssetType, native_enum=False, length=16, validate_strings=True),
        nullable=False,
    )
    isin: Mapped[str | None] = mapped_column(
        String(12),
        nullable=True,
        unique=True,
        doc="ISO 6166 identifier, when the provider supplies one.",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        doc=(
            "The provider's current view. False for delisted/inactive instruments. "
            "Rows are never deleted so that backtests can include them and avoid "
            "survivorship bias. For historical questions use is_tradable_at()."
        ),
    )

    listed_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        doc=(
            "First instant the instrument was tradable. NULL means unknown, which "
            "is treated as 'listed before all available history' -- the honest "
            "reading, since we cannot invent a listing date we were never told."
        ),
    )
    delisted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
        doc=(
            "First instant the instrument was no longer tradable. NULL means still "
            "listed. Half-open interval: an instrument is tradable on "
            "[listed_at, delisted_at)."
        ),
    )

    provider: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        doc="Provider that supplied this instrument's metadata (mock, alpaca, ...).",
    )
    provider_symbol: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        doc=(
            "The provider's own ticker, when it differs from ours. Kept on the "
            "instrument rather than on every candle: one row, not millions."
        ),
    )

    __table_args__ = (
        CheckConstraint("length(currency) = 3", name="currency_iso4217"),
        CheckConstraint("symbol = upper(symbol)", name="symbol_uppercase"),
        CheckConstraint(
            "listed_at IS NULL OR delisted_at IS NULL OR delisted_at > listed_at",
            name="lifecycle_ordered",
        ),
        Index("ix_instruments_exchange_symbol", "exchange", "symbol"),
        # Serves point-in-time universe queries, which filter on both bounds.
        Index("ix_instruments_listed_at_delisted_at", "listed_at", "delisted_at"),
    )

    def is_tradable_at(self, moment: datetime) -> bool:
        """Was this instrument tradable at ``moment``?

        The half-open interval ``[listed_at, delisted_at)`` is deliberate and
        matches the convention used for candle windows everywhere else: an
        instrument delisted at time T did not trade *at* T.

        A NULL bound is treated as unbounded on that side. That is the
        conservative reading for ``listed_at`` (we have no evidence it was
        unlisted) and the correct one for ``delisted_at`` (still trading).

        Note this asks a question about the *listing*, not about whether the
        market was open -- session calendars are a separate concern.
        """
        moment = ensure_utc(moment)
        if self.listed_at is not None and moment < self.listed_at:
            return False
        return not (self.delisted_at is not None and moment >= self.delisted_at)

    def __repr__(self) -> str:
        return f"<Instrument {self.symbol} ({self.exchange})>"
