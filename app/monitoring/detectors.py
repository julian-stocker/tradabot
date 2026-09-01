"""Comparing two observations and naming what changed.

Every function here is pure: two dictionaries in, a list of events out. No
prices, no filings, no broker, no clock beyond the timestamp it is handed. That
is what makes the materiality rules auditable -- there is nowhere for a hidden
input to enter.

The first observation is never an event
---------------------------------------
When there is no previous state, a detector records the baseline and reports
nothing. A fresh install would otherwise announce the current state of every
subject it can see, which is the single loudest thing this engine could do and
carries no information at all: none of it *changed*.

The one exception is a discrete arrival -- a filing that was not in the previous
snapshot is news whether or not a baseline existed for the other fields. Even
there, a missing baseline suppresses it, because "every filing this company has
ever made" is not what a first run should say.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.monitoring import materiality as rules
from app.monitoring.schemas import (
    ChangeEvent,
    EventConfidence,
    EventKind,
    Evidence,
    Materiality,
    Provenance,
    Scope,
    ScopeKind,
)

PRICE_SOURCE = "local candles (split adjusted)"
ADVISOR_SOURCE = "production Advisor"
PORTFOLIO_SOURCE = "Portfolio Fit over a read-only account snapshot"
HEALTH_SOURCE = "fact store health"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _relative_change(previous: float | None, current: float | None) -> float | None:
    if previous is None or current is None or previous == 0:
        return None
    return current / previous - 1


# --------------------------------------------------------------------- market
def detect_market_regime(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
    sessions_in_state: int,
) -> list[ChangeEvent]:
    """A benchmark regime transition that has held long enough to be reported.

    ``sessions_in_state`` is how many consecutive sessions the new regime has
    been true. Requiring several is what separates a turn from a wobble; the
    cost is a delay on genuine turns, which is the cheaper error for a layer
    whose whole purpose is not crying wolf.
    """
    if previous is None:
        return []
    was, is_now = previous.get("regime"), current.get("regime")
    if not is_now or is_now in (was, "INSUFFICIENT_HISTORY"):
        return []
    if sessions_in_state < rules.REGIME_MIN_SESSIONS_IN_STATE:
        return []
    distance = current.get("distance_from_ma200")
    return [
        ChangeEvent(
            kind=EventKind.MARKET_REGIME_CHANGE,
            occurred_at=now,
            subject="market",
            previous_state=str(was),
            current_state=str(is_now),
            materiality=Materiality.SIGNIFICANT,
            summary=(
                f"The benchmark regime moved from {was} to {is_now}; it is "
                f"{_pct(distance)} from its 200-day average."
            ),
            evidence=(
                Evidence(
                    "distance_from_ma200",
                    previous.get("distance_from_ma200"),
                    distance,
                    threshold=rules.REGIME_TREND_BAND,
                    unit="fraction",
                    comparison="beyond the trend band",
                ),
                Evidence(
                    "sessions_in_new_regime",
                    None,
                    sessions_in_state,
                    threshold=rules.REGIME_MIN_SESSIONS_IN_STATE,
                    comparison="at or above the persistence floor",
                ),
            ),
            confidence=EventConfidence.HIGH,
            provenance=(Provenance(PRICE_SOURCE, as_of, "benchmark 200-day average"),),
            scope=Scope(ScopeKind.MARKET),
            dedup_key=f"MARKET_REGIME:{is_now}",
        )
    ]


def detect_sector_move(
    sector: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
) -> list[ChangeEvent]:
    """A sector's five-session move, when it is large in absolute terms.

    This one is a *level*, not a transition, which is the deliberate exception:
    a large weekly sector move is news each time it happens, and the cooldown
    rather than the previous state is what stops it repeating.
    """
    move = current.get("return_5d")
    if move is None:
        return []
    band = rules.band(move, rules.SECTOR_MOVE_5D, rules.SECTOR_MOVE_5D_SIGNIFICANT)
    if band is Materiality.ROUTINE:
        return []
    direction = "higher" if move > 0 else "lower"
    return [
        ChangeEvent(
            kind=EventKind.SECTOR_MOVE,
            occurred_at=now,
            subject=sector,
            previous_state=(None if previous is None else f"5d {_pct(previous.get('return_5d'))}"),
            current_state=f"5d {_pct(move)}",
            materiality=band,
            summary=(
                f"{sector} moved {_pct(abs(move))} {direction} over five sessions "
                f"across {current.get('members_used')} members."
            ),
            evidence=(
                Evidence(
                    "return_5d",
                    None if previous is None else previous.get("return_5d"),
                    move,
                    unit="fraction",
                    threshold=rules.SECTOR_MOVE_5D,
                    comparison="absolute move over five sessions",
                ),
            ),
            confidence=EventConfidence.MEDIUM,
            provenance=(
                Provenance(
                    PRICE_SOURCE,
                    as_of,
                    f"equal-weighted across {current.get('members_used')} members",
                ),
            ),
            scope=Scope(ScopeKind.SECTOR),
            dedup_key=f"SECTOR_MOVE:{sector}:{'up' if move > 0 else 'down'}",
        )
    ]


def detect_symbol(
    symbol: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
    sector_confidence: EventConfidence = EventConfidence.HIGH,
) -> list[ChangeEvent]:
    """Unusual volume, unusual volatility, and relative-strength crossings."""
    events: list[ChangeEvent] = []
    scope = Scope(ScopeKind.COMPANY)

    volume = current.get("volume_ratio")
    if volume is not None:
        band = rules.band(volume, rules.VOLUME_RATIO_NOTABLE, rules.VOLUME_RATIO_SIGNIFICANT)
        # Only an *elevated* ratio is unusual. A quiet session is common and
        # uninformative, and banding on magnitude alone would report both.
        if band is not Materiality.ROUTINE and volume > 1:
            events.append(
                ChangeEvent(
                    kind=EventKind.UNUSUAL_VOLUME,
                    occurred_at=now,
                    subject=symbol,
                    previous_state=(
                        None if previous is None else f"{previous.get('volume_ratio')}x median"
                    ),
                    current_state=f"{volume:.1f}x median",
                    materiality=band,
                    summary=(f"{symbol} traded {volume:.1f} times its 20-session median volume."),
                    evidence=(
                        Evidence(
                            "volume_ratio",
                            None if previous is None else previous.get("volume_ratio"),
                            volume,
                            unit="x median",
                            threshold=rules.VOLUME_RATIO_NOTABLE,
                            comparison="session volume over 20-session median",
                        ),
                    ),
                    confidence=EventConfidence.HIGH,
                    provenance=(Provenance(PRICE_SOURCE, as_of),),
                    scope=scope,
                    dedup_key=f"UNUSUAL_VOLUME:{symbol}:{band!s}",
                )
            )

    volatility = current.get("volatility_ratio")
    if volatility is not None:
        band = rules.band(
            volatility,
            rules.VOLATILITY_RATIO_NOTABLE,
            rules.VOLATILITY_RATIO_SIGNIFICANT,
        )
        if band is not Materiality.ROUTINE and volatility > 1:
            events.append(
                ChangeEvent(
                    kind=EventKind.UNUSUAL_VOLATILITY,
                    occurred_at=now,
                    subject=symbol,
                    previous_state=(
                        None if previous is None else f"{previous.get('volatility_ratio')}x"
                    ),
                    current_state=f"{volatility:.1f}x",
                    materiality=band,
                    summary=(
                        f"{symbol}'s 20-session volatility is {volatility:.1f} times "
                        f"its one-year level ({_pct(current.get('volatility_20d'))} "
                        f"annualised)."
                    ),
                    evidence=(
                        Evidence(
                            "volatility_ratio",
                            None if previous is None else previous.get("volatility_ratio"),
                            volatility,
                            unit="x one-year",
                            threshold=rules.VOLATILITY_RATIO_NOTABLE,
                            comparison="20-session over 252-session realised volatility",
                        ),
                    ),
                    confidence=EventConfidence.HIGH,
                    provenance=(Provenance(PRICE_SOURCE, as_of),),
                    scope=scope,
                    dedup_key=f"UNUSUAL_VOLATILITY:{symbol}:{band!s}",
                )
            )

    if previous is not None:
        was = previous.get("relative_strength_252d")
        is_now = current.get("relative_strength_252d")
        if was is not None and is_now is not None:
            crossed_up = was <= 0 < is_now and is_now >= rules.RELATIVE_STRENGTH_BAND
            crossed_down = was >= 0 > is_now and is_now <= -rules.RELATIVE_STRENGTH_BAND
            if crossed_up or crossed_down:
                direction = "ahead of" if crossed_up else "behind"
                events.append(
                    ChangeEvent(
                        kind=EventKind.RELATIVE_STRENGTH_CHANGE,
                        occurred_at=now,
                        subject=symbol,
                        previous_state=f"{_pct(was)} vs benchmark",
                        current_state=f"{_pct(is_now)} vs benchmark",
                        materiality=Materiality.NOTABLE,
                        summary=(
                            f"{symbol} moved to {direction} the benchmark over twelve "
                            f"months, from {_pct(was)} to {_pct(is_now)}."
                        ),
                        evidence=(
                            Evidence(
                                "relative_strength_252d",
                                was,
                                is_now,
                                change=is_now - was,
                                unit="fraction",
                                threshold=rules.RELATIVE_STRENGTH_BAND,
                                comparison="crossed zero and cleared the band",
                            ),
                        ),
                        confidence=weakest_of(EventConfidence.HIGH, sector_confidence),
                        provenance=(Provenance(PRICE_SOURCE, as_of),),
                        scope=scope,
                        dedup_key=(
                            f"RELATIVE_STRENGTH:{symbol}:{'ahead' if crossed_up else 'behind'}"
                        ),
                    )
                )
    return events


def weakest_of(*levels: EventConfidence) -> EventConfidence:
    from app.monitoring.schemas import weakest  # noqa: PLC0415 -- avoids a cycle

    return weakest(*levels)


# -------------------------------------------------------------------- company
def detect_company(
    symbol: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
) -> list[ChangeEvent]:
    """Filings, fundamentals, valuation band and Advisor confidence."""
    if previous is None:
        return []
    events: list[ChangeEvent] = []
    scope = Scope(ScopeKind.COMPANY)
    reported_confidence = _confidence_from(current.get("confidence"))

    accession = current.get("latest_accession")
    if accession and accession != previous.get("latest_accession"):
        form = str(current.get("latest_form") or "unknown")
        band = (
            Materiality.SIGNIFICANT
            if form in rules.MATERIAL_FORMS
            else Materiality.NOTABLE
            if form in rules.NOTABLE_FORMS
            else Materiality.ROUTINE
        )
        events.append(
            ChangeEvent(
                kind=EventKind.NEW_SEC_FILING,
                occurred_at=now,
                subject=symbol,
                previous_state=str(previous.get("latest_form") or "none"),
                current_state=form,
                materiality=band,
                summary=(f"{symbol} filed a {form} on {current.get('latest_filed')}."),
                evidence=(
                    Evidence(
                        "accession",
                        previous.get("latest_accession"),
                        accession,
                        comparison="a filing not present at the previous observation",
                    ),
                ),
                confidence=EventConfidence.HIGH,
                provenance=(
                    Provenance(
                        "SEC fact store",
                        as_of,
                        f"accession {accession}",
                    ),
                ),
                scope=scope,
                dedup_key=f"NEW_SEC_FILING:{symbol}:{accession}",
            )
        )

    was_band, is_band = previous.get("valuation_context"), current.get("valuation_context")
    if is_band and was_band and is_band != was_band:
        events.append(
            ChangeEvent(
                kind=EventKind.VALUATION_STATE_CHANGE,
                occurred_at=now,
                subject=symbol,
                previous_state=str(was_band),
                current_state=str(is_band),
                materiality=Materiality.NOTABLE,
                summary=(
                    f"{symbol}'s price-to-sales moved from {was_band} to {is_band} "
                    f"against its own history."
                ),
                evidence=(
                    Evidence(
                        "ps_ttm",
                        previous.get("valuation_ps"),
                        current.get("valuation_ps"),
                        change=_relative_change(
                            previous.get("valuation_ps"), current.get("valuation_ps")
                        ),
                        comparison="band against the company's own history",
                    ),
                ),
                confidence=reported_confidence,
                provenance=(Provenance(ADVISOR_SOURCE, as_of),),
                scope=scope,
                dedup_key=f"VALUATION_STATE:{symbol}",
            )
        )

    was_conf, is_conf = previous.get("confidence"), current.get("confidence")
    if was_conf and is_conf and was_conf != is_conf:
        events.append(
            ChangeEvent(
                kind=EventKind.COMPANY_CONFIDENCE_CHANGE,
                occurred_at=now,
                subject=symbol,
                previous_state=str(was_conf),
                current_state=str(is_conf),
                materiality=Materiality.NOTABLE,
                summary=(
                    f"Company-analysis confidence for {symbol} moved from {was_conf} to {is_conf}."
                ),
                evidence=(Evidence("company_analysis_confidence", was_conf, is_conf),),
                confidence=reported_confidence,
                provenance=(Provenance(ADVISOR_SOURCE, as_of),),
                scope=scope,
                dedup_key=f"COMPANY_CONFIDENCE:{symbol}",
            )
        )

    for name in sorted(
        k
        for k in current
        if k.startswith("metric_") and k.removeprefix("metric_") in rules.FUNDAMENTAL_METRICS
    ):
        was, is_now = previous.get(name), current.get(name)
        change = _relative_change(was, is_now)
        if change is None:
            continue
        band = rules.band(
            change,
            rules.FUNDAMENTAL_CHANGE_NOTABLE,
            rules.FUNDAMENTAL_CHANGE_SIGNIFICANT,
        )
        if band is Materiality.ROUTINE and change == 0:
            continue
        label = name.removeprefix("metric_")
        events.append(
            ChangeEvent(
                kind=EventKind.FUNDAMENTAL_CHANGE,
                occurred_at=now,
                subject=symbol,
                previous_state=f"{label} {was:,.4g}",
                current_state=f"{label} {is_now:,.4g}",
                materiality=band,
                summary=(
                    f"{symbol}'s {label.replace('_', ' ')} moved {_pct(change)} "
                    f"between observations."
                ),
                evidence=(
                    Evidence(
                        label,
                        was,
                        is_now,
                        change=change,
                        unit="relative",
                        threshold=rules.FUNDAMENTAL_CHANGE_NOTABLE,
                        comparison="relative change in a trailing figure",
                    ),
                ),
                confidence=reported_confidence,
                provenance=(Provenance(ADVISOR_SOURCE, as_of),),
                scope=scope,
                dedup_key=f"FUNDAMENTAL:{symbol}:{label}:{band!s}",
            )
        )
    return events


def _confidence_from(value: Any) -> EventConfidence:
    try:
        return EventConfidence(str(value))
    except ValueError:
        return EventConfidence.INSUFFICIENT


# ------------------------------------------------------------------ portfolio
def detect_portfolio(
    account: str,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
    sector_confidence: EventConfidence = EventConfidence.MEDIUM,
) -> list[ChangeEvent]:
    """Everything about one account's shape that moved since the last run."""
    if previous is None:
        return []
    scope = Scope(ScopeKind.PORTFOLIO, account=account)
    source = (Provenance(PORTFOLIO_SOURCE, as_of, f"account {account}"),)
    events = _holding_events(account, previous, current, now=now, scope=scope, source=source)
    events.extend(
        _shape_events(
            account,
            previous,
            current,
            now=now,
            scope=scope,
            source=source,
            sector_confidence=sector_confidence,
        )
    )
    return events


def _holding_events(
    account: str,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    now: datetime,
    scope: Scope,
    source: tuple[Provenance, ...],
) -> list[ChangeEvent]:
    """Positions opened, closed, or materially resized."""
    events: list[ChangeEvent] = []
    was_positions = set(previous.get("positions") or [])
    positions = set(current.get("positions") or [])
    weights = dict(current.get("weights") or {})
    was_weights = dict(previous.get("weights") or {})

    for symbol in sorted(positions - was_positions):
        events.append(
            ChangeEvent(
                kind=EventKind.POSITION_ADDED,
                occurred_at=now,
                subject=symbol,
                previous_state="not held",
                current_state=f"{_pct(weights.get(symbol))} of equity",
                materiality=Materiality.SIGNIFICANT,
                summary=(f"{account} now holds {symbol} at {_pct(weights.get(symbol))} of equity."),
                evidence=(Evidence("weight", 0.0, weights.get(symbol)),),
                confidence=EventConfidence.HIGH,
                provenance=source,
                scope=scope,
                dedup_key=f"POSITION_ADDED:{account}:{symbol}",
            )
        )
    for symbol in sorted(was_positions - positions):
        events.append(
            ChangeEvent(
                kind=EventKind.POSITION_REMOVED,
                occurred_at=now,
                subject=symbol,
                previous_state=f"{_pct(was_weights.get(symbol))} of equity",
                current_state="not held",
                materiality=Materiality.SIGNIFICANT,
                summary=f"{account} no longer holds {symbol}.",
                evidence=(Evidence("weight", was_weights.get(symbol), 0.0),),
                confidence=EventConfidence.HIGH,
                provenance=source,
                scope=scope,
                dedup_key=f"POSITION_REMOVED:{account}:{symbol}",
            )
        )

    for symbol in sorted(positions & was_positions):
        was, is_now = was_weights.get(symbol), weights.get(symbol)
        if was is None or is_now is None:
            continue
        shift = is_now - was
        if shift == 0:
            continue
        events.append(
            ChangeEvent(
                kind=EventKind.PORTFOLIO_WEIGHT_CHANGE,
                occurred_at=now,
                subject=symbol,
                previous_state=_pct(was),
                current_state=_pct(is_now),
                materiality=(
                    Materiality.NOTABLE if abs(shift) >= rules.WEIGHT_SHIFT else Materiality.ROUTINE
                ),
                summary=(
                    f"{symbol} moved from {_pct(was)} to {_pct(is_now)} of {account}'s equity."
                ),
                evidence=(
                    Evidence(
                        "weight",
                        was,
                        is_now,
                        change=shift,
                        unit="fraction of equity",
                        threshold=rules.WEIGHT_SHIFT,
                    ),
                ),
                confidence=EventConfidence.HIGH,
                provenance=source,
                scope=scope,
                dedup_key=f"WEIGHT:{account}:{symbol}",
            )
        )

    return events


def _shape_events(
    account: str,
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    now: datetime,
    scope: Scope,
    source: tuple[Provenance, ...],
    sector_confidence: EventConfidence,
) -> list[ChangeEvent]:
    """Concentration, sector weight, correlation cluster and cash level."""
    events: list[ChangeEvent] = []
    was_band, is_band = previous.get("concentration"), current.get("concentration")
    top3_shift = (current.get("top3_pct") or 0.0) - (previous.get("top3_pct") or 0.0)
    if (was_band != is_band and is_band) or abs(top3_shift) >= rules.WEIGHT_SHIFT:
        events.append(
            ChangeEvent(
                kind=EventKind.PORTFOLIO_CONCENTRATION_CHANGE,
                occurred_at=now,
                subject=account,
                previous_state=f"{was_band} top3 {_pct(previous.get('top3_pct'))}",
                current_state=f"{is_band} top3 {_pct(current.get('top3_pct'))}",
                materiality=(
                    Materiality.SIGNIFICANT if was_band != is_band else Materiality.NOTABLE
                ),
                summary=(
                    f"{account}'s top-three concentration moved from "
                    f"{_pct(previous.get('top3_pct'))} to "
                    f"{_pct(current.get('top3_pct'))} ({is_band})."
                ),
                evidence=(
                    Evidence(
                        "top3_pct",
                        previous.get("top3_pct"),
                        current.get("top3_pct"),
                        change=top3_shift,
                        threshold=rules.WEIGHT_SHIFT,
                    ),
                ),
                confidence=EventConfidence.HIGH,
                provenance=source,
                scope=scope,
                dedup_key=f"CONCENTRATION:{account}:{is_band}",
            )
        )

    was_sectors = dict(previous.get("sector_weights") or {})
    sectors = dict(current.get("sector_weights") or {})
    for sector in sorted(set(was_sectors) | set(sectors)):
        was, is_now = was_sectors.get(sector, 0.0), sectors.get(sector, 0.0)
        shift = is_now - was
        crossed = (was < rules.SECTOR_HEAVY_LEVEL <= is_now) or (
            is_now < rules.SECTOR_HEAVY_LEVEL <= was
        )
        if abs(shift) < rules.SECTOR_SHIFT and not crossed:
            continue
        events.append(
            ChangeEvent(
                kind=EventKind.SECTOR_CONCENTRATION_CHANGE,
                occurred_at=now,
                subject=sector,
                previous_state=_pct(was),
                current_state=_pct(is_now),
                materiality=(Materiality.SIGNIFICANT if crossed else Materiality.NOTABLE),
                summary=(
                    f"{account}'s {sector} exposure moved from {_pct(was)} to {_pct(is_now)}."
                ),
                evidence=(
                    Evidence(
                        "sector_weight",
                        was,
                        is_now,
                        change=shift,
                        threshold=rules.SECTOR_SHIFT,
                        comparison=(
                            f"crossed the {_pct(rules.SECTOR_HEAVY_LEVEL)} heavy level"
                            if crossed
                            else "shift in sector weight"
                        ),
                    ),
                ),
                # Sector labels are proxy-derived, so a sector event can never be
                # more trustworthy than the labelling behind it.
                confidence=sector_confidence,
                provenance=source,
                scope=scope,
                dedup_key=f"SECTOR_CONCENTRATION:{account}:{sector}",
            )
        )

    was_corr, is_corr = previous.get("average_correlation"), current.get("average_correlation")
    if was_corr is not None and is_corr is not None:
        was_cluster, is_cluster = _cluster(was_corr), _cluster(is_corr)
        if was_cluster != is_cluster:
            events.append(
                ChangeEvent(
                    kind=EventKind.CORRELATION_CLUSTER_CHANGE,
                    occurred_at=now,
                    subject=account,
                    previous_state=was_cluster,
                    current_state=is_cluster,
                    materiality=Materiality.SIGNIFICANT,
                    summary=(
                        f"{account}'s average internal correlation moved from "
                        f"{was_corr:.2f} to {is_corr:.2f}, changing its overlap band "
                        f"from {was_cluster} to {is_cluster}."
                    ),
                    evidence=(
                        Evidence(
                            "average_correlation",
                            was_corr,
                            is_corr,
                            change=is_corr - was_corr,
                            threshold=rules.CORRELATION_BANDS["p90"],
                            comparison="percentile band of real equity pairs",
                        ),
                    ),
                    confidence=EventConfidence.HIGH,
                    provenance=source,
                    scope=scope,
                    dedup_key=f"CORRELATION_CLUSTER:{account}:{is_cluster}",
                )
            )

    was_cash, is_cash = previous.get("cash_pct"), current.get("cash_pct")
    if was_cash is not None and is_cash is not None:
        shift = is_cash - was_cash
        band = rules.band(shift, rules.CASH_SHIFT_NOTABLE, rules.CASH_SHIFT_SIGNIFICANT)
        if band is not Materiality.ROUTINE:
            events.append(
                ChangeEvent(
                    kind=EventKind.CASH_LEVEL_CHANGE,
                    occurred_at=now,
                    subject=account,
                    previous_state=_pct(was_cash),
                    current_state=_pct(is_cash),
                    materiality=band,
                    summary=(
                        f"{account}'s cash moved from {_pct(was_cash)} to "
                        f"{_pct(is_cash)} of equity."
                    ),
                    evidence=(
                        Evidence(
                            "cash_pct",
                            was_cash,
                            is_cash,
                            change=shift,
                            threshold=rules.CASH_SHIFT_NOTABLE,
                        ),
                    ),
                    confidence=EventConfidence.HIGH,
                    provenance=source,
                    scope=scope,
                    dedup_key=f"CASH_LEVEL:{account}",
                )
            )
    return events


def _cluster(correlation: float) -> str:
    bands = rules.CORRELATION_BANDS
    if correlation >= bands["p99"]:
        return "EXTREME_OVERLAP"
    if correlation >= bands["p90"]:
        return "HIGH_OVERLAP"
    if correlation >= bands["p75"]:
        return "ELEVATED_OVERLAP"
    return "NORMAL_OVERLAP"


# --------------------------------------------------------------------- system
def detect_health(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    now: datetime,
    as_of: str,
) -> list[ChangeEvent]:
    """A change in whether the data behind every other answer is usable."""
    if previous is None:
        return []
    was, is_now = previous.get("status"), current.get("status")
    if not is_now or was == is_now:
        return []
    recovered = is_now == "READY"
    return [
        ChangeEvent(
            kind=EventKind.DATA_HEALTH_CHANGE,
            occurred_at=now,
            subject="sec_fact_store",
            previous_state=str(was),
            current_state=str(is_now),
            materiality=(Materiality.NOTABLE if recovered else Materiality.CRITICAL),
            summary=(
                f"The SEC fact store moved from {was} to {is_now}"
                + ("." if recovered else "; company analysis is degraded until it is synced.")
            ),
            evidence=(
                Evidence("status", was, is_now),
                Evidence(
                    "rows",
                    previous.get("rows"),
                    current.get("rows"),
                    change=(current.get("rows") or 0) - (previous.get("rows") or 0),
                ),
            ),
            confidence=EventConfidence.HIGH,
            provenance=(Provenance(HEALTH_SOURCE, as_of),),
            scope=Scope(ScopeKind.SYSTEM),
            dedup_key=f"DATA_HEALTH:{is_now}",
        )
    ]
