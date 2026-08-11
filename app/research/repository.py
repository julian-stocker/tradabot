"""Persistence for research outcomes and backtest runs.

Upserts are keyed on the natural identity of a label -- (evaluation, horizon,
direction, policy version) -- rather than on a surrogate id, which is what makes
the labelling job safe to run on a schedule. A second run over the same window
updates rows in place; it does not accumulate a duplicate set that would double
the weight of every observation in the resulting statistics.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_utc, utc_now
from app.db.models import BacktestRun, SignalEvaluation, SignalOutcome, TradeOutcome
from app.domain.enums import LabelStatus


class OutcomeRepository:
    """Reads and writes :class:`SignalOutcome` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def existing_for(
        self, *, evaluation_id: int, label_policy_version: str
    ) -> dict[tuple[str, int], SignalOutcome]:
        """Every stored outcome for one evaluation, keyed by (horizon, direction)."""
        stmt = select(SignalOutcome).where(
            SignalOutcome.evaluation_id == evaluation_id,
            SignalOutcome.label_policy_version == label_policy_version,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {(row.horizon, row.direction): row for row in rows}

    async def upsert(self, outcome: SignalOutcome) -> SignalOutcome:
        """Insert, or update the existing row for the same natural key.

        A ``PENDING`` row that now has enough future data is *updated* to
        ``COMPLETE`` rather than replaced, so its id -- and anything joined to it
        -- survives maturation.
        """
        existing = await self._session.scalar(
            select(SignalOutcome).where(
                SignalOutcome.evaluation_id == outcome.evaluation_id,
                SignalOutcome.horizon == outcome.horizon,
                SignalOutcome.direction == outcome.direction,
                SignalOutcome.label_policy_version == outcome.label_policy_version,
            )
        )
        if existing is None:
            self._session.add(outcome)
            return outcome

        for column in (
            "status",
            "future_timestamp",
            "future_price",
            "raw_return",
            "mfe",
            "mae",
            "target_status",
            "stop_status",
            "barrier_outcome",
            "time_to_target_seconds",
            "time_to_stop_seconds",
            "ambiguous_bar_timestamp",
            "label_timeframe",
            "bars_observed",
            "rolled_to_next_session",
            "reference_price",
            "reference_timestamp",
            "computed_at",
        ):
            setattr(existing, column, getattr(outcome, column))
        return existing

    async def status_counts(self) -> dict[str, int]:
        stmt = select(SignalOutcome.status, func.count()).group_by(SignalOutcome.status)
        return dict((await self._session.execute(stmt)).all())  # type: ignore[arg-type]

    async def horizon_counts(self) -> dict[str, int]:
        stmt = (
            select(SignalOutcome.horizon, func.count())
            .where(SignalOutcome.status == LabelStatus.COMPLETE.value)
            .group_by(SignalOutcome.horizon)
        )
        return dict((await self._session.execute(stmt)).all())  # type: ignore[arg-type]

    async def pending_count(self) -> int:
        stmt = select(func.count()).where(SignalOutcome.status != LabelStatus.COMPLETE.value)
        return int((await self._session.execute(stmt)).scalar_one())

    async def total_count(self) -> int:
        stmt = select(func.count()).select_from(SignalOutcome)
        return int((await self._session.execute(stmt)).scalar_one())


class EvaluationCursor:
    """Streams evaluations in id order, in bounded chunks.

    Part AH: a 52-symbol replay produces far more observations than should ever
    be materialised at once, and one transaction spanning the whole job would
    hold a write lock against the live scheduler for its entire duration.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def chunk(
        self,
        *,
        after_id: int,
        limit: int,
        since: datetime | None = None,
        until: datetime | None = None,
        instrument_ids: Sequence[int] | None = None,
    ) -> list[SignalEvaluation]:
        stmt = select(SignalEvaluation).where(SignalEvaluation.id > after_id)
        if since is not None:
            stmt = stmt.where(SignalEvaluation.evaluated_at >= ensure_utc(since))
        if until is not None:
            stmt = stmt.where(SignalEvaluation.evaluated_at <= ensure_utc(until))
        if instrument_ids:
            stmt = stmt.where(SignalEvaluation.instrument_id.in_(list(instrument_ids)))
        stmt = stmt.order_by(SignalEvaluation.id).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())


class BacktestRunRepository:
    """Persistence for backtest run metadata."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, run: BacktestRun) -> BacktestRun:
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return await self._session.get(BacktestRun, run_id)

    async def by_key(self, run_key: str) -> BacktestRun | None:
        stmt = select(BacktestRun).where(BacktestRun.run_key == run_key)
        row: BacktestRun | None = await self._session.scalar(stmt)
        return row

    async def list_recent(self, limit: int = 20) -> Sequence[BacktestRun]:
        stmt = select(BacktestRun).order_by(BacktestRun.started_at.desc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def complete(
        self,
        run: BacktestRun,
        *,
        observation_count: int,
        symbols_processed: int,
        metrics: dict[str, Any],
        duration_seconds: float,
    ) -> BacktestRun:
        run.status = "COMPLETED"
        run.completed_at = utc_now()
        run.observation_count = observation_count
        run.symbols_processed = symbols_processed
        run.metrics = metrics
        run.duration_seconds = duration_seconds
        return run

    async def fail(self, run: BacktestRun, *, error: str) -> BacktestRun:
        run.status = "FAILED"
        run.completed_at = utc_now()
        run.error = error
        return run


class TradeOutcomeRepository:
    """Reads and writes execution-aware outcomes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, outcome: TradeOutcome) -> TradeOutcome:
        existing = await self._session.scalar(
            select(TradeOutcome).where(
                TradeOutcome.evaluation_id == outcome.evaluation_id,
                TradeOutcome.simulation_profile_id == outcome.simulation_profile_id,
                TradeOutcome.backtest_run_id == outcome.backtest_run_id,
                TradeOutcome.cost_model_version == outcome.cost_model_version,
            )
        )
        if existing is None:
            self._session.add(outcome)
            return outcome
        for column in (
            "executed",
            "rejection_reason",
            "entry_timestamp",
            "entry_price",
            "exit_timestamp",
            "exit_price",
            "quantity",
            "exit_reason",
            "gross_pnl",
            "fees",
            "spread_cost",
            "slippage_cost",
            "net_pnl",
            "net_return",
            "holding_period_seconds",
            "modelled_spread_bps",
            "cost_basis",
            "spread_quality",
            "session_phase",
            "computed_at",
        ):
            setattr(existing, column, getattr(outcome, column))
        return existing

    async def for_run(self, run_id: int) -> Sequence[TradeOutcome]:
        stmt = select(TradeOutcome).where(TradeOutcome.backtest_run_id == run_id)
        return (await self._session.execute(stmt)).scalars().all()
