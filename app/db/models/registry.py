"""Company and listing identity.

The defect this exists to fix
-----------------------------
Tradabot resolved a company by its bare ticker. That works while the universe is
one country and breaks the moment it is not: ``DTE`` is Deutsche Telekom in
Frankfurt and DTE Energy in Michigan, ``ALV`` is Allianz and Autoliv, ``ABX`` is
Barrick and Abacus Global Management. Phase 13.0 confirmed all four empirically
-- the old path would have produced a complete, confident, correctly formatted
report about the wrong company.

A wrong answer is worse than no answer, because no answer is obvious.

Two identities, deliberately separate
-------------------------------------
:class:`Company`
    The economic and reporting entity. Owns **fundamentals**: filings, statements,
    share counts, fiscal calendar, reporting currency. Identified by CIK where the
    SEC knows it, never by a ticker.

:class:`Listing`
    One security on one venue. Owns **market data**: bars, splits, dividends,
    quote currency, trading calendar. Identified by ``(mic, symbol)``.

One company has many listings. SAP SE files once and trades in Frankfurt and New
York in two currencies. Attaching fundamentals to a listing would duplicate them
per venue and invite exactly the ADR-versus-ordinary confusion this model exists
to prevent.

Why ``instruments`` is left alone
---------------------------------
``instruments`` is the **broker-tradable universe** -- what Alpaca offers, all of
it US. Its global ``UNIQUE(symbol)`` is correct *for that set* and is what 4.9
million candles, 443 thousand signal evaluations and every paper position hang
off by ``instrument_id``.

So this migration adds two tables and alters none. A listing points *optionally*
at an instrument when Tradabot happens to have prices for it. Deutsche Telekom on
Xetra can exist as a first-class identity with no instrument row and no prices at
all, which is precisely the state international support starts in.

The alternative -- relaxing ``UNIQUE(symbol)`` and inserting foreign rows into
``instruments`` -- would make ``get_by_symbol`` ambiguous at ten call sites
including market-data ingest and the backtester, for no gain this phase needs.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Company(Base, TimestampMixin):
    """An economic reporting entity. **Fundamentals attach here.**"""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        doc="ISO 3166-1 alpha-2 of the reporting entity, not of any listing venue.",
    )
    cik: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        unique=True,
        index=True,
        doc=(
            "SEC Central Index Key, zero-padded. The canonical key for anything "
            "the SEC knows about, including foreign private issuers. Null for a "
            "company that has never registered with the SEC."
        ),
    )
    lei: Mapped[str | None] = mapped_column(
        String(20), nullable=True, unique=True, doc="ISO 17442, when known."
    )
    reporting_currency: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        doc=(
            "ISO 4217 the company reports in. Distinct from any listing's quote "
            "currency, and the reason a valuation ratio can be refused rather "
            "than silently mixed."
        ),
    )
    taxonomy: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        doc="Accounting taxonomy of its filings: 'us-gaap' or 'ifrs-full'.",
    )
    sic: Mapped[str | None] = mapped_column(
        String(4),
        nullable=True,
        index=True,
        doc=(
            "The SEC's Standard Industrial Classification of this filer. The "
            "only sector signal that covers every company in the fact store, "
            "foreign private issuers included, and the basis on which generic "
            "margin and leverage analysis is refused for financial companies."
        ),
    )
    sic_description: Mapped[str | None] = mapped_column(
        String(128), nullable=True, doc="The SEC's own wording for that code."
    )

    __table_args__ = (
        CheckConstraint("length(country) = 2", name="company_country_iso3166"),
        CheckConstraint(
            "reporting_currency IS NULL OR length(reporting_currency) = 3",
            name="company_currency_iso4217",
        ),
        Index("ix_companies_country", "country"),
    )


class Listing(Base, TimestampMixin):
    """One security on one venue. **Prices attach here.**"""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, doc="Local ticker on this venue, upper-cased."
    )
    mic: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        doc=(
            "ISO 10383 Market Identifier Code -- XETR, XTSE, XNAS. A MIC rather "
            "than a display name because 'Frankfurt' and 'FSE' and 'Xetra' are "
            "three spellings of an ambiguous thing."
        ),
    )
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    quote_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, doc="ISO 4217 this listing trades in."
    )
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False, default="STOCK")
    is_primary: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        doc="Whether this is the company's primary listing. Never inferred.",
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        doc=(
            "The broker-tradable instrument, when Tradabot holds prices for this "
            "listing. Null is an ordinary state: a listing Tradabot can describe "
            "from filings but cannot price."
        ),
    )
    provider_symbol: Mapped[str | None] = mapped_column(
        String(32), nullable=True, doc="A data provider's own ticker for this listing."
    )

    __table_args__ = (
        # The venue-qualified identity. This is what makes DTE/XETR and DTE/XNYS
        # two different things rather than one impossible row.
        UniqueConstraint("mic", "symbol", name="uq_listings_mic_symbol"),
        CheckConstraint("symbol = upper(symbol)", name="listing_symbol_uppercase"),
        CheckConstraint("length(country) = 2", name="listing_country_iso3166"),
        CheckConstraint("length(quote_currency) = 3", name="listing_currency_iso4217"),
        Index("ix_listings_symbol", "symbol"),
    )
