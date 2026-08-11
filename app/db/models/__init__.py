"""ORM models.

Importing this package registers every model on ``Base.metadata``, which Alembic
autogenerate and ``create_all`` both rely on.
"""

from app.db.models.candle import Candle
from app.db.models.corporate_action import CorporateActionRow
from app.db.models.instrument import Instrument
from app.db.models.notification import NotificationAttempt, NotificationState
from app.db.models.paper import (
    DecisionOutcome,
    PortfolioSnapshot,
    VirtualOrder,
    VirtualPortfolio,
    VirtualPosition,
    VirtualTrade,
)
from app.db.models.signal import SignalRow
from app.db.models.simulation import BrokerCostProfile, RiskProfile, SimulationProfile
from app.db.models.trade_decision import TradeDecisionRow

__all__ = [
    "BrokerCostProfile",
    "Candle",
    "CorporateActionRow",
    "DecisionOutcome",
    "Instrument",
    "NotificationAttempt",
    "NotificationState",
    "PortfolioSnapshot",
    "RiskProfile",
    "SignalRow",
    "SimulationProfile",
    "TradeDecisionRow",
    "VirtualOrder",
    "VirtualPortfolio",
    "VirtualPosition",
    "VirtualTrade",
]
