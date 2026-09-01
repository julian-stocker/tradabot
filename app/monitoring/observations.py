"""Turning raw inputs into the small set of facts the detectors compare.

Separated from detection on purpose. An observation is "what is true now",
computed from prices, filings, portfolios and data health. A detector is "how
this differs from last time", and it sees nothing but two observations.

That split is what makes the whole engine testable without a database, a broker
or a network: a detector test hands over two dictionaries. It is also what makes
the historical replay honest -- the replay builds observations at a past
``as_of`` using only data available then, and the detectors cannot tell the
difference because they never see the underlying data at all.

Every observation is computed from data at or before ``as_of``. Nothing here
reads a forward return, and nothing may: a monitoring layer that knew what
happened next would be an alpha model, and this repository has established that
it has no validated one.
"""

from __future__ import annotations

import math
import statistics as st
from collections.abc import Mapping, Sequence
from typing import Any

_TRADING_DAYS = 252
_VOLUME_WINDOW = 20
_VOL_SHORT = 20
_VOL_LONG = 252
_MA_LONG = 200
_YEAR = 252
_MIN_HISTORY = 60
_MIN_RETURNS_FOR_VOL = 2
_MIN_SECTOR_MEMBERS = 3


def _returns(closes: Sequence[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1] > 0]


def _annualised(returns: Sequence[float]) -> float | None:
    if len(returns) < _MIN_RETURNS_FOR_VOL:
        return None
    return st.pstdev(returns) * math.sqrt(_TRADING_DAYS)


class Bars:
    """Split-adjusted closes and volumes for one symbol, indexed by date.

    Slicing is always "at or before ``as_of``", so a caller cannot accidentally
    reach past the point being simulated.
    """

    def __init__(self, closes: Mapping[str, float], volumes: Mapping[str, float]) -> None:
        self._closes = closes
        self._volumes = volumes
        self._dates = sorted(closes)

    def upto(self, as_of: str) -> list[str]:
        return [d for d in self._dates if d <= as_of]

    def closes(self, as_of: str, sessions: int) -> list[float]:
        days = self.upto(as_of)[-sessions:]
        return [self._closes[d] for d in days]

    def volumes(self, as_of: str, sessions: int) -> list[float]:
        days = self.upto(as_of)[-sessions:]
        return [self._volumes.get(d, 0.0) for d in days]

    def has(self, as_of: str, sessions: int) -> bool:
        return len(self.upto(as_of)) >= sessions

    def as_closes(self) -> Mapping[str, float]:
        """The close series, for callers that take a plain date-to-price map."""
        return self._closes


def market_observation(bars: Bars, as_of: str, *, trend_band: float) -> dict[str, Any]:
    """The benchmark's regime, and the measurements behind it.

    Three states, derived from where the index sits relative to its 200-day
    average with a dead band around it. The band is what stops an index drifting
    along its average from reporting a new regime every few sessions.
    """
    if not bars.has(as_of, _MA_LONG + 1):
        return {"regime": "INSUFFICIENT_HISTORY"}
    closes = bars.closes(as_of, _MA_LONG + 1)
    ma = st.mean(closes[-_MA_LONG:])
    distance = closes[-1] / ma - 1
    regime = (
        "TRENDING_UP"
        if distance > trend_band
        else "TRENDING_DOWN"
        if distance < -trend_band
        else "RANGE_BOUND"
    )
    window = bars.closes(as_of, _VOL_LONG + 1)
    short = _annualised(_returns(window[-(_VOL_SHORT + 1) :]))
    long = _annualised(_returns(window))
    return {
        "regime": regime,
        "distance_from_ma200": round(distance, 4),
        "volatility_20d": round(short, 4) if short is not None else None,
        "volatility_252d": round(long, 4) if long is not None else None,
        "as_of": as_of,
    }


def symbol_observation(bars: Bars, benchmark: Bars, as_of: str) -> dict[str, Any] | None:
    """Volume, volatility and relative strength for one symbol.

    Returns ``None`` when there is too little history to say anything, rather
    than a dictionary of nulls that a detector would have to keep checking.
    """
    if not bars.has(as_of, _MIN_HISTORY):
        return None
    closes = bars.closes(as_of, _VOL_LONG + 1)
    volumes = bars.volumes(as_of, _VOLUME_WINDOW + 1)

    volume_ratio = None
    if len(volumes) > _VOLUME_WINDOW:
        baseline = st.median(volumes[:-1])
        if baseline > 0:
            volume_ratio = volumes[-1] / baseline

    short = _annualised(_returns(closes[-(_VOL_SHORT + 1) :]))
    long = _annualised(_returns(closes))
    volatility_ratio = short / long if short is not None and long not in (None, 0) else None

    relative_strength = None
    if bars.has(as_of, _YEAR + 1) and benchmark.has(as_of, _YEAR + 1):
        own = bars.closes(as_of, _YEAR + 1)
        market = benchmark.closes(as_of, _YEAR + 1)
        if own[0] > 0 and market[0] > 0:
            relative_strength = (own[-1] / own[0] - 1) - (market[-1] / market[0] - 1)

    return {
        "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
        "volatility_ratio": (round(volatility_ratio, 3) if volatility_ratio is not None else None),
        "volatility_20d": round(short, 4) if short is not None else None,
        "relative_strength_252d": (
            round(relative_strength, 4) if relative_strength is not None else None
        ),
        "as_of": as_of,
    }


def sector_observation(
    members: Mapping[str, Sequence[str]], bars: Mapping[str, Bars], as_of: str
) -> dict[str, dict[str, Any]]:
    """Five-session return per sector, equally weighted across its members.

    Equal weighting rather than capitalisation weighting because Tradabot holds
    no share counts for most of the universe, and a sector move dominated by one
    mega-cap is a fact about that company, not the sector.
    """
    out: dict[str, dict[str, Any]] = {}
    for sector, symbols in members.items():
        moves: list[float] = []
        for symbol in symbols:
            series = bars.get(symbol)
            if series is None or not series.has(as_of, 6):
                continue
            closes = series.closes(as_of, 6)
            if closes[0] > 0:
                moves.append(closes[-1] / closes[0] - 1)
        if len(moves) >= _MIN_SECTOR_MEMBERS:
            out[sector] = {
                "return_5d": round(st.mean(moves), 4),
                "members_used": len(moves),
                "as_of": as_of,
            }
    return out


def company_observation(context: Any, filings: dict[str, Any] | None) -> dict[str, Any]:
    """One company's reported state, read off Advisor output.

    Nothing is derived here. Every figure is one the Advisor already computed;
    this only selects which of them the monitor watches for change.
    """
    labels = dict(getattr(context, "labels", {}) or {})
    observation: dict[str, Any] = {
        "available": bool(getattr(context, "available", False)),
        "valuation_context": getattr(context, "valuation_context", None),
        "valuation_ps": getattr(context, "valuation_value", None),
        "confidence": getattr(context, "confidence", "INSUFFICIENT"),
        "balance_sheet": labels.get("assessment"),
        "dilution": labels.get("dilution"),
        "revenue_basis": labels.get("revenue_basis"),
    }
    for name, value in (getattr(context, "metrics", {}) or {}).items():
        observation[f"metric_{name}"] = value
    if filings:
        observation.update(
            {
                "latest_accession": filings.get("accession"),
                "latest_form": filings.get("form"),
                "latest_filed": filings.get("filed"),
            }
        )
    return observation


def portfolio_observation(report: Any) -> dict[str, Any]:
    """One account's shape, read off a Portfolio Fit report."""
    exposure = report.exposure
    risk = report.risk
    return {
        "equity": round(exposure.equity, 2),
        "cash_pct": round(exposure.cash_pct, 4),
        "invested_pct": round(exposure.invested_pct, 4),
        "positions": sorted(exposure.weights),
        "weights": {s: round(w, 4) for s, w in sorted(exposure.weights.items())},
        "sector_weights": {s: round(w, 4) for s, w in sorted(exposure.sector_weights.items())},
        "top3_pct": round(exposure.top3_pct, 4),
        "concentration": str(exposure.concentration),
        "average_correlation": (
            round(risk.average_correlation, 4) if risk.average_correlation is not None else None
        ),
        "annualised_volatility": (
            round(risk.annualised_volatility, 4) if risk.annualised_volatility is not None else None
        ),
    }


def health_observation(health: Any) -> dict[str, Any]:
    """The fact store's own state, as the durability layer reports it."""
    return {
        "status": str(health.status),
        "rows": health.rows,
        "symbols": health.symbols,
        "newest_filed": health.newest_filed,
        "age_days": health.age_days,
    }
