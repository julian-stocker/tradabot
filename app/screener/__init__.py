"""Deterministic company discovery over Tradabot's canonical layers.

Answers *which covered companies satisfy these stated conditions*. It computes
no financial quantity of its own, ranks nothing by desirability, and returns a
match as a fact about a company's filings rather than an opinion about its
shares.
"""

from app.screener.registry import METRICS, ScreenMetric, describe, get, keys
from app.screener.schemas import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cost,
    Criterion,
    CriterionResult,
    Evaluation,
    NotEvaluable,
    Operator,
    Scope,
    ScreenCandidate,
    ScreenResult,
)
from app.screener.service import InvalidCriterionError, ScreenerService

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "METRICS",
    "Cost",
    "Criterion",
    "CriterionResult",
    "Evaluation",
    "InvalidCriterionError",
    "NotEvaluable",
    "Operator",
    "Scope",
    "ScreenCandidate",
    "ScreenMetric",
    "ScreenResult",
    "ScreenerService",
    "describe",
    "get",
    "keys",
]
