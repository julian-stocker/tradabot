"""One pass: observe, compare, judge, suppress, rank.

The engine exists to answer one question — *is anything here worth telling
someone about?* — and to be able to answer "no". That negative answer is the
hard part and the reason for every suppression stage below.

Four stages, in order
---------------------
**Materiality.** A change below its declared threshold is ROUTINE. It is
detected and counted, never reported.

**Deduplication within the run.** Two detectors can reach the same subject by
different routes. The same key twice in one pass is one event.

**Cooldown across runs.** A measure hovering at its threshold crosses back and
forth, producing a genuinely new transition each time. The cooldown is what
stops that from being reported every session, and it is per-kind: a regime is
quiet for a week, a filing never repeats at all because its key is the
accession.

**Ranking.** What survives is ordered by how much attention it deserves, so a
reader who stops after three lines has read the three that mattered.

What the engine never does
--------------------------
It does not decide what to *do*. There is no action, no priority queue of
trades, no rotation. It decides what is worth *saying*, which is a smaller and
much better supported claim.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.monitoring import detectors
from app.monitoring import materiality as rules
from app.monitoring import observations as obs
from app.monitoring.schemas import (
    MATERIALITY_ORDER,
    REPORTABLE_FROM,
    ChangeEvent,
    EventConfidence,
    EventKind,
    Materiality,
    MonitoringRun,
)
from app.monitoring.state import InMemoryStateStore, MonitorStateStore

logger = get_logger(__name__)

MARKET_SCOPE = "market"
SYMBOL_SCOPE = "symbol"
SECTOR_SCOPE = "sector"
COMPANY_SCOPE = "company"
PORTFOLIO_SCOPE = "portfolio"
HEALTH_SCOPE = "health"

_CONFIDENCE_RANK: dict[str, int] = {
    "INSUFFICIENT": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
}


@dataclass(slots=True)
class MonitoringInputs:
    """Everything one pass reads. Assembled by the caller, never fetched here.

    Keeping acquisition outside the engine is what lets the historical replay
    hand over past observations and get the same behaviour, and what keeps the
    engine testable with three dictionaries and no I/O.
    """

    as_of: str
    bars: Mapping[str, obs.Bars]
    benchmark: str = "SPY"
    sectors: Mapping[str, str] | None = None
    watched: Sequence[str] = ()
    company_contexts: Mapping[str, Any] | None = None
    latest_filings: Mapping[str, dict[str, Any]] | None = None
    portfolios: Mapping[str, Any] | None = None
    fact_store_health: Any = None
    sector_confidence: EventConfidence = EventConfidence.MEDIUM


class MonitoringEngine:
    """Builds one :class:`MonitoringRun` from one set of inputs."""

    def __init__(
        self,
        store: MonitorStateStore | None = None,
        *,
        now: datetime | None = None,
        cooldowns: bool = True,
    ) -> None:
        self._store = store if store is not None else InMemoryStateStore()
        self._now = now
        self._cooldowns = cooldowns

    # ------------------------------------------------------------------ run
    def run(self, inputs: MonitoringInputs) -> MonitoringRun:
        now = self._now or datetime.now(UTC)
        candidates: list[ChangeEvent] = []
        examined = 0

        candidates.extend(self._market(inputs, now))
        examined += 1
        candidates.extend(self._sectors(inputs, now))
        candidates.extend(self._symbols(inputs, now))
        examined += len(inputs.watched)
        candidates.extend(self._companies(inputs, now))
        candidates.extend(self._portfolios(inputs, now))
        examined += len(inputs.portfolios or {})
        candidates.extend(self._health(inputs, now))

        kept, routine, duplicate, cooled = self._filter(candidates, now)
        self._store.flush()
        return MonitoringRun(
            as_of=inputs.as_of,
            started_at=now,
            events=tuple(rank(kept)),
            suppressed_routine=routine,
            suppressed_duplicate=duplicate,
            suppressed_cooldown=cooled,
            subjects_examined=examined,
            notes=(
                f"reporting floor is {REPORTABLE_FROM}",
                "no event expresses an action; this layer describes change only",
            ),
        )

    # -------------------------------------------------------------- stages
    def _market(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        bars = inputs.bars.get(inputs.benchmark)
        if bars is None:
            return []
        current = obs.market_observation(bars, inputs.as_of, trend_band=rules.REGIME_TREND_BAND)
        held = self._store.get(MARKET_SCOPE, inputs.benchmark)
        previous = held.state if held else None

        # A regime must persist before it is announced, so the baseline tracks
        # the last *announced* regime separately from the one currently forming.
        confirmed = (previous or {}).get("confirmed_regime")
        pending = (previous or {}).get("pending_regime")
        sessions = int((previous or {}).get("pending_sessions") or 0)
        regime = current.get("regime")
        if regime == pending:
            sessions += 1
        else:
            pending, sessions = regime, 1

        events: list[ChangeEvent] = []
        if confirmed is not None:
            events = detectors.detect_market_regime(
                {**(previous or {}), "regime": confirmed},
                current,
                now=now,
                as_of=inputs.as_of,
                sessions_in_state=sessions,
            )
        if events or confirmed is None:
            confirmed = regime
        self._store.put(
            MARKET_SCOPE,
            inputs.benchmark,
            {
                **current,
                "confirmed_regime": confirmed,
                "pending_regime": pending,
                "pending_sessions": sessions,
            },
            observed_at=inputs.as_of,
        )
        return events

    def _sectors(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        if not inputs.sectors:
            return []
        members: dict[str, list[str]] = {}
        for symbol, sector in inputs.sectors.items():
            if symbol in inputs.bars:
                members.setdefault(sector, []).append(symbol)
        current = obs.sector_observation(members, inputs.bars, inputs.as_of)
        events: list[ChangeEvent] = []
        for sector, observation in sorted(current.items()):
            held = self._store.get(SECTOR_SCOPE, sector)
            events.extend(
                detectors.detect_sector_move(
                    sector,
                    held.state if held else None,
                    observation,
                    now=now,
                    as_of=inputs.as_of,
                )
            )
            self._store.put(SECTOR_SCOPE, sector, observation, observed_at=inputs.as_of)
        return events

    def _symbols(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        benchmark = inputs.bars.get(inputs.benchmark)
        if benchmark is None:
            return []
        events: list[ChangeEvent] = []
        for symbol in sorted(inputs.watched):
            bars = inputs.bars.get(symbol)
            if bars is None:
                continue
            observation = obs.symbol_observation(bars, benchmark, inputs.as_of)
            if observation is None:
                continue
            held = self._store.get(SYMBOL_SCOPE, symbol)
            events.extend(
                detectors.detect_symbol(
                    symbol,
                    held.state if held else None,
                    observation,
                    now=now,
                    as_of=inputs.as_of,
                    sector_confidence=inputs.sector_confidence,
                )
            )
            self._store.put(SYMBOL_SCOPE, symbol, observation, observed_at=inputs.as_of)
        return events

    def _companies(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        if not inputs.company_contexts:
            return []
        events: list[ChangeEvent] = []
        filings = inputs.latest_filings or {}
        for symbol in sorted(inputs.company_contexts):
            observation = obs.company_observation(
                inputs.company_contexts[symbol], filings.get(symbol)
            )
            held = self._store.get(COMPANY_SCOPE, symbol)
            previous = held.state if held else None
            baseline, carried = _valuation_hysteresis(previous, observation)
            found = detectors.detect_company(
                symbol, baseline, observation, now=now, as_of=inputs.as_of
            )
            events.extend(found)
            if any(e.kind is EventKind.VALUATION_STATE_CHANGE for e in found):
                carried["confirmed_valuation"] = observation.get("valuation_context")
            self._store.put(
                COMPANY_SCOPE, symbol, {**observation, **carried}, observed_at=inputs.as_of
            )
        return events

    def _portfolios(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        if not inputs.portfolios:
            return []
        events: list[ChangeEvent] = []
        for account in sorted(inputs.portfolios):
            observation = obs.portfolio_observation(inputs.portfolios[account])
            held = self._store.get(PORTFOLIO_SCOPE, account)
            events.extend(
                detectors.detect_portfolio(
                    account,
                    held.state if held else None,
                    observation,
                    now=now,
                    as_of=inputs.as_of,
                    sector_confidence=inputs.sector_confidence,
                )
            )
            self._store.put(PORTFOLIO_SCOPE, account, observation, observed_at=inputs.as_of)
        return events

    def _health(self, inputs: MonitoringInputs, now: datetime) -> list[ChangeEvent]:
        if inputs.fact_store_health is None:
            return []
        observation = obs.health_observation(inputs.fact_store_health)
        held = self._store.get(HEALTH_SCOPE, "sec_fact_store")
        events = detectors.detect_health(
            held.state if held else None, observation, now=now, as_of=inputs.as_of
        )
        self._store.put(HEALTH_SCOPE, "sec_fact_store", observation, observed_at=inputs.as_of)
        return events

    # -------------------------------------------------------------- filter
    def _filter(
        self, candidates: Sequence[ChangeEvent], now: datetime
    ) -> tuple[list[ChangeEvent], int, int, int]:
        kept: list[ChangeEvent] = []
        seen: set[str] = set()
        routine = duplicate = cooled = 0
        for event in candidates:
            if not event.reportable:
                routine += 1
                continue
            key = event.key()
            if key in seen:
                duplicate += 1
                continue
            seen.add(key)
            if self._cooldowns and self._in_cooldown(event, now):
                cooled += 1
                continue
            kept.append(event)
            self._store.mark_notified("emitted", key, at=now.isoformat())
        return kept, routine, duplicate, cooled

    def _in_cooldown(self, event: ChangeEvent, now: datetime) -> bool:
        hours = rules.cooldown_hours(event.kind)
        if hours <= 0:
            return False
        held = self._store.get("emitted", event.key())
        if held is None or not held.notified_at:
            return False
        try:
            last = datetime.fromisoformat(held.notified_at)
        except ValueError:
            return False
        return now - last < timedelta(hours=hours)


def _valuation_hysteresis(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Hold a new valuation band for a second pass before letting it report.

    Returns the baseline the detector should compare against, and the bookkeeping
    fields to carry into the stored state. When the new band has not yet been
    confirmed, the baseline is given the *current* band so the comparison finds
    no change -- the observation is still recorded, it simply stays quiet.
    """
    band = current.get("valuation_context")
    if previous is None:
        return None, {
            "confirmed_valuation": band,
            "pending_valuation": band,
            "pending_valuation_count": 1,
        }
    confirmed = previous.get("confirmed_valuation", previous.get("valuation_context"))
    pending = previous.get("pending_valuation")
    count = int(previous.get("pending_valuation_count") or 0)
    if band == pending:
        count += 1
    else:
        pending, count = band, 1
    baseline = dict(previous)
    baseline["valuation_context"] = confirmed if count >= rules.VALUATION_CONFIRM_PASSES else band
    carried = {
        "confirmed_valuation": confirmed,
        "pending_valuation": pending,
        "pending_valuation_count": count,
    }
    return baseline, carried


def rank(events: Sequence[ChangeEvent]) -> list[ChangeEvent]:
    """Most worth reading first.

    Materiality dominates, then confidence -- a SIGNIFICANT change nobody can
    stand behind should not outrank a NOTABLE one that is solid. Magnitude
    breaks the remaining ties, and the subject name makes the order stable so
    two identical runs produce identical output.
    """
    return sorted(events, key=_rank_key)


def _rank_key(event: ChangeEvent) -> tuple[int, int, float, str]:
    magnitude = 0.0
    for item in event.evidence:
        if item.change is not None:
            magnitude = max(magnitude, abs(item.change))
        elif isinstance(item.current, (int, float)) and item.threshold:
            magnitude = max(magnitude, abs(float(item.current) / item.threshold))
    return (
        -MATERIALITY_ORDER[str(event.materiality)],
        -_CONFIDENCE_RANK[str(event.confidence)],
        -magnitude,
        f"{event.kind}:{event.subject}",
    )


def is_quiet(run: MonitoringRun) -> bool:
    """**The question this whole phase exists to answer.**"""
    return run.quiet


__all__ = [
    "Materiality",
    "MonitoringEngine",
    "MonitoringInputs",
    "MonitoringRun",
    "is_quiet",
    "rank",
]
