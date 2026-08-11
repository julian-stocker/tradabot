"""API contract tests.

Driven through ``httpx.ASGITransport`` -- the real app, real routing, real
serialisation, no network socket. Contracts are asserted on the response body,
not on internal call counts, so a refactor that preserves behaviour keeps passing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422

# The fixtures seed 2022-2024. The candles endpoint defaults to a window ending
# "now", which is correct in production but finds nothing in a historical
# fixture, so tests that need data state their window explicitly.
SEEDED_WINDOW = "start=2022-01-01T00:00:00Z&end=2024-01-01T00:00:00Z"


class TestHealth:
    async def test_health_reports_ok(self, client):
        response = await client.get("/health")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert body["market_data_provider"] == "mock"
        assert body["environment"] == "test"

    async def test_health_timestamp_is_utc(self, client):
        body = (await client.get("/health")).json()
        assert datetime.fromisoformat(body["timestamp"]).tzinfo is not None


class TestOpenAPI:
    async def test_schema_is_served(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == HTTP_OK
        assert response.json()["info"]["title"] == "tradabot"

    async def test_every_endpoint_is_documented(self, client):
        """A route without a summary is undiscoverable in the docs UI."""
        paths = (await client.get("/openapi.json")).json()["paths"]
        undocumented = [
            f"{method.upper()} {path}"
            for path, methods in paths.items()
            for method, spec in methods.items()
            if not spec.get("summary")
        ]
        assert not undocumented

    async def test_docs_page_is_available(self, client):
        assert (await client.get("/docs")).status_code == HTTP_OK


class TestInstruments:
    async def test_list_returns_the_universe(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["meta"]["count"] > 0
        assert {"NVDA", "AAPL"} <= {i["symbol"] for i in body["items"]}

    async def test_list_is_paginated(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments?limit=2")).json()
        assert len(body["items"]) == 2
        assert body["meta"]["limit"] == 2

    async def test_filter_by_exchange(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments?exchange=XETR")).json()
        assert all(i["exchange"] == "XETR" for i in body["items"])
        assert body["items"], "fixture universe should contain an XETR listing"

    async def test_filter_by_asset_type(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments?asset_type=ETF")).json()
        assert all(i["asset_type"] == "ETF" for i in body["items"])

    async def test_get_one(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA")).json()
        assert body["symbol"] == "NVDA"
        assert body["currency"] == "USD"
        assert body["isin"] == "US67066G1040"

    async def test_symbol_is_case_insensitive(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/nvda")
        assert response.status_code == HTTP_OK
        assert response.json()["symbol"] == "NVDA"

    async def test_unknown_symbol_is_404_with_a_useful_body(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NOPE")
        assert response.status_code == HTTP_NOT_FOUND

        body = response.json()
        assert body["error"] == "instrument_not_found"
        assert "NOPE" in body["detail"]


class TestCandles:
    async def test_returns_candles(self, seeded_client):
        response = await seeded_client.get(
            f"/api/v1/instruments/NVDA/candles?{SEEDED_WINDOW}&limit=50"
        )
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["symbol"] == "NVDA"
        assert body["timeframe"] == "1d"
        assert 0 < body["count"] <= 50
        assert len(body["candles"]) == body["count"]

    async def test_prices_are_serialised_as_strings(self, seeded_client):
        """JSON has no decimal type; a float round trip would lose precision."""
        body = (
            await seeded_client.get(f"/api/v1/instruments/NVDA/candles?{SEEDED_WINDOW}&limit=5")
        ).json()
        candle = body["candles"][0]
        for field in ("open", "high", "low", "close"):
            assert isinstance(candle[field], str), f"{field} must not be a JSON number"

    async def test_ohlc_invariants_hold(self, seeded_client):
        from decimal import Decimal

        body = (
            await seeded_client.get(f"/api/v1/instruments/NVDA/candles?{SEEDED_WINDOW}&limit=100")
        ).json()
        for candle in body["candles"]:
            high, low = Decimal(candle["high"]), Decimal(candle["low"])
            open_, close = Decimal(candle["open"]), Decimal(candle["close"])
            assert high >= low
            assert high >= max(open_, close)
            assert low <= min(open_, close)

    async def test_candles_are_ascending(self, seeded_client):
        body = (
            await seeded_client.get(f"/api/v1/instruments/NVDA/candles?{SEEDED_WINDOW}&limit=100")
        ).json()
        stamps = [c["timestamp"] for c in body["candles"]]
        assert stamps == sorted(stamps)

    async def test_time_window_is_respected(self, seeded_client):
        params = "start=2023-01-01T00:00:00Z&end=2023-02-01T00:00:00Z"
        body = (await seeded_client.get(f"/api/v1/instruments/NVDA/candles?{params}")).json()
        for candle in body["candles"]:
            stamp = datetime.fromisoformat(candle["timestamp"])
            assert datetime(2023, 1, 1, tzinfo=UTC) <= stamp < datetime(2023, 2, 1, tzinfo=UTC)

    async def test_inverted_window_is_a_400(self, seeded_client):
        params = "start=2023-06-01T00:00:00Z&end=2023-01-01T00:00:00Z"
        response = await seeded_client.get(f"/api/v1/instruments/NVDA/candles?{params}")
        assert response.status_code == HTTP_BAD_REQUEST
        assert response.json()["error"] == "invalid_request"

    async def test_invalid_timeframe_is_rejected(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/candles?timeframe=3y")
        assert response.status_code == HTTP_UNPROCESSABLE

    async def test_unknown_symbol_is_404(self, seeded_client):
        assert (
            await seeded_client.get("/api/v1/instruments/NOPE/candles")
        ).status_code == HTTP_NOT_FOUND


class TestQuote:
    async def test_returns_spread_metrics(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/quote")
        assert response.status_code == HTTP_OK

        body = response.json()
        from decimal import Decimal

        bid, ask = Decimal(body["bid"]), Decimal(body["ask"])
        assert ask >= bid
        assert Decimal(body["mid_price"]) == (bid + ask) / 2
        assert Decimal(body["spread_absolute"]) == ask - bid
        assert body["spread_bps"] == pytest.approx(body["spread_percent"] * 100)


class TestFeatures:
    async def test_returns_a_feature_series(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/features?bars=30")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["count"] == 30
        assert len(body["rows"]) == 30

    async def test_definitions_accompany_the_values(self, seeded_client):
        """A consumer must never have to read the source to interpret a number."""
        body = (await seeded_client.get("/api/v1/instruments/NVDA/features?bars=5")).json()
        assert body["definitions"]
        for definition in body["definitions"]:
            assert definition["description"].strip()
            assert definition["warmup_bars"] >= 1

        defined = {d["name"] for d in body["definitions"]}
        assert set(body["rows"][0]["values"]) <= defined

    async def test_expected_features_are_present(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/features?bars=5")).json()
        values = body["rows"][-1]["values"]
        for name in ("rsi_14", "sma_20", "ema_20", "atr_14", "volatility_20", "rel_volume_20"):
            assert name in values

    async def test_latest_snapshot_is_fully_warmed_up(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/features/latest")
        assert response.status_code == HTTP_OK

        body = response.json()
        unwarmed = [name for name, value in body["values"].items() if value is None]
        assert not unwarmed, f"snapshot returned unwarmed features: {unwarmed}"

    async def test_as_of_excludes_later_bars(self, seeded_client):
        """The API-level guarantee against look-ahead."""
        params = "as_of=2023-06-01T00:00:00Z"
        body = (
            await seeded_client.get(f"/api/v1/instruments/NVDA/features/latest?{params}")
        ).json()
        assert datetime.fromisoformat(body["timestamp"]) < datetime(2023, 6, 1, tzinfo=UTC)

    async def test_insufficient_history_is_422_not_404(self, seeded_client):
        """The instrument exists and the request is valid; we just cannot compute."""
        params = "as_of=2022-01-05T00:00:00Z"
        response = await seeded_client.get(f"/api/v1/instruments/NVDA/features/latest?{params}")
        assert response.status_code == HTTP_UNPROCESSABLE
        assert response.json()["error"] == "insufficient_data"

    async def test_unknown_symbol_is_404(self, seeded_client):
        assert (
            await seeded_client.get("/api/v1/instruments/NOPE/features")
        ).status_code == HTTP_NOT_FOUND


class TestSignal:
    async def test_returns_a_complete_signal(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/signal")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["symbol"] == "NVDA"
        assert -100 <= body["score"] <= 100
        assert body["classification"] in {
            "STRONG_BEARISH",
            "BEARISH",
            "NEUTRAL",
            "BULLISH",
            "STRONG_BULLISH",
        }
        assert 0.0 <= body["confidence"] <= 1.0
        assert body["engine_version"]

    async def test_signal_explains_itself(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        assert body["reasons"], "a signal must state its supporting evidence"
        for reason in body["reasons"]:
            assert reason["kind"] == "SUPPORT"
            assert reason["message"].strip()
            assert reason["code"].strip()
        for risk in body["risks"]:
            assert risk["kind"] == "RISK"

    async def test_all_components_are_reported(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        names = {c["name"] for c in body["components"]}
        assert names == {"momentum", "volume", "trend", "volatility", "regime", "spread"}

    async def test_quality_components_never_score_positive(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        for component in body["components"]:
            if component["kind"] == "QUALITY":
                assert component["score"] <= 0

    async def test_net_edge_is_reported(self, seeded_client):
        """Direction alone is not an opportunity; cost must be visible."""
        from decimal import Decimal

        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        edge = body["net_edge"]
        assert Decimal(edge["net_edge_bps"]) == Decimal(edge["expected_move_bps"]) - Decimal(
            edge["cost_bps"]
        )
        assert edge["is_actionable"] == (Decimal(edge["net_edge_bps"]) > 0)

    async def test_actionable_requires_direction_and_positive_edge(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        expected = body["classification"] != "NEUTRAL" and body["net_edge"]["is_actionable"]
        assert body["is_actionable"] == expected

    async def test_feature_snapshot_is_attached_for_audit(self, seeded_client):
        body = (await seeded_client.get("/api/v1/instruments/NVDA/signal")).json()
        assert body["feature_snapshot"]
        assert "rsi_14" in body["feature_snapshot"]

    @pytest.mark.parametrize(
        ("horizon", "bucket"),
        [("30m", "SHORT_TERM"), ("1d", "SHORT_TERM"), ("5d", "MEDIUM_TERM"), ("3mo", "LONG_TERM")],
    )
    async def test_horizons_are_supported(self, seeded_client, horizon, bucket):
        response = await seeded_client.get(f"/api/v1/instruments/NVDA/signal?horizon={horizon}")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["horizon"] == horizon
        assert body["horizon_bucket"] == bucket

    async def test_signal_is_deterministic(self, seeded_client):
        """Same inputs, same output. A signal that drifts cannot be evaluated."""
        params = "as_of=2023-11-01T00:00:00Z"
        first = (await seeded_client.get(f"/api/v1/instruments/NVDA/signal?{params}")).json()
        second = (await seeded_client.get(f"/api/v1/instruments/NVDA/signal?{params}")).json()
        assert first["score"] == second["score"]
        assert first["classification"] == second["classification"]
        assert first["feature_snapshot"] == second["feature_snapshot"]

    async def test_as_of_signal_uses_the_configured_spread(self, seeded_client):
        """A live spread in a historical signal would be look-ahead in the cost model."""
        params = "as_of=2023-11-01T00:00:00Z"
        body = (await seeded_client.get(f"/api/v1/instruments/NVDA/signal?{params}")).json()
        assert float(body["spread_bps"]) == 10.0

    async def test_unknown_symbol_is_404(self, seeded_client):
        assert (
            await seeded_client.get("/api/v1/instruments/NOPE/signal")
        ).status_code == HTTP_NOT_FOUND

    async def test_invalid_horizon_is_rejected(self, seeded_client):
        response = await seeded_client.get("/api/v1/instruments/NVDA/signal?horizon=42y")
        assert response.status_code == HTTP_UNPROCESSABLE


class TestAdminSync:
    async def test_sync_ingests_data(self, client):
        params = "symbols=NVDA&start=2023-01-01T00:00:00Z&end=2023-06-01T00:00:00Z"
        response = await client.post(f"/api/v1/admin/sync?{params}")
        assert response.status_code == HTTP_OK

        body = response.json()
        assert body["ok"]
        assert body["instruments_synced"] > 0
        assert body["candles_written"] > 0
        assert body["symbols_succeeded"] == ["NVDA"]

    async def test_sync_reports_per_symbol_failures(self, client):
        params = "symbols=NVDA,BOGUS&start=2023-01-01T00:00:00Z&end=2023-03-01T00:00:00Z"
        body = (await client.post(f"/api/v1/admin/sync?{params}")).json()
        assert not body["ok"]
        assert body["symbols_failed"][0]["symbol"] == "BOGUS"
        assert "NVDA" in body["symbols_succeeded"]

    async def test_sync_is_idempotent(self, client):
        params = "symbols=AAPL&start=2023-01-01T00:00:00Z&end=2023-03-01T00:00:00Z"
        first = (await client.post(f"/api/v1/admin/sync?{params}")).json()
        second = (await client.post(f"/api/v1/admin/sync?{params}")).json()
        assert first["candles_written"] == second["candles_written"]

        body = (
            await client.get(f"/api/v1/instruments/AAPL/candles?{SEEDED_WINDOW}&limit=5000")
        ).json()
        stamps = [c["timestamp"] for c in body["candles"]]
        assert len(stamps) == len(set(stamps)), "re-sync must not duplicate bars"


class TestEndToEnd:
    async def test_full_slice_from_ingest_to_signal(self, client):
        """The phase 1 goal: data -> storage -> features -> signal -> API."""
        params = "symbols=MSFT&start=2022-01-01T00:00:00Z&end=2024-01-01T00:00:00Z"
        sync = await client.post(f"/api/v1/admin/sync?{params}")
        assert sync.status_code == HTTP_OK
        assert sync.json()["candles_written"] > 100

        candles = await client.get(f"/api/v1/instruments/MSFT/candles?{SEEDED_WINDOW}&limit=10")
        assert candles.json()["count"] == 10

        features = await client.get("/api/v1/instruments/MSFT/features/latest")
        assert features.status_code == HTTP_OK
        assert all(v is not None for v in features.json()["values"].values())

        signal = await client.get("/api/v1/instruments/MSFT/signal?horizon=5d")
        assert signal.status_code == HTTP_OK

        body = signal.json()
        assert body["symbol"] == "MSFT"
        assert body["reasons"]
        assert body["components"]
        assert "net_edge" in body
