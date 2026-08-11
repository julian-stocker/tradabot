"""The read-only research and backtest endpoints.

There is deliberately **no** endpoint that starts a backtest. A 52-symbol replay
reads tens of thousands of candles and writes thousands of rows; exposing it over
unauthenticated HTTP would let a browser tab saturate the database and race the
scheduler for the same write lock.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_listing_backtests_is_empty_but_valid(client: AsyncClient) -> None:
    response = await client.get("/api/v1/backtests")

    assert response.status_code == 200
    assert response.json() == []


async def test_an_unknown_run_is_a_clean_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/backtests/9999")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_outcome_status_reports_zero_rather_than_failing(client: AsyncClient) -> None:
    response = await client.get("/api/v1/research/outcomes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["pending"] == 0


async def test_score_calibration_returns_every_band(client: AsyncClient) -> None:
    """Empty bands are still reported, with n=0 -- an absent band reads as an
    absent *measurement*, which is not the same as "nothing scored there"."""
    response = await client.get("/api/v1/research/score-calibration?horizon=1d")

    assert response.status_code == 200
    payload = response.json()
    labels = [band["label"] for band in payload["bands"]]
    assert labels == ["<0", "0-25", "25-50", "50-60", "60-70", "70-75", "75-80", "80-85", ">=85"]
    assert all(band["n"] == 0 for band in payload["bands"])


async def test_the_calibration_response_carries_its_caveat(client: AsyncClient) -> None:
    """The number must not travel without the warning attached to it."""
    response = await client.get("/api/v1/research/score-calibration?horizon=1d")

    caveat = response.json()["caveat"].lower()
    assert "not a basis for changing thresholds" in caveat


async def test_the_threshold_view_uses_the_finer_bands(client: AsyncClient) -> None:
    response = await client.get(
        "/api/v1/research/score-calibration?horizon=1d&around_threshold=true"
    )

    labels = [band["label"] for band in response.json()["bands"]]
    assert labels == ["60-65", "65-70", "70-75", "75-80", "80-85", ">=85"]


async def test_no_research_endpoint_leaks_a_credential(client: AsyncClient) -> None:
    for path in (
        "/api/v1/backtests",
        "/api/v1/research/outcomes",
        "/api/v1/research/score-calibration?horizon=1d",
    ):
        body = (await client.get(path)).text.lower()
        for forbidden in ("discord.com", "webhook", "api_key", "secret"):
            assert forbidden not in body, f"{forbidden} appeared in {path}"


async def test_there_is_no_endpoint_that_starts_a_backtest(client: AsyncClient) -> None:
    """Mutation endpoints are absent by design, not merely unimplemented."""
    for path in ("/api/v1/backtests", "/api/v1/research/outcomes"):
        assert (await client.post(path)).status_code in {404, 405}
