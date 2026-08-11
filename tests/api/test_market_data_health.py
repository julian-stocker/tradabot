"""The market-data health endpoint.

An ops endpoint is exactly the kind of surface that ends up exposed further than
intended, so the strongest assertions here are about what the response does *not*
contain.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.errors import ProviderError
from app.domain.quotes import Quote

ENDPOINT = "/health/market-data"
HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503

FAKE_KEY = "PKTESTFAKE1234567890"


async def test_health_reports_provider_and_configuration(client: AsyncClient) -> None:
    response = await client.get(ENDPOINT)

    body = response.json()
    assert body["provider"] == "mock"
    assert body["configured"] is True, "the mock is always configured -- that is its point"
    assert body["reachable"] is None, "no probe was requested"


async def test_health_never_returns_a_credential(client: AsyncClient) -> None:
    """No key, no prefix, no length -- nothing to reconstruct or confirm from."""
    response = await client.get(ENDPOINT)

    body = response.json()
    rendered = response.text.lower()

    assert "api_key" not in body
    assert "api_secret" not in body
    assert "key" not in rendered.replace("watchlist", "")
    assert FAKE_KEY.lower() not in rendered


async def test_an_empty_database_is_reported_as_stale(client: AsyncClient) -> None:
    """No data is not the same as fresh data, and must not read as healthy."""
    response = await client.get(ENDPOINT)

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    body = response.json()
    assert body["stale"] is True
    assert body["last_market_timestamp"] is None


async def test_stored_history_is_reported_with_its_age(seeded_client: AsyncClient) -> None:
    response = await seeded_client.get(ENDPOINT)

    body = response.json()
    assert body["last_market_timestamp"] is not None
    assert body["market_data_age_seconds"] > 0
    assert body["max_age_seconds"] > 0


async def test_history_ending_in_2023_is_stale(seeded_client: AsyncClient) -> None:
    """The seeded fixture stops at the start of 2024, which is years old by now."""
    response = await seeded_client.get(ENDPOINT)

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    assert response.json()["stale"] is True


async def test_the_endpoint_does_not_probe_unless_asked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A health check that always hits a rate-limited API is a way to exhaust quota."""
    calls = 0

    async def counting_quote(self: Any, symbol: str) -> Quote:
        nonlocal calls
        calls += 1
        raise ProviderError("should not have been called")

    monkeypatch.setattr(
        type(client._transport.app.state.provider),  # type: ignore[union-attr]
        "get_latest_quote",
        counting_quote,
    )

    await client.get(ENDPOINT)

    assert calls == 0


async def test_a_probe_reports_reachability(client: AsyncClient) -> None:
    response = await client.get(ENDPOINT, params={"probe": "true"})

    assert response.json()["reachable"] is True


async def test_a_failing_probe_is_reported_not_raised(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A health check must report a problem, not become one."""

    async def failing_quote(self: Any, symbol: str) -> Quote:
        raise ProviderError("upstream refused the connection")

    monkeypatch.setattr(
        type(client._transport.app.state.provider),  # type: ignore[union-attr]
        "get_latest_quote",
        failing_quote,
    )

    response = await client.get(ENDPOINT, params={"probe": "true"})

    assert response.status_code == HTTP_SERVICE_UNAVAILABLE
    body = response.json()
    assert body["reachable"] is False
    assert body["last_error"] is not None


async def test_a_probe_error_message_is_redacted(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that echoes a key into its error must not echo it into a response."""

    async def leaky_quote(self: Any, symbol: str) -> Quote:
        raise ProviderError(f"auth failed for api_key={FAKE_KEY}")

    monkeypatch.setattr(
        type(client._transport.app.state.provider),  # type: ignore[union-attr]
        "get_latest_quote",
        leaky_quote,
    )

    response = await client.get(ENDPOINT, params={"probe": "true"})

    assert FAKE_KEY not in response.text
