"""Domain exception hierarchy.

Mapped to HTTP status codes exactly once, in ``app.api.errors``. Business code
raises these; it never imports ``fastapi.HTTPException`` (coding rule 10).

Rule 8 -- *do not silently swallow exceptions* -- is why these exist at all:
every failure mode gets a name so callers can react to it deliberately.
"""

from __future__ import annotations


class TradabotError(Exception):
    """Base class for every error tradabot raises deliberately."""


class ConfigurationError(TradabotError):
    """Invalid or inconsistent application configuration."""


class NotFoundError(TradabotError):
    """A requested entity does not exist."""

    def __init__(self, entity: str, identifier: object) -> None:
        self.entity = entity
        self.identifier = identifier
        super().__init__(f"{entity} not found: {identifier!r}")


class InstrumentNotFoundError(NotFoundError):
    def __init__(self, symbol: str) -> None:
        super().__init__("instrument", symbol)
        self.symbol = symbol


class InsufficientDataError(TradabotError):
    """Not enough history to compute the requested value.

    Raised instead of returning a partially-warmed-up indicator. Returning a
    half-warmed SMA is a silent correctness bug; refusing is loud and testable.
    """

    def __init__(self, required: int, available: int, context: str = "") -> None:
        self.required = required
        self.available = available
        suffix = f" ({context})" if context else ""
        super().__init__(
            f"insufficient data: need at least {required} bars, have {available}{suffix}"
        )


class ProviderError(TradabotError):
    """A market-data provider failed or returned unusable data."""


class UnknownProviderError(ConfigurationError):
    def __init__(self, name: str, available: tuple[str, ...]) -> None:
        self.name = name
        self.available = available
        super().__init__(
            f"unknown market data provider {name!r}; available: {', '.join(available)}"
        )


class ValidationError(TradabotError):
    """External data violated an invariant we rely on."""
