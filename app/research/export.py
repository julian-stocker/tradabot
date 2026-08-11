"""Deterministic dataset export, with the feature/label boundary made explicit.

Column groups are declared, not inferred. :data:`FEATURE_COLUMNS` is what a model
may consume; :data:`LABEL_COLUMNS` is what it may be scored against;
:data:`CONTEXT_COLUMNS` is metadata for filtering and splitting. The manifest
records all three, and :func:`assert_no_leakage` checks at export time that no
label column reached the feature group -- because the failure mode is silent and
the resulting model looks excellent right up until it meets live data.

Splitting (part AB)
-------------------
Every row carries ``reference_timestamp``, ``label_end_timestamp`` and
``session_date``. Those three are what a future walk-forward split needs: the
first to order, the second to purge rows whose outcome window overlaps the test
period, the third to embargo whole sessions. A random row-level split of this
dataset would be invalid -- consecutive 5-minute observations of the same symbol
are near-duplicates with overlapping label windows, so random assignment leaks
the test set into training almost perfectly. Nothing here performs a split; it
only guarantees the columns exist to do one correctly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.models import Instrument, SignalEvaluation, SignalOutcome, WatchlistEntry
from app.domain.enums import Horizon, LabelStatus
from app.research.analytics import RESEARCH_FEATURES, _feature_view, _sector_of
from app.research.costs import COST_MODEL_VERSION
from app.research.horizons import LABEL_POLICY_VERSION
from app.research.quality import SUSPICIOUS_SPREAD_BPS

DATASET_VERSION: Final = "dataset-v1"

IDENTITY_COLUMNS: Final[tuple[str, ...]] = (
    "evaluation_id",
    "symbol",
    "sector",
    "reference_timestamp",
)

FEATURE_COLUMNS: Final[tuple[str, ...]] = RESEARCH_FEATURES
"""**X.** Signal-time only. Every one is read from a column that
:func:`~app.scanner.service._build_evaluation` populated from data available at
the evaluation instant."""

LABEL_COLUMNS: Final[tuple[str, ...]] = (
    "raw_return",
    "mfe",
    "mae",
    "barrier_outcome",
    "time_to_target_seconds",
    "time_to_stop_seconds",
    "future_price",
    "future_timestamp",
    "label_status",
)
"""**Y.** Derived from bars after the reference instant. Never an input."""

CONTEXT_COLUMNS: Final[tuple[str, ...]] = (
    "horizon",
    "session_phase",
    "data_quality",
    "label_timeframe",
    "label_end_timestamp",
    "session_date",
    "qualified",
    "classification",
    "direction",
    "tracked_signal_id",
    "backtest_run_id",
    "feature_set_version",
    "signal_model_version",
    "scanner_policy_version",
    "label_policy_version",
)
"""Filtering, versioning and split metadata. Not features, not labels.

``tracked_signal_id`` and ``session_date`` are the sampling-policy handles (part
R): they are what identifies consecutive evaluations of the same setup, so
correlated rows can be recognised later rather than silently counted as
independent evidence."""


class LeakageError(RuntimeError):
    """Raised when a label column appears among the features.

    A hard failure rather than a warning. An export that leaks is worse than no
    export, because everything downstream of it looks like it works.
    """


def assert_no_leakage(
    feature_columns: tuple[str, ...] = FEATURE_COLUMNS,
    label_columns: tuple[str, ...] = LABEL_COLUMNS,
) -> None:
    overlap = set(feature_columns) & set(label_columns)
    if overlap:
        msg = f"label columns present in the feature group: {sorted(overlap)}"
        raise LeakageError(msg)


@dataclass(slots=True)
class DatasetManifest:
    """Everything needed to interpret an exported file. No credentials."""

    dataset_version: str
    created_at: datetime
    horizon: str
    row_count: int
    symbols: list[str]
    date_range: tuple[str | None, str | None]
    feature_columns: list[str]
    label_columns: list[str]
    context_columns: list[str]
    excluded: dict[str, int]
    feature_set_versions: list[str]
    signal_model_versions: list[str]
    scanner_policy_versions: list[str]
    cost_model_version: str
    label_policy_version: str
    filters: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "created_at": self.created_at.isoformat(),
            "horizon": self.horizon,
            "row_count": self.row_count,
            "symbols": self.symbols,
            "date_range": {"first": self.date_range[0], "last": self.date_range[1]},
            "columns": {
                "identity": list(IDENTITY_COLUMNS),
                "features": self.feature_columns,
                "labels": self.label_columns,
                "context": self.context_columns,
            },
            "excluded_rows": self.excluded,
            "versions": {
                "feature_set": self.feature_set_versions,
                "signal_model": self.signal_model_versions,
                "scanner_policy": self.scanner_policy_versions,
                "cost_model": self.cost_model_version,
                "label_policy": self.label_policy_version,
            },
            "filters": self.filters,
        }

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n")
        return path


@dataclass(slots=True)
class ExportResult:
    frame: pl.DataFrame
    manifest: DatasetManifest
    data_path: Path | None = None
    manifest_path: Path | None = None


async def build_dataset(
    session: AsyncSession,
    *,
    horizon: Horizon,
    backtest_run_id: int | None = None,
    include_backtest: bool = True,
    regular_session_only: bool = True,
    complete_only: bool = True,
    exclude_unreliable_spread: bool = True,
) -> ExportResult:
    """Assemble one horizon's dataset as a Polars frame.

    Deterministic: rows are ordered by ``(reference_timestamp, evaluation_id)``,
    and the column order is fixed by the declared groups. Two exports of the same
    database therefore produce byte-identical data, which is what makes the
    reproducibility test possible.
    """
    assert_no_leakage()

    stmt = (
        select(SignalEvaluation, SignalOutcome, Instrument, WatchlistEntry.tags)
        .join(SignalOutcome, SignalOutcome.evaluation_id == SignalEvaluation.id)
        .join(Instrument, Instrument.id == SignalEvaluation.instrument_id)
        .outerjoin(WatchlistEntry, WatchlistEntry.instrument_id == SignalEvaluation.instrument_id)
        .where(SignalOutcome.horizon == horizon.value)
        .order_by(SignalOutcome.reference_timestamp, SignalEvaluation.id)
    )
    if backtest_run_id is not None:
        stmt = stmt.where(SignalEvaluation.backtest_run_id == backtest_run_id)
    elif not include_backtest:
        stmt = stmt.where(SignalEvaluation.backtest_run_id.is_(None))

    rows = (await session.execute(stmt)).all()

    excluded: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    for evaluation, outcome, instrument, tags in rows:
        if complete_only and outcome.status != LabelStatus.COMPLETE.value:
            excluded[outcome.status] = excluded.get(outcome.status, 0) + 1
            continue
        if regular_session_only and evaluation.session_phase != "REGULAR":
            key = f"session_{evaluation.session_phase}"
            excluded[key] = excluded.get(key, 0) + 1
            continue
        if exclude_unreliable_spread and _spread_is_implausible(evaluation.spread_bps):
            excluded["suspicious_spread"] = excluded.get("suspicious_spread", 0) + 1
            continue
        if outcome.barrier_outcome == "AMBIGUOUS_SAME_BAR":
            # Kept in the dataset but counted: the row is real, its barrier
            # ordering is not knowable, and a study of barrier outcomes must be
            # able to see how many rows rest on that.
            excluded["ambiguous_same_bar_retained"] = (
                excluded.get("ambiguous_same_bar_retained", 0) + 1
            )

        records.append(
            _record(
                evaluation=evaluation,
                outcome=outcome,
                instrument=instrument,
                tags=tags,
                horizon=horizon,
            )
        )

    frame = pl.DataFrame(records) if records else pl.DataFrame(schema=_empty_schema())
    frame = frame.select(_column_order())

    manifest = DatasetManifest(
        dataset_version=DATASET_VERSION,
        created_at=utc_now(),
        horizon=horizon.value,
        row_count=frame.height,
        symbols=sorted({str(record["symbol"]) for record in records}),
        date_range=_date_range(records),
        feature_columns=list(FEATURE_COLUMNS),
        label_columns=list(LABEL_COLUMNS),
        context_columns=list(CONTEXT_COLUMNS),
        excluded=dict(sorted(excluded.items())),
        feature_set_versions=sorted({str(r["feature_set_version"]) for r in records}),
        signal_model_versions=sorted({str(r["signal_model_version"]) for r in records}),
        scanner_policy_versions=sorted({str(r["scanner_policy_version"]) for r in records}),
        cost_model_version=COST_MODEL_VERSION,
        label_policy_version=LABEL_POLICY_VERSION,
        filters={
            "backtest_run_id": backtest_run_id,
            "include_backtest": include_backtest,
            "regular_session_only": regular_session_only,
            "complete_only": complete_only,
            "exclude_unreliable_spread": exclude_unreliable_spread,
        },
    )
    return ExportResult(frame=frame, manifest=manifest)


def write_dataset(
    result: ExportResult, *, directory: Path, stem: str, fmt: str = "parquet"
) -> ExportResult:
    """Write the frame and its manifest side by side.

    Parquet is the canonical format: it preserves dtypes and nulls, which CSV
    cannot -- a null label round-trips through CSV as an empty string and then as
    ``0.0``, silently turning "we do not know yet" into "the market did nothing".
    CSV remains available for inspection.
    """
    directory.mkdir(parents=True, exist_ok=True)
    suffix = "parquet" if fmt == "parquet" else "csv"
    data_path = directory / f"{stem}.{suffix}"

    if fmt == "parquet":
        result.frame.write_parquet(data_path)
    else:
        result.frame.write_csv(data_path)

    manifest_path = result.manifest.write(directory / f"{stem}.manifest.json")
    result.data_path = data_path
    result.manifest_path = manifest_path
    return result


def _record(
    *,
    evaluation: SignalEvaluation,
    outcome: SignalOutcome,
    instrument: Instrument,
    tags: Any,
    horizon: Horizon,
) -> dict[str, Any]:
    features = _feature_view(evaluation)
    reference = outcome.reference_timestamp

    record: dict[str, Any] = {
        "evaluation_id": evaluation.id,
        "symbol": instrument.symbol,
        "sector": _sector_of(tags),
        "reference_timestamp": reference,
    }
    record.update({name: features.get(name) for name in FEATURE_COLUMNS})
    record.update(
        {
            "raw_return": outcome.raw_return,
            "mfe": outcome.mfe,
            "mae": outcome.mae,
            "barrier_outcome": outcome.barrier_outcome,
            "time_to_target_seconds": outcome.time_to_target_seconds,
            "time_to_stop_seconds": outcome.time_to_stop_seconds,
            "future_price": float(outcome.future_price) if outcome.future_price else None,
            "future_timestamp": outcome.future_timestamp,
            "label_status": outcome.status,
            "horizon": horizon.value,
            "session_phase": evaluation.session_phase,
            "data_quality": evaluation.data_quality,
            "label_timeframe": outcome.label_timeframe,
            "label_end_timestamp": outcome.future_timestamp,
            "session_date": reference.date().isoformat(),
            "qualified": evaluation.qualified,
            "classification": evaluation.classification,
            "direction": evaluation.direction,
            "tracked_signal_id": evaluation.tracked_signal_id,
            "backtest_run_id": evaluation.backtest_run_id,
            "feature_set_version": evaluation.feature_set_version,
            "signal_model_version": evaluation.signal_model_version,
            "scanner_policy_version": evaluation.scanner_policy_version,
            "label_policy_version": outcome.label_policy_version,
        }
    )
    return record


def _column_order() -> list[str]:
    return [*IDENTITY_COLUMNS, *FEATURE_COLUMNS, *LABEL_COLUMNS, *CONTEXT_COLUMNS]


def _empty_schema() -> dict[str, Any]:
    """A typed empty frame, so an export with no rows still has the columns."""
    schema: dict[str, Any] = {
        "evaluation_id": pl.Int64,
        "symbol": pl.Utf8,
        "sector": pl.Utf8,
        "reference_timestamp": pl.Datetime(time_zone="UTC"),
    }
    for name in FEATURE_COLUMNS:
        schema[name] = pl.Float64
    schema.update(
        {
            "raw_return": pl.Float64,
            "mfe": pl.Float64,
            "mae": pl.Float64,
            "barrier_outcome": pl.Utf8,
            "time_to_target_seconds": pl.Float64,
            "time_to_stop_seconds": pl.Float64,
            "future_price": pl.Float64,
            "future_timestamp": pl.Datetime(time_zone="UTC"),
            "label_status": pl.Utf8,
            "horizon": pl.Utf8,
            "session_phase": pl.Utf8,
            "data_quality": pl.Utf8,
            "label_timeframe": pl.Utf8,
            "label_end_timestamp": pl.Datetime(time_zone="UTC"),
            "session_date": pl.Utf8,
            "qualified": pl.Boolean,
            "classification": pl.Utf8,
            "direction": pl.Int64,
            "tracked_signal_id": pl.Int64,
            "backtest_run_id": pl.Int64,
            "feature_set_version": pl.Utf8,
            "signal_model_version": pl.Utf8,
            "scanner_policy_version": pl.Utf8,
            "label_policy_version": pl.Utf8,
        }
    )
    return schema


def _spread_is_implausible(spread_bps: float | None) -> bool:
    return spread_bps is not None and (spread_bps < 0 or spread_bps > SUSPICIOUS_SPREAD_BPS)


def _date_range(records: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    if not records:
        return None, None
    stamps = sorted(str(record["reference_timestamp"]) for record in records)
    return stamps[0], stamps[-1]
