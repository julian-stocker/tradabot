"""Durable SEC fundamentals ingestion.

Owns the production fact store at ``data/sec_facts.parquet``: how it is built,
how it is checked, and which symbols belong in it. The Advisor reads that file
through :class:`~app.advisor.facts.FactStore` and never fetches on demand, so
this package is the only thing in Tradabot that talks to EDGAR.

The store is **production data, not a research artifact.** It is rebuildable
from EDGAR at any time with ``tradabot fundamentals sync``, and it depends on
nothing under ``reports/``.
"""

from app.fundamentals.client import EdgarClient, EdgarUnavailableError
from app.fundamentals.concepts import CONCEPTS, FACT_COLUMNS, SCHEMA_VERSION
from app.fundamentals.store import (
    STALE_AFTER_DAYS,
    FactStoreHealth,
    FactStoreStatus,
    health,
    schema_hash,
)
from app.fundamentals.sync import (
    DEFAULT_CACHE,
    DEFAULT_STORE,
    SymbolOutcome,
    SyncOutcome,
    sync_facts,
)
from app.fundamentals.universe import database_path, universe_symbols

__all__ = [
    "CONCEPTS",
    "DEFAULT_CACHE",
    "DEFAULT_STORE",
    "FACT_COLUMNS",
    "SCHEMA_VERSION",
    "STALE_AFTER_DAYS",
    "EdgarClient",
    "EdgarUnavailableError",
    "FactStoreHealth",
    "FactStoreStatus",
    "SymbolOutcome",
    "SyncOutcome",
    "database_path",
    "health",
    "schema_hash",
    "sync_facts",
    "universe_symbols",
]
