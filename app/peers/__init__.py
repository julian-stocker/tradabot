"""Cross-sectional fundamental context.

Answers "how does this company currently compare with genuinely comparable
companies on the dimensions Tradabot can measure safely" -- and refuses when
the comparison would not be genuine.

Deliberately has no broker, publishing or Discord dependency: Discord is one
consumer of this layer, not its owner. A structural test asserts the boundary.
"""

from app.peers.schemas import (
    V1_METRICS,
    MetricComparison,
    MetricRefusal,
    MetricSpec,
    PeerBasis,
    PeerComparison,
    PeerGroup,
    PeerMember,
    PeerOutcome,
)
from app.peers.service import PeerComparisonService, describe
from app.peers.statistics import MIN_PEERS, percentile_rank, quantile, usable
from app.peers.universe import PeerCompany, PeerUniverse, load

__all__ = [
    "MIN_PEERS",
    "V1_METRICS",
    "MetricComparison",
    "MetricRefusal",
    "MetricSpec",
    "PeerBasis",
    "PeerCompany",
    "PeerComparison",
    "PeerComparisonService",
    "PeerGroup",
    "PeerMember",
    "PeerOutcome",
    "PeerUniverse",
    "describe",
    "load",
    "percentile_rank",
    "quantile",
    "usable",
]
