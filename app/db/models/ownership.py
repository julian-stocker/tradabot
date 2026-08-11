"""Ownership and external-account boundaries.

**Nothing here is authentication.** These two tables exist so that adding a
second person later is a migration of *data*, not a redesign of the paper broker.
Today there is exactly one owner, created automatically, and every portfolio
belongs to it.

Why now rather than later
-------------------------
`simulation_profiles` will accumulate live portfolio state -- cash, positions,
realised P&L. Adding an ownership column to a table in that condition is a much
worse job than adding it to one holding a handful of configuration rows. Both
columns are nullable and default to the local owner, so nothing existing changes
behaviour.

Why credentials are *not* here
------------------------------
:class:`ExternalAccountConnection` records **that** a connection exists and what
it is for. It never stores the secret. The column is
``credential_reference`` -- a pointer into whatever secret store is in use -- and
a raw ``api_secret`` column would be a database backup away from a leak, in a
file that gets copied to laptops and attached to issues.

For this single-user installation the secret store is **environment variables**,
and ``credential_reference`` names the variable rather than holding its value.
See docs/provider-connections.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

LOCAL_OWNER_IDENTITY = "local-user"
"""The single owner of a local installation. A stable, meaningless identifier --
deliberately not a name, an email or anything else that would have to be
migrated when real identities arrive."""


class IdentityType(StrEnum):
    """Where an owner's identity comes from."""

    LOCAL = "LOCAL"
    """A local single-user installation. The only one implemented."""
    DISCORD = "DISCORD"
    """Future: a Discord user id, once a bot exists."""


class ConnectionProvider(StrEnum):
    ALPACA = "ALPACA"


class ConnectionPurpose(StrEnum):
    """What a connection is *for*.

    Separated from the provider because one provider serves several purposes
    with very different risk: market data is read-only, trading is not. Recording
    them distinctly means a future connection cannot silently acquire trading
    scope because it happened to be with the same vendor.
    """

    MARKET_DATA = "MARKET_DATA"
    PAPER_TRADING = "PAPER_TRADING"
    LIVE_TRADING = "LIVE_TRADING"


class ConnectionEnvironment(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class ConnectionStatus(StrEnum):
    CONFIGURED = "CONFIGURED"
    """Credentials are present in the configured secret store."""
    NOT_CONFIGURED = "NOT_CONFIGURED"
    ERROR = "ERROR"


class TradabotUser(Base):
    """Someone who owns portfolios.

    One row today. The table exists so a second one is an insert.
    """

    __tablename__ = "tradabot_users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    external_identity_type: Mapped[str] = mapped_column(
        String(16), nullable=False, doc="LOCAL today; DISCORD once a bot exists."
    )
    external_identity_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        doc="Identifier within that type. 'local-user' for a local installation.",
    )
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "external_identity_type",
            "external_identity_id",
            name="uq_tradabot_users_identity",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<TradabotUser {self.external_identity_type}:{self.external_identity_id}>"


class ExternalAccountConnection(Base):
    """A record that an owner has connected an external account.

    **Contains no secret and never will.** The connection is described here; the
    credential lives in whatever store ``credential_reference`` points at.

    Today the only row describes the global Alpaca market-data connection, owned
    by the local user, referencing the environment variables that hold it. It is
    documentation of the current arrangement, not a source of credentials --
    :mod:`app.market_data.registry` still reads configuration directly, and does
    not consult this table.
    """

    __tablename__ = "external_account_connections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("tradabot_users.id", ondelete="CASCADE"), nullable=False
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    connection_status: Mapped[str] = mapped_column(String(32), nullable=False)

    credential_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "A POINTER to the credential, never the credential. For this "
            "installation, the names of the environment variables that hold it. "
            "A raw secret column would be one database backup away from a leak."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "provider", "purpose", "environment", name="uq_connection_scope"
        ),
        Index("ix_external_connections_owner", "owner_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid
        return f"<ExternalAccountConnection {self.provider}/{self.purpose}>"
