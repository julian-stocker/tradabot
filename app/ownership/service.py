"""Owner and external-connection bookkeeping.

Seeds the single local owner, attaches the personal portfolios to it, and
records the global Alpaca market-data connection as a **description** of what is
configured -- never as a source of credentials.

The distinction matters. :mod:`app.market_data.registry` builds the provider from
settings and does not consult this table; if it did, a database row could change
which credentials the system uses, which is a much larger security surface than a
configuration file. What this table provides is the answer to "what is connected,
by whom, for what purpose", which is the question a second user makes hard.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.time import utc_now
from app.db.models import ExternalAccountConnection, SimulationProfile, TradabotUser
from app.db.models.ownership import (
    LOCAL_OWNER_IDENTITY,
    ConnectionEnvironment,
    ConnectionProvider,
    ConnectionPurpose,
    ConnectionStatus,
    IdentityType,
)

logger = get_logger(__name__)

LOCAL_OWNER_DISPLAY_NAME = "Local user"

ALPACA_CREDENTIAL_REFERENCE = "env:TRADABOT_ALPACA__API_KEY+API_SECRET"
"""**A pointer, not a secret.** Names the environment variables that hold the
credential so an operator can find it; contains nothing that could be used."""


@dataclass(frozen=True, slots=True)
class OwnershipReport:
    """What seeding did."""

    owner_id: int
    owner_created: bool
    profiles_assigned: int
    connections_recorded: int

    def summary(self) -> str:
        return (
            f"owner #{self.owner_id} "
            f"({'created' if self.owner_created else 'existing'}), "
            f"{self.profiles_assigned} portfolios assigned, "
            f"{self.connections_recorded} connections recorded"
        )


class OwnershipService:
    """Reads and maintains the ownership boundary."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def local_owner(self, *, create: bool = True) -> TradabotUser | None:
        """The single owner of this installation.

        Identified by a stable meaningless string rather than a name or an email,
        so real identities can arrive later without migrating the ones already
        stored.
        """
        stmt = select(TradabotUser).where(
            TradabotUser.external_identity_type == IdentityType.LOCAL.value,
            TradabotUser.external_identity_id == LOCAL_OWNER_IDENTITY,
        )
        owner = (await self._session.execute(stmt)).scalar_one_or_none()
        if owner is not None or not create:
            return owner

        owner = TradabotUser(
            external_identity_type=IdentityType.LOCAL.value,
            external_identity_id=LOCAL_OWNER_IDENTITY,
            display_name=LOCAL_OWNER_DISPLAY_NAME,
            enabled=True,
            created_at=utc_now(),
        )
        self._session.add(owner)
        await self._session.flush()
        logger.info("created local owner", owner_id=owner.id)
        return owner

    async def assign_unowned_profiles(self, owner: TradabotUser) -> int:
        """Give every ownerless portfolio to ``owner``.

        Idempotent, and only touches rows with no owner -- so running it after a
        second user exists cannot reassign their portfolios.
        """
        stmt = select(SimulationProfile).where(SimulationProfile.owner_id.is_(None))
        rows = (await self._session.execute(stmt)).scalars().all()
        for row in rows:
            row.owner_id = owner.id
        if rows:
            await self._session.flush()
        return len(rows)

    async def record_market_data_connection(
        self, owner: TradabotUser, settings: Settings
    ) -> ExternalAccountConnection:
        """Record the global Alpaca market-data connection.

        **Market data only.** Purpose is fixed to ``MARKET_DATA``; nothing here
        creates a trading connection, and the environment is ``PAPER`` because
        tradabot never authenticates against a live trading endpoint.

        Stores a *reference* to the credential, never the credential.
        """
        stmt = select(ExternalAccountConnection).where(
            ExternalAccountConnection.owner_id == owner.id,
            ExternalAccountConnection.provider == ConnectionProvider.ALPACA.value,
            ExternalAccountConnection.purpose == ConnectionPurpose.MARKET_DATA.value,
            ExternalAccountConnection.environment == ConnectionEnvironment.PAPER.value,
        )
        connection = (await self._session.execute(stmt)).scalar_one_or_none()

        status = (
            ConnectionStatus.CONFIGURED
            if settings.alpaca.is_configured
            else ConnectionStatus.NOT_CONFIGURED
        )
        now = utc_now()

        if connection is None:
            connection = ExternalAccountConnection(
                owner_id=owner.id,
                provider=ConnectionProvider.ALPACA.value,
                purpose=ConnectionPurpose.MARKET_DATA.value,
                environment=ConnectionEnvironment.PAPER.value,
                connection_status=status.value,
                credential_reference=ALPACA_CREDENTIAL_REFERENCE,
                created_at=now,
                updated_at=now,
            )
            self._session.add(connection)
        else:
            connection.connection_status = status.value
            connection.credential_reference = ALPACA_CREDENTIAL_REFERENCE
            connection.updated_at = now

        await self._session.flush()
        return connection

    async def connections(self, owner_id: int | None = None) -> list[ExternalAccountConnection]:
        stmt = select(ExternalAccountConnection)
        if owner_id is not None:
            stmt = stmt.where(ExternalAccountConnection.owner_id == owner_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def profiles_for(self, owner_id: int) -> list[SimulationProfile]:
        stmt = (
            select(SimulationProfile)
            .where(SimulationProfile.owner_id == owner_id)
            .order_by(SimulationProfile.initial_capital)
        )
        return list((await self._session.execute(stmt)).scalars().all())


async def ensure_local_ownership(session: AsyncSession, settings: Settings) -> OwnershipReport:
    """Seed the owner, adopt unowned portfolios, record the connection.

    Safe to run repeatedly; every step is idempotent.
    """
    service = OwnershipService(session)
    existing = await service.local_owner(create=False)
    owner = existing or await service.local_owner()
    # `create=True` always returns a row; this narrows the type for mypy.
    assert owner is not None

    assigned = await service.assign_unowned_profiles(owner)
    await service.record_market_data_connection(owner, settings)

    report = OwnershipReport(
        owner_id=owner.id,
        owner_created=existing is None,
        profiles_assigned=assigned,
        connections_recorded=1,
    )
    logger.info("ownership ensured", summary=report.summary())
    return report
