"""ORM models.

Importing this package registers every model on ``Base.metadata``, which Alembic
autogenerate and ``create_all`` both rely on.
"""

from app.db.models.candle import Candle
from app.db.models.corporate_action import CorporateActionRow
from app.db.models.instrument import Instrument
from app.db.models.notification import NotificationAttempt, NotificationState
from app.db.models.options import OptionQuoteSnapshot, OptionSurfaceSnapshot
from app.db.models.ownership import ExternalAccountConnection, TradabotUser
from app.db.models.paper import (
    DecisionOutcome,
    PortfolioSnapshot,
    VirtualOrder,
    VirtualPortfolio,
    VirtualPosition,
    VirtualTrade,
)
from app.db.models.research import BacktestRun, SignalOutcome, TradeOutcome
from app.db.models.scanner import (
    ScanRun,
    SignalEvaluation,
    TrackedSignal,
    WatchlistEntry,
)
from app.db.models.signal import SignalRow
from app.db.models.simulation import BrokerCostProfile, RiskProfile, SimulationProfile
from app.db.models.trade_decision import TradeDecisionRow

__all__ = [
    "BacktestRun",
    "BrokerCostProfile",
    "Candle",
    "CorporateActionRow",
    "DecisionOutcome",
    "ExternalAccountConnection",
    "Instrument",
    "NotificationAttempt",
    "NotificationState",
    "OptionQuoteSnapshot",
    "OptionSurfaceSnapshot",
    "PortfolioSnapshot",
    "RiskProfile",
    "ScanRun",
    "SignalEvaluation",
    "SignalOutcome",
    "SignalRow",
    "SimulationProfile",
    "TrackedSignal",
    "TradabotUser",
    "TradeDecisionRow",
    "TradeOutcome",
    "VirtualOrder",
    "VirtualPortfolio",
    "VirtualPosition",
    "VirtualTrade",
    "WatchlistEntry",
]
