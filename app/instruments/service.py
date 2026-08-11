"""Instrument business logic."""

from __future__ import annotations

from collections.abc import Sequence

from app.core.errors import InstrumentNotFoundError
from app.db.models import Instrument
from app.domain.enums import AssetType
from app.instruments.repository import InstrumentRepository


class InstrumentService:
    """Read operations over the instrument universe.

    Thin by design: it exists so route handlers contain no data access (coding
    rule 10) and so "not found" is raised as a domain error rather than an HTTP
    concern leaking into the repository.
    """

    def __init__(self, repository: InstrumentRepository) -> None:
        self._repository = repository

    async def list_instruments(
        self,
        *,
        exchange: str | None = None,
        asset_type: AssetType | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Instrument]:
        return await self._repository.list_all(
            exchange=exchange,
            asset_type=asset_type,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )

    async def get_instrument(self, symbol: str) -> Instrument:
        """Fetch one instrument.

        Raises:
            InstrumentNotFoundError: no instrument with that symbol.
        """
        instrument = await self._repository.get_by_symbol(symbol)
        if instrument is None:
            raise InstrumentNotFoundError(symbol)
        return instrument
