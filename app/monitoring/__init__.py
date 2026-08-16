"""Read-only market and portfolio monitoring.

Decides **what is worth reporting**, and nothing else. It observes the Advisor,
Portfolio Fit, market data, the SEC fact store and read-only account snapshots,
compares each against the previous run, and emits a ranked list of material
changes -- or, far more often, nothing at all.

It has no transport. Delivering a message is a separate concern with its own
credentials, retries and failure modes, and mixing it in here would make the
materiality rules untestable without a network.

It expresses no action. Every event describes a transition that occurred; none
recommends a response, because no validated predictive evidence in this
repository supports one.
"""

from app.monitoring.digest import Digest, DigestSection
from app.monitoring.digest import build as build_digest
from app.monitoring.engine import (
    MonitoringEngine,
    MonitoringInputs,
    is_quiet,
    rank,
)
from app.monitoring.journal import EventJournal
from app.monitoring.observations import Bars
from app.monitoring.schemas import (
    ChangeEvent,
    EventConfidence,
    EventKind,
    Evidence,
    Materiality,
    MonitoringRun,
    Provenance,
    Scope,
    ScopeKind,
    weakest,
)
from app.monitoring.state import (
    InMemoryStateStore,
    JsonStateStore,
    MonitorStateStore,
    StoredState,
)

__all__ = [
    "Bars",
    "ChangeEvent",
    "Digest",
    "DigestSection",
    "EventConfidence",
    "EventJournal",
    "EventKind",
    "Evidence",
    "InMemoryStateStore",
    "JsonStateStore",
    "Materiality",
    "MonitorStateStore",
    "MonitoringEngine",
    "MonitoringInputs",
    "MonitoringRun",
    "Provenance",
    "Scope",
    "ScopeKind",
    "StoredState",
    "build_digest",
    "is_quiet",
    "rank",
    "weakest",
]
