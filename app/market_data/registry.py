"""Provider registry.

The single place that knows which concrete providers exist. Adding a real
provider means writing an adapter and registering it here -- nothing else in the
application changes (coding rule 12).
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.core.errors import UnknownProviderError
from app.market_data.provider import MarketDataProvider
from app.market_data.providers.mock import MockMarketDataProvider

ProviderFactory = Callable[[Settings], MarketDataProvider]

_REGISTRY: dict[str, ProviderFactory] = {
    "mock": lambda settings: MockMarketDataProvider(seed=settings.mock_seed),
}


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under ``name``.

    Raises:
        ValueError: if ``name`` is already taken. Silent replacement would make
            "which provider am I actually running?" unanswerable.
    """
    key = name.lower()
    if key in _REGISTRY:
        msg = f"market data provider {key!r} is already registered"
        raise ValueError(msg)
    _REGISTRY[key] = factory


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def build_provider(settings: Settings) -> MarketDataProvider:
    """Instantiate the provider named by ``settings.market_data_provider``."""
    key = settings.market_data_provider.lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise UnknownProviderError(key, available_providers())
    return factory(settings)
