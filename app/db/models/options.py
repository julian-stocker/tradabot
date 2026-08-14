"""Point-in-time option surface snapshots.

Why this table exists at all
----------------------------
Alpaca serves option chains as **snapshots of now**. There is no historical
"as of" parameter, and historical option bars require the OPRA agreement and
only reach back to February 2024 even then. So an option surface that is not
captured today cannot be reconstructed tomorrow -- unlike candles, which can
always be backfilled.

That asymmetry is the whole justification: every day this table is not written
is a day of research that can never be recovered.

Two grains, deliberately
------------------------
:class:`OptionSurfaceSnapshot` is one row per symbol per capture -- the derived
summary (ATM IV, skew, term slope, expected move). Small enough to keep forever.

:class:`OptionQuoteSnapshot` is the contract-level slice it was derived from,
restricted to a canonical window around the money. Keeping it means a future
phase can recompute skew a different way instead of being stuck with today's
definition; restricting it keeps the table from growing 9x for contracts nobody
will study. Measured on this universe: 2,598 contracts per symbol per day full,
283 canonical.

Provenance is not optional here
-------------------------------
``feed`` records ``indicative`` or ``opra``. The free indicative feed is
explicitly *not* the consolidated OPRA best bid/offer -- Alpaca documents it as
"indicative derivatives" of quotes -- so any IV computed from it is approximate.
A later reader must be able to tell which feed a row came from without guessing,
because mixing the two silently would put a step change in the middle of a
volatility series.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import Money, UTCDateTime

PRICE_PRECISION = 18
PRICE_SCALE = 6
RATIO_PRECISION = 12
RATIO_SCALE = 6


class OptionSurfaceSnapshot(Base):
    """One symbol's derived option surface at one capture instant.

    Every field here is a *derivation*, and the raw inputs live in
    :class:`OptionQuoteSnapshot` rows sharing the same ``captured_at``. Nothing
    is inferred when its inputs are missing -- a NULL means "could not be
    computed from what the feed returned", never a filled-in guess.
    """

    __tablename__ = "option_surface_snapshots"
    __table_args__ = (
        UniqueConstraint("instrument_id", "captured_at", name="uq_option_surface_capture"),
        Index("ix_option_surface_captured", "captured_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    underlying_price: Mapped[Decimal] = mapped_column(
        Money(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )

    atm_iv: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    """Implied volatility of the near-the-money contracts in the front window."""

    iv_30d: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    """ATM IV interpolated to 30 days. **Derived**, and NULL when the two
    bracketing expiries do not exist rather than extrapolated from one."""

    skew_25d: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    """Put IV minus call IV at roughly 25-delta. Positive is the usual equity
    shape; NVDA measured +4.2 vol points and JPM measured negative, so the sign
    carries information rather than being a constant."""

    term_slope: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    """Far-expiry ATM IV minus near-expiry ATM IV. Negative means event premium
    in the front month."""

    expected_move_pct: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    """The straddle-implied one-sigma move to the front expiry, as a percent of
    spot. **Magnitude only** -- this table carries no directional claim, exactly
    as ``ExpectedMovement`` does not."""

    contracts_seen: Mapped[int] = mapped_column(nullable=False, default=0)
    contracts_with_iv: Mapped[int] = mapped_column(nullable=False, default=0)
    """Both counts kept so a thin capture is visible as thin. On this universe
    roughly 63% of contracts carry IV; a sudden drop means the feed changed, not
    that the market did."""

    feed: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)


class OptionQuoteSnapshot(Base):
    """One contract, at one capture instant, inside the canonical window.

    The primary key is ``(instrument_id, captured_at, occ_symbol)``: a capture
    is idempotent, so a retried job overwrites rather than duplicating.
    """

    __tablename__ = "option_quote_snapshots"
    __table_args__ = (
        Index("ix_option_quotes_captured", "captured_at"),
        Index("ix_option_quotes_instrument_expiry", "instrument_id", "expiration"),
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    occ_symbol: Mapped[str] = mapped_column(String(32), primary_key=True)

    expiration: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    strike: Mapped[Decimal] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE), nullable=False)
    option_type: Mapped[str] = mapped_column(String(1), nullable=False)
    """``C`` or ``P``, as parsed from the OCC symbol."""

    bid: Mapped[Decimal | None] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE))
    ask: Mapped[Decimal | None] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE))
    mid: Mapped[Decimal | None] = mapped_column(Money(PRICE_PRECISION, PRICE_SCALE))
    """Stored rather than recomputed on read: the bid and ask that produced it
    are this feed's, and a later reader must not re-derive a mid from a
    different quote."""

    implied_volatility: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    delta: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    gamma: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    vega: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))
    theta: Mapped[Decimal | None] = mapped_column(Money(RATIO_PRECISION, RATIO_SCALE))

    open_interest: Mapped[int | None] = mapped_column()
    """Absent on the indicative feed -- measured 0 of 14,001 contracts. The
    column exists so an OPRA capture can populate it without a migration."""

    feed: Mapped[str] = mapped_column(String(16), nullable=False)
