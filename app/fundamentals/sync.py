"""Rebuilding ``data/sec_facts.parquet`` from EDGAR, repeatably.

Why this module exists
----------------------
The fact store the Advisor depends on was originally produced by a research
script writing into ``reports/``. When that directory was deleted the Advisor
lost its data source, and the only surviving copy was a cache in a
session-scoped temporary directory. It was recoverable that day and would not
have been the next.

So the ingestion lives here, in the application, and its inputs are things that
survive: the local instruments database for the symbol universe, a cache under
``data/`` for the raw payloads, and EDGAR itself.

Three properties are load-bearing
---------------------------------
**Idempotent.** Running twice produces the same file. Rows are sorted by a
stable key before writing, so a rebuild is byte-comparable rather than merely
equivalent.

**Resumable.** Each symbol's filtered payload is cached on disk the moment it
arrives. An interrupted sync of a thousand companies resumes from where it
stopped instead of re-downloading gigabytes.

**Fail-soft per symbol.** One company's outage is recorded and skipped. It never
aborts the run, because a partial store that knows what it is missing is more
useful than no store at all.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from app.core.logging import get_logger
from app.fundamentals.client import EdgarClient, EdgarUnavailableError
from app.fundamentals.concepts import (
    CONCEPTS,
    FACT_COLUMNS,
    METRIC_BY_CONCEPT,
    TAXONOMIES,
)

logger = get_logger(__name__)

DEFAULT_STORE: Path = Path("data/sec_facts.parquet")
DEFAULT_CACHE: Path = Path("data/sec_cache")
DEFAULT_MAX_AGE_DAYS: int = 7

_SCHEMA: dict[str, Any] = {
    "symbol": pl.String,
    "cik": pl.Int64,
    "metric": pl.String,
    "concept": pl.String,
    "taxonomy": pl.String,
    "unit": pl.String,
    "value": pl.Float64,
    "form": pl.String,
    "filed": pl.String,
    "accepted": pl.String,
    "accession": pl.String,
    "fy": pl.Int64,
    "fp": pl.String,
    "period_start": pl.String,
    "period_end": pl.String,
}
"""Declared rather than inferred. ``period_start`` is null for every instant
fact, so inference from the first rows types the column as null and then fails
when the first duration fact arrives."""

_SORT_KEY: tuple[str, ...] = (
    "symbol",
    "metric",
    "concept",
    "unit",
    "period_end",
    "period_start",
    "filed",
    "accession",
)


@dataclass(slots=True)
class SymbolOutcome:
    symbol: str
    cik: int | None
    status: str
    facts: int = 0
    accepted: int = 0
    detail: str | None = None


@dataclass(slots=True)
class SyncOutcome:
    """What one sync run did. Counts, never payloads."""

    requested: int
    written: int
    symbols: int
    output: Path
    from_cache: int = 0
    fetched: int = 0
    failed: int = 0
    unmapped: tuple[str, ...] = ()
    seconds: float = 0.0
    per_symbol: list[SymbolOutcome] = field(default_factory=list)


def cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol.upper()}.json"


def _fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    if max_age_days <= 0:
        return True
    age = time.time() - path.stat().st_mtime
    return age <= max_age_days * 86_400


def _extract(payload: dict[str, Any], accepted: dict[str, str]) -> list[dict[str, Any]]:
    """Flatten one companyfacts document to the ingested concepts only.

    Discarding everything else at the cache boundary is what keeps the cache
    small enough to live in the repository's data directory: a full
    companyfacts document is tens of megabytes, and Tradabot uses a few dozen
    concepts of it.
    """
    facts = payload.get("facts", {})
    rows: list[dict[str, Any]] = []
    for taxonomy in TAXONOMIES:
        for concept, body in facts.get(taxonomy, {}).items():
            metric = METRIC_BY_CONCEPT.get(concept)
            if metric is None:
                continue
            for unit, entries in (body.get("units") or {}).items():
                for entry in entries:
                    accession = entry.get("accn")
                    value = entry.get("val")
                    filed = entry.get("filed")
                    end = entry.get("end")
                    # An entry without an accession cannot be attributed to a
                    # filing, and one without a filing date cannot be made
                    # point-in-time. Either way it is unusable, not defaulted.
                    if not accession or value is None or not filed or not end:
                        continue
                    rows.append(
                        {
                            "metric": metric,
                            "concept": concept,
                            "taxonomy": taxonomy,
                            "unit": str(unit),
                            "value": float(value),
                            "form": entry.get("form"),
                            "filed": str(filed),
                            "accepted": accepted.get(str(accession)),
                            "accession": str(accession),
                            "fy": entry.get("fy"),
                            "fp": entry.get("fp"),
                            "period_start": entry.get("start"),
                            "period_end": str(end),
                        }
                    )
    return rows


def _load_symbol(
    symbol: str,
    cik: int,
    *,
    client: EdgarClient,
    cache_dir: Path,
    max_age_days: int,
    force: bool,
) -> tuple[SymbolOutcome, list[dict[str, Any]]]:
    path = cache_path(cache_dir, symbol)
    if not force and _fresh(path, max_age_days):
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("discarding unreadable cache entry", symbol=symbol,
                           reason=type(exc).__name__)
        else:
            rows = list(cached.get("facts", []))
            return (
                SymbolOutcome(
                    symbol,
                    cik,
                    "CACHED",
                    len(rows),
                    sum(1 for r in rows if r.get("accepted")),
                ),
                rows,
            )

    try:
        payload = client.companyfacts(cik)
    except EdgarUnavailableError as exc:
        return SymbolOutcome(symbol, cik, "UNAVAILABLE", detail=str(exc)), []
    try:
        accepted = client.acceptance_times(cik)
    except EdgarUnavailableError as exc:
        # Acceptance timestamps are provenance detail. Losing them degrades the
        # record; losing the facts would break the Advisor, so the facts win.
        logger.warning("acceptance times unavailable", symbol=symbol, reason=str(exc))
        accepted = {}

    rows = _extract(payload, accepted)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.partial")
    tmp.write_text(
        json.dumps({"symbol": symbol, "cik": cik, "facts": rows}, separators=(",", ":")),
        encoding="utf-8",
    )
    # Atomic replace: an interrupted write must not leave a half-parsed cache
    # entry that a later run would trust.
    tmp.replace(path)
    return (
        SymbolOutcome(
            symbol, cik, "FETCHED", len(rows), sum(1 for r in rows if r.get("accepted"))
        ),
        rows,
    )


def sync_facts(
    symbols: Sequence[str],
    *,
    client: EdgarClient | None = None,
    output: Path = DEFAULT_STORE,
    cache_dir: Path = DEFAULT_CACHE,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    force: bool = False,
    tickers: dict[str, int] | None = None,
    progress: Callable[[int, int, SymbolOutcome], None] | None = None,
) -> SyncOutcome:
    """Rebuild the persisted fact store for ``symbols``.

    Args:
        symbols: tickers to ingest. Order does not affect the output.
        client: EDGAR reader. Constructed if omitted.
        output: parquet path to write.
        cache_dir: durable per-symbol cache, under ``data/`` by default.
        max_age_days: reuse a cache entry younger than this without a request.
        force: re-fetch every symbol regardless of cache age.
        tickers: ticker -> CIK map, fetched from EDGAR if omitted.
        progress: called after each symbol, for CLI reporting.

    Returns:
        Counts describing the run. Never file contents, never credentials.
    """
    started = time.monotonic()
    client = client or EdgarClient()
    wanted = sorted({s.upper().replace(".", "-") for s in symbols if s})
    lookup = tickers if tickers is not None else client.company_tickers()

    resolved = [(s, lookup[s]) for s in wanted if s in lookup]
    unmapped = tuple(s for s in wanted if s not in lookup)

    rows: list[dict[str, Any]] = []
    outcomes: list[SymbolOutcome] = []
    for index, (symbol, cik) in enumerate(resolved, start=1):
        outcome, symbol_rows = _load_symbol(
            symbol,
            cik,
            client=client,
            cache_dir=cache_dir,
            max_age_days=max_age_days,
            force=force,
        )
        for row in symbol_rows:
            rows.append({**row, "symbol": symbol, "cik": cik})
        outcomes.append(outcome)
        if progress is not None:
            progress(index, len(resolved), outcome)

    frame = _frame(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".parquet.partial")
    frame.write_parquet(tmp)
    tmp.replace(output)

    result = SyncOutcome(
        requested=len(wanted),
        written=frame.height,
        symbols=int(frame["symbol"].n_unique()) if frame.height else 0,
        output=output,
        from_cache=sum(1 for o in outcomes if o.status == "CACHED"),
        fetched=sum(1 for o in outcomes if o.status == "FETCHED"),
        failed=sum(1 for o in outcomes if o.status == "UNAVAILABLE"),
        unmapped=unmapped,
        seconds=time.monotonic() - started,
        per_symbol=outcomes,
    )
    logger.info(
        "sec fact store written",
        rows=result.written,
        symbols=result.symbols,
        fetched=result.fetched,
        cached=result.from_cache,
        failed=result.failed,
    )
    return result


def _frame(rows: Iterable[dict[str, Any]]) -> pl.DataFrame:
    """Assemble the canonical frame: fixed columns, fixed types, fixed order."""
    materialised = list(rows)
    if not materialised:
        return pl.DataFrame(schema=_SCHEMA)
    frame = pl.from_dicts(materialised, schema=_SCHEMA)
    return frame.select(FACT_COLUMNS).sort(_SORT_KEY, nulls_last=True)


__all__ = [
    "CONCEPTS",
    "DEFAULT_CACHE",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_STORE",
    "SymbolOutcome",
    "SyncOutcome",
    "sync_facts",
]
