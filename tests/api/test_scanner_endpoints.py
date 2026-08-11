"""Scanner status, candidates and signal-history endpoints.

Read-only, and the strongest assertion is that none of them leaks a credential
or can trigger a scan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.db.models import SignalEvaluation, TrackedSignal

HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422

T0 = datetime(2024, 6, 5, 15, 0, tzinfo=UTC)


def make_evaluation(instrument_id: int, *, score: float, qualified: bool, **extra: object):
    """A minimal but complete evaluation row."""
    defaults = {
        "instrument_id": instrument_id,
        "evaluated_at": T0,
        "market_data_timestamp": T0 - timedelta(minutes=5),
        "score": score,
        "confidence": 0.7,
        "classification": "BULLISH",
        "direction": 1,
        "qualified": qualified,
        "agreement": 0.75,
        "aligned": True,
        "net_edge_bps": 60.0,
        "spread_bps": 8.0,
        "timeframe_states": {"1h": {"role": "primary", "trend": "UP"}},
        "trend_metrics": {},
        "momentum_metrics": {},
        "volume_metrics": {"relative_volume": 1.4},
        "volatility_metrics": {},
        "structure_metrics": {},
        "liquidity_metrics": {},
        "reason_codes": [],
        "risk_codes": [],
        "data_quality": "OK",
        "session_phase": "REGULAR",
        "feature_set_version": "features-v1",
        "signal_model_version": "signal-v1",
        "scanner_policy_version": "scanner-v1",
    }
    return SignalEvaluation(**(defaults | extra))


def make_signal(instrument_id: int, *, lifecycle: str = "QUALIFIED", score: float = 80.0):
    return TrackedSignal(
        instrument_id=instrument_id,
        direction="LONG",
        primary_timeframe="1h",
        horizon="5d",
        setup="BREAKOUT",
        lifecycle=lifecycle,
        current_score=score,
        peak_score=score,
        current_confidence=0.7,
        evaluation_count=1,
        discovered_at=T0,
        last_evaluated_at=T0,
        qualified_at=T0,
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
async def test_status_reports_configuration(client: AsyncClient) -> None:
    response = await client.get("/api/v1/scanner/status")

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["watchlist_size"] == 0
    assert body["signal_threshold"] > 0
    assert body["session_phase"]


async def test_status_never_exposes_a_credential(client: AsyncClient) -> None:
    text = (await client.get("/api/v1/scanner/status")).text.lower()

    for forbidden in ("api_key", "secret", "webhook", "discord.com", "password"):
        assert forbidden not in text


async def test_status_reports_a_scan_that_has_never_run(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/scanner/status")).json()

    assert body["last_scan_started"] is None
    assert body["last_success"] is None


async def test_status_describes_the_threshold_as_a_heuristic(client: AsyncClient) -> None:
    """The schema must not let a reader mistake 75 for a probability."""
    schema = (await client.get("/openapi.json")).json()
    description = schema["components"]["schemas"]["ScannerStatusResponse"]["properties"][
        "signal_threshold"
    ]["description"]

    assert "not a probability" in description.lower()


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------
async def test_no_candidates_returns_an_empty_list(client: AsyncClient) -> None:
    """Zero is a valid answer, not an error and not padding."""
    response = await client.get("/api/v1/scanner/candidates")

    assert response.status_code == HTTP_OK
    body = response.json()
    assert body["candidates"] == []
    assert body["total_qualified"] == 0


async def test_candidates_are_ranked_with_their_contributions(
    seeded_client: AsyncClient, engine: object
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        session.add(make_evaluation(1, score=80.0, qualified=True))
        session.add(make_evaluation(2, score=92.0, qualified=True))
        await session.commit()

    body = (await seeded_client.get("/api/v1/scanner/candidates")).json()

    assert len(body["candidates"]) == 2
    assert body["candidates"][0]["score"] == 92.0, "highest first"
    assert body["candidates"][0]["contributions"], "the ordering is auditable"


async def test_unqualified_evaluations_are_not_candidates(
    seeded_client: AsyncClient, engine: object
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        session.add(make_evaluation(1, score=40.0, qualified=False))
        await session.commit()

    body = (await seeded_client.get("/api/v1/scanner/candidates")).json()

    assert body["candidates"] == []


async def test_fewer_than_the_limit_returns_what_exists(
    seeded_client: AsyncClient, engine: object
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        session.add(make_evaluation(1, score=80.0, qualified=True))
        session.add(make_evaluation(2, score=85.0, qualified=True))
        await session.commit()

    body = (await seeded_client.get("/api/v1/scanner/candidates?limit=5")).json()

    assert len(body["candidates"]) == 2


async def test_the_candidate_limit_is_bounded(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/scanner/candidates?limit=999")).status_code == (
        HTTP_UNPROCESSABLE
    )


async def test_candidates_disclaim_what_ranking_means(client: AsyncClient) -> None:
    note = (await client.get("/api/v1/scanner/candidates")).json()["note"]

    assert "not" in note.lower()
    assert "probability" in note.lower()


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
async def test_active_signals_are_listed(seeded_client: AsyncClient, engine: object) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        session.add(make_signal(1))
        await session.commit()

    body = (await seeded_client.get("/api/v1/signals/active")).json()

    assert len(body) == 1
    assert body[0]["lifecycle"] == "QUALIFIED"
    assert body[0]["symbol"]


async def test_terminal_signals_are_not_active(seeded_client: AsyncClient, engine: object) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        session.add(make_signal(1, lifecycle="INVALIDATED"))
        session.add(make_signal(2, lifecycle="EXPIRED"))
        await session.commit()

    assert (await seeded_client.get("/api/v1/signals/active")).json() == []


async def test_one_signal_can_be_fetched(seeded_client: AsyncClient, engine: object) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        signal = make_signal(1)
        session.add(signal)
        await session.commit()
        signal_id = signal.id

    body = (await seeded_client.get(f"/api/v1/signals/{signal_id}")).json()

    assert body["id"] == signal_id
    assert body["setup"] == "BREAKOUT"
    assert body["peak_score"] == 80.0


async def test_an_unknown_signal_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/signals/99999")).status_code == HTTP_NOT_FOUND


async def test_a_signals_evaluation_history_is_readable(
    seeded_client: AsyncClient, engine: object
) -> None:
    """The X history: what tradabot knew at each point, with no future values."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)  # type: ignore[arg-type]
    async with factory() as session:
        signal = make_signal(1)
        session.add(signal)
        await session.flush()
        session.add(make_evaluation(1, score=80.0, qualified=True, tracked_signal_id=signal.id))
        await session.commit()
        signal_id = signal.id

    body = (await seeded_client.get(f"/api/v1/signals/{signal_id}/evaluations")).json()

    assert len(body) == 1
    assert body[0]["feature_set_version"] == "features-v1"
    assert "timeframe_states" in body[0]


async def test_evaluation_history_for_an_unknown_signal_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/signals/99999/evaluations")).status_code == HTTP_NOT_FOUND


async def test_no_endpoint_can_start_a_scan(client: AsyncClient) -> None:
    """A scan is a scheduled operation. An HTTP trigger would be an
    unauthenticated way to exhaust provider quota."""
    schema = (await client.get("/openapi.json")).json()

    scanner_paths = {p: list(m) for p, m in schema["paths"].items() if "scanner" in p}
    assert scanner_paths, "the scanner endpoints exist"
    for methods in scanner_paths.values():
        assert set(methods) == {"get"}, "read-only"
