"""API contracts for corporate actions, universe, adjustment and simulation profiles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422

SEEDED_WINDOW = "start=2022-01-01T00:00:00Z&end=2024-01-01T00:00:00Z"


class TestCorporateActionEndpoint:
    async def test_returns_splits_for_an_instrument(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/corporate-actions")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["symbol"] == "NVDA"
        assert body["count"] >= 1
        splits = [a for a in body["actions"] if a["action_type"] == "SPLIT"]
        assert splits, "the mock NVDA has splits"
        assert splits[0]["split_ratio"] is not None

    async def test_returns_dividends_with_money_and_dates(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/AAPL/corporate-actions")).json()
        dividends = [a for a in body["actions"] if a["action_type"] == "CASH_DIVIDEND"]
        assert dividends
        for dividend in dividends:
            assert dividend["cash_amount"] is not None
            assert dividend["currency"] == "USD"
            assert dividend["split_ratio"] is None, "a dividend is not a share-count change"

    async def test_actions_are_chronological(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/corporate-actions")).json()
        stamps = [a["effective_at"] for a in body["actions"]]
        assert stamps == sorted(stamps)

    async def test_known_as_of_hides_later_actions(self, seeded_client):
        params = "known_as_of=2022-01-01T00:00:00Z"
        body = (
            await seeded_client.get(f"/api/v1/instruments/NVDA/corporate-actions?{params}")
        ).json()
        for action in body["actions"]:
            assert datetime.fromisoformat(action["effective_at"]) <= datetime(
                2022, 1, 1, tzinfo=UTC
            )

    async def test_every_action_is_described(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/corporate-actions")).json()
        assert all(a["description"] for a in body["actions"])

    async def test_unknown_symbol_is_404(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NOPE/corporate-actions")
        assert response.status_code == HTTP_NOT_FOUND


class TestUniverseEndpoint:
    async def test_point_in_time_universe(self, seeded_client):
        response = await seeded_client.get("/api/v1/universe?as_of=2023-01-01T00:00:00Z")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["as_of"] is not None
        assert body["count"] > 0

    async def test_delisted_instrument_appears_only_while_listed(self, seeded_client):
        """OLDCO was delisted 2022-09-30 in the mock universe."""
        during = (await seeded_client.get("/api/v1/universe?as_of=2022-01-01T00:00:00Z")).json()
        after = (await seeded_client.get("/api/v1/universe?as_of=2023-01-01T00:00:00Z")).json()

        assert "OLDCO" in {i["symbol"] for i in during["instruments"]}
        assert "OLDCO" not in {i["symbol"] for i in after["instruments"]}

    async def test_late_lister_is_absent_before_it_existed(self, seeded_client):
        """LATE lists 2021-06-01 in the mock universe."""
        before = (await seeded_client.get("/api/v1/universe?as_of=2021-01-01T00:00:00Z")).json()
        assert "LATE" not in {i["symbol"] for i in before["instruments"]}

    async def test_window_query_uses_overlap(self, seeded_client):
        params = "start=2022-01-01T00:00:00Z&end=2023-01-01T00:00:00Z"
        body = (await seeded_client.get(f"/api/v1/universe?{params}")).json()
        assert body["start"] is not None
        assert body["end"] is not None
        assert "OLDCO" in {i["symbol"] for i in body["instruments"]}, (
            "delisted mid-window, so it was tradable for part of it"
        )

    async def test_lifecycle_dates_are_exposed(self, seeded_client):
        body = (await seeded_client.get("/api/v1/universe?as_of=2022-01-01T00:00:00Z")).json()
        oldco = next(i for i in body["instruments"] if i["symbol"] == "OLDCO")
        assert oldco["delisted_at"] is not None
        assert oldco["listed_at"] is not None

    async def test_defaults_to_now(self, seeded_client):
        body = (await seeded_client.get("/api/v1/universe")).json()
        assert body["as_of"] is not None
        assert "OLDCO" not in {i["symbol"] for i in body["instruments"]}


class TestAdjustmentOnFeatureEndpoints:
    async def test_default_is_split_adjusted(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        assert body["price_adjustment"] == "SPLIT_ADJUSTED"

    async def test_raw_and_adjusted_features_differ_across_a_split(self, seeded_client):
        """NVDA splits 4-for-1 in 2021 and 10-for-1 in 2024 in the mock data."""
        adjusted = (
            await seeded_client.get(
                "/api/v1/instruments/NVDA/features/latest?adjustment=SPLIT_ADJUSTED"
            )
        ).json()
        raw = (
            await seeded_client.get("/api/v1/instruments/NVDA/features/latest?adjustment=RAW")
        ).json()
        assert adjusted["values"]["sma_20"] != raw["values"]["sma_20"]

    async def test_total_return_is_refused_clearly(self, seeded_client):
        """Better an explicit refusal than a silently wrong series."""
        response = await seeded_client.get(
            "/api/v1/instruments/NVDA/features/latest?adjustment=TOTAL_RETURN"
        )
        assert response.status_code >= 500
        assert "TOTAL_RETURN" in response.text or "internal" in response.text.lower()

    async def test_invalid_adjustment_is_rejected(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/features?adjustment=MAGIC")
        assert response.status_code == HTTP_UNPROCESSABLE

    async def test_signal_accepts_an_adjustment(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/signal?adjustment=RAW")
        assert response.status_code == HTTP_OK
        assert response.json()["price_adjustment"] == "RAW"


class TestSimulationProfileEndpoints:
    @pytest.fixture
    async def client_with_profiles(self, seeded_client, engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.simulation.defaults import build_default_profiles
        from app.simulation.repository import SimulationProfileRepository

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await SimulationProfileRepository(session).upsert_many(build_default_profiles())
            await session.commit()
        return seeded_client

    async def test_lists_profiles(self, client_with_profiles):
        response = await client_with_profiles.get("/api/v1/simulation/profiles")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["count"] == 9
        assert body["distinct_risk_profiles"] == 3, (
            "nine portfolios must share three risk profiles, not nine copies"
        )

    async def test_profile_exposes_derived_economics(self, client_with_profiles):
        body = (
            await client_with_profiles.get("/api/v1/simulation/profiles/500eur-balanced")
        ).json()
        assert body["initial_capital"] == "500.000000"
        assert body["risk"]["name"] == "balanced"
        assert float(body["max_position_notional"]) == 150.0  # 500 x 30% (balanced)
        assert float(body["risk_budget"]) == 5.0

    async def test_same_risk_profile_across_capital_sizes(self, client_with_profiles):
        body = (await client_with_profiles.get("/api/v1/simulation/profiles")).json()
        balanced = [p for p in body["profiles"] if p["risk"]["name"] == "balanced"]
        assert len(balanced) == 3
        assert len({p["risk"]["risk_per_trade"] for p in balanced}) == 1
        assert len({p["initial_capital"] for p in balanced}) == 3

    async def test_unknown_profile_is_404(self, client_with_profiles):
        response = await client_with_profiles.get("/api/v1/simulation/profiles/nope")
        assert response.status_code == HTTP_NOT_FOUND


class TestAdminSyncIngestsActions:
    async def test_sync_reports_corporate_actions(self, client):
        params = f"symbols=NVDA&{SEEDED_WINDOW}"
        body = (await client.post(f"/api/v1/admin/sync?{params}")).json()
        assert body["ok"]
        assert body["corporate_actions_written"] >= 1

    async def test_synced_actions_are_readable(self, client):
        params = f"symbols=NVDA&{SEEDED_WINDOW}"
        await client.post(f"/api/v1/admin/sync?{params}")
        body = (await client.get("/api/v1/instruments/NVDA/corporate-actions")).json()
        assert body["count"] >= 1
