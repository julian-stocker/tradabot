"""What a company is currently known to have filed, ready for any consumer.

A read model over the event store, and nothing more. It selects, groups and
labels; it classifies nothing, fetches nothing and interprets nothing. Discord
is the first consumer and the shape here is deliberately not Discord's: the
weekly newsletter, monitoring and a future web view all need the same answer,
and a report built inside a renderer is a report only that renderer can use.

What the section answers
------------------------
What happened, when it became public, which primary source establishes it, what
figure (if any) was safely extracted, how much attention the filing warrants,
and what remains unknown. It does not answer whether any of that is good news.
There is no field in which a direction could be recorded, and
``historical_evidence`` stays ``NOT_ESTABLISHED`` because no event study over
these event kinds exists.

Current is not the same as true
-------------------------------
An event's historical truth never expires; whether it belongs under a heading
called *current developments* is a separate question, answered by
:mod:`app.research_intelligence.freshness` per event kind. Nothing is deleted
to make that work -- selection is a read-time filter over a store that keeps
everything, which is also what lets a question asked about last quarter be
answered with last quarter's filings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from app.core.logging import get_logger
from app.research_intelligence import context as magnitude_context
from app.research_intelligence.freshness import MAX_WINDOW_DAYS, is_current
from app.research_intelligence.schemas import (
    ContextStatus,
    EventKind,
    FiscalPeriod,
    HistoricalEvidence,
    MagnitudeContext,
    Materiality,
    ResearchEvent,
    ResearchFact,
)

logger = get_logger(__name__)

MAX_CURRENT_DEVELOPMENTS: Final = 3
"""Filings shown. Declared once, applied to every company.

Three, because the card already carries fundamentals, valuation, market
position, peer context and data quality, and a fourth block of filings turns a
report into a feed. It is a *presentation* budget and is deliberately not tuned
per company -- a limit that moved when a company had more to say would make the
section's length a signal, which is a claim nothing here can support."""

FUND_TYPES: Final[frozenset[str]] = frozenset({"ETF", "FUND", "ETN"})

_MATERIALITY_RANK: Final[dict[Materiality, int]] = {
    Materiality.CRITICAL: 3,
    Materiality.SIGNIFICANT: 2,
    Materiality.NOTABLE: 1,
    Materiality.ROUTINE: 0,
}

KIND_LABELS: Final[dict[EventKind, str]] = {
    EventKind.EARNINGS_RELEASE: "Earnings",
    EventKind.MANAGEMENT_CHANGE: "Management change",
    EventKind.M_AND_A: "Acquisition or disposal",
    EventKind.MATERIAL_AGREEMENT: "Material agreement",
    EventKind.DEBT_EVENT: "Debt obligation",
    EventKind.ACCOUNTING_RESTATEMENT: "Non-reliance on prior financials",
    EventKind.AUDITOR_CHANGE: "Auditor change",
    EventKind.BANKRUPTCY_OR_RECEIVERSHIP: "Bankruptcy or receivership",
    EventKind.IMPAIRMENT: "Material impairment",
    EventKind.EXIT_OR_DISPOSAL_COSTS: "Exit or disposal costs",
    EventKind.LISTING_RULE_MATTER: "Listing rule matter",
    EventKind.CONTROL_CHANGE: "Change in control",
    EventKind.CYBERSECURITY_INCIDENT: "Cybersecurity incident",
    EventKind.UNREGISTERED_EQUITY_SALE: "Unregistered equity sale",
    EventKind.PERIODIC_REPORT: "Periodic report",
    EventKind.UNCLASSIFIED_SEC_FILING: "Unclassified filing",
}
"""Short names. The SEC's own item title stays on ``summary`` for anyone who
wants the legal wording; repeating *Departure of Directors or Certain Officers;
Election of Directors* on a card costs a line and adds nothing a reader of
"Management change" did not already have."""

METRIC_LABELS: Final[dict[str, str]] = {
    "revenue": "Revenue",
    "net_income": "Net income",
    "operating_income": "Operating income",
    "eps_diluted": "Diluted EPS",
    "eps_basic": "Basic EPS",
    "gross_margin": "Gross margin",
}

_PERIOD_PHRASES: Final[dict[FiscalPeriod, str]] = {
    FiscalPeriod.QUARTER: "Quarter ended",
    FiscalPeriod.YEAR: "Year ended",
    FiscalPeriod.YEAR_TO_DATE: "Year to date ended",
    FiscalPeriod.TRAILING_TWELVE_MONTHS: "Trailing twelve months ended",
    FiscalPeriod.INSTANT: "As of",
}

QUIET_KINDS: Final[frozenset[EventKind]] = frozenset(
    {EventKind.UNCLASSIFIED_SEC_FILING, EventKind.PERIODIC_REPORT}
)
"""Kinds that are counted but never given a slot, for two different reasons.

An **unclassified filing** establishes that *something* was disclosed without
establishing what, so there is nothing to put on a card -- but its count is
what turns coverage into ``SOURCE_LIMITATION`` rather than into silence.

A **periodic report** is excluded because the consumer already shows it. A
10-Q's contents *are* the fundamentals, the valuation and the growth history
printed above this section, computed from that very filing. Listing it again as
a development says only "the quarterly report was filed", and it was measured
crowding out the thing a reader actually wanted: AMD filed its 10-Q and its
earnings release on the same day, and the 10-Q won the tiebreak and pushed the
release -- the one carrying an extracted revenue figure -- off the card."""

SHOWN_KINDS: Final[frozenset[EventKind]] = frozenset(k for k in EventKind if k not in QUIET_KINDS)


class CoverageStatus(StrEnum):
    """Why the section says what it says. Seven states, each a different fact."""

    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    """Current developments exist, and so do current filings this system cannot
    classify. Both are true and the reader should know."""
    NO_CURRENT_EVENTS = "NO_CURRENT_EVENTS"
    """The source path works and the company is covered; nothing recent
    qualifies. A real, informative answer."""
    SOURCE_LIMITATION = "SOURCE_LIMITATION"
    """Filings exist and the regime does not carry deterministic item codes --
    a foreign private issuer reporting through 6-K. **Not** the same as nothing
    having happened, and never presented as if it were."""
    NO_COVERAGE = "NO_COVERAGE"
    """The store holds nothing for this company. Every public issuer files
    something, so an empty history means this company has not been ingested --
    a gap in Tradabot, not in the company."""
    UNAVAILABLE = "UNAVAILABLE"
    """No research store at all. A property of this installation."""
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """A fund or note, which has no company filings to report."""


@dataclass(frozen=True, slots=True)
class DevelopmentFigure:
    """One figure a filing stated outright, with the period it belongs to.

    Carries the raw value, unit and currency rather than a formatted string:
    money formatting belongs to whichever surface is rendering, and this model
    is shared by several.
    """

    metric: str
    label: str
    value: float
    unit: str
    currency: str
    period: str
    context: MagnitudeContext | None = None
    """Proportion against the company's own point-in-time history, when one was
    computable. Deliberately **not rendered on the Discord card** -- see
    :meth:`CurrentDevelopmentsService.for_company`."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "period": self.period,
            "context": self.context.as_dict() if self.context else None,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentItem:
    """One classified occurrence inside one filing."""

    kind: EventKind
    label: str
    item: str | None
    materiality: Materiality
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "label": self.label,
            "item": self.item,
            "materiality": str(self.materiality),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class Development:
    """One filing, with everything it reported.

    The unit is the **filing**, not the event, because an 8-K carrying Items
    1.01 and 5.02 is one thing that happened on one day. Rendering it as two
    entries would read as two unrelated filings; the items stay listed
    separately inside it, so no distinct semantics are merged away.
    """

    accession: str
    form: str
    published_at: str
    occurred_at: str | None
    items: tuple[DevelopmentItem, ...]
    figures: tuple[DevelopmentFigure, ...] = ()
    evidence_available: bool = False
    source_url: str = ""
    amends_accession: str | None = None

    @property
    def materiality(self) -> Materiality:
        """The highest band among the filing's items."""
        return max(
            (i.materiality for i in self.items),
            key=lambda m: _MATERIALITY_RANK.get(m, 0),
            default=Materiality.ROUTINE,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "accession": self.accession,
            "form": self.form,
            "published_at": self.published_at,
            "occurred_at": self.occurred_at,
            "materiality": str(self.materiality),
            "items": [i.as_dict() for i in self.items],
            "figures": [f.as_dict() for f in self.figures],
            "evidence_available": self.evidence_available,
            "source_url": self.source_url,
            "amends_accession": self.amends_accession,
        }


@dataclass(frozen=True, slots=True)
class CurrentDevelopments:
    """The answer, with its own reason for being what it is."""

    status: CoverageStatus
    as_of: str
    company_id: int | None = None
    developments: tuple[Development, ...] = ()
    suppressed: int = 0
    """Qualifying filings beyond :data:`MAX_CURRENT_DEVELOPMENTS`. Counted so
    the card can say the list was trimmed rather than imply it was complete."""
    unclassified_current: int = 0
    periodic_current: int = 0
    """Current 10-K/10-Q/20-F/40-F filings. Counted, never listed -- their
    contents are the fundamentals section, not a development."""
    detail: str | None = None
    historical_evidence: HistoricalEvidence = HistoricalEvidence.NOT_ESTABLISHED
    """Fixed. No event study over these event kinds exists, so no displayed
    development may carry a claim about what comparable filings did next."""

    @property
    def has_developments(self) -> bool:
        return bool(self.developments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "as_of": self.as_of,
            "company_id": self.company_id,
            "developments": [d.as_dict() for d in self.developments],
            "suppressed": self.suppressed,
            "unclassified_current": self.unclassified_current,
            "periodic_current": self.periodic_current,
            "detail": self.detail,
            "historical_evidence": str(self.historical_evidence),
        }


FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{dashed}-index.htm"
"""EDGAR's own landing page for one filing. Built from the accession, never
fetched, and the only host this module will ever name."""


def filing_url(cik: str, accession: str) -> str:
    try:
        number = int(cik)
    except (TypeError, ValueError):
        return ""
    return FILING_INDEX.format(cik=number, accession=accession.replace("-", ""), dashed=accession)


class CurrentDevelopmentsService:
    """Selects the filings worth showing for one company, as of one moment.

    Args:
        store: an :class:`~app.research_intelligence.store.EventStore`, or
            ``None`` when this installation has no research store. The two are
            different answers and are kept apart.
        facts: an optional :class:`~app.advisor.facts.FactStore`, used only to
            put a reported figure in proportion.
        max_developments: presentation budget. Declared, not tuned.
    """

    def __init__(
        self,
        *,
        store: Any | None,
        facts: Any | None = None,
        max_developments: int = MAX_CURRENT_DEVELOPMENTS,
    ) -> None:
        self._store = store
        self._facts = facts
        self._max = max_developments

    def for_company(
        self,
        *,
        company_id: int | None,
        cik: str | None,
        as_of: str,
        company_key: str | None = None,
        asset_type: str = "STOCK",
    ) -> CurrentDevelopments:
        """Current developments for one company. **Never raises.**

        Identity is the caller's resolved ``company_id`` -- never a ticker.
        Cross-listed issuers therefore share one event history: SAP's Frankfurt
        and US listings resolve to the same company and see the same filings,
        which is what stops one issuer growing two logical histories that would
        later have to be reconciled by guessing they were the same.

        Magnitude context is computed here and **not rendered on the card**. A
        quarter's revenue expressed as 38% of a trailing-year comparator is
        arithmetically sound and reads, to almost anyone, as either a decline or
        an endorsement -- neither of which the number says. It stays on the
        model for consumers that can afford the sentence to explain it.
        """
        if asset_type.upper() in FUND_TYPES:
            return CurrentDevelopments(
                status=CoverageStatus.NOT_APPLICABLE,
                as_of=as_of,
                detail="a fund has no company filings to report",
            )
        if self._store is None:
            return CurrentDevelopments(
                status=CoverageStatus.UNAVAILABLE,
                as_of=as_of,
                detail="the research store has not been built",
            )
        if company_id is None:
            return CurrentDevelopments(
                status=CoverageStatus.NO_COVERAGE,
                as_of=as_of,
                detail="this listing has no resolved company identity",
            )
        try:
            # Bounded by the longest freshness window rather than reading the
            # filer's whole history: Novo Nordisk has 904 stored events and all
            # but a handful are older than any window could reach.
            known = self._store.recent_events(company_id, as_of=as_of, since=_horizon(as_of))
        except Exception as exc:
            logger.warning(
                "research store unreadable", company_id=company_id, reason=type(exc).__name__
            )
            return CurrentDevelopments(
                status=CoverageStatus.UNAVAILABLE,
                as_of=as_of,
                company_id=company_id,
                detail="the research store could not be read",
            )
        return self._report(
            known,
            company_id=company_id,
            cik=cik or "",
            as_of=as_of,
            company_key=company_key,
            ingested=bool(known) or self._store.has_company(company_id),
        )

    # ------------------------------------------------------------- internals
    def _report(
        self,
        known: list[ResearchEvent],
        *,
        company_id: int,
        cik: str,
        as_of: str,
        company_key: str | None,
        ingested: bool,
    ) -> CurrentDevelopments:
        if not ingested:
            return CurrentDevelopments(
                status=CoverageStatus.NO_COVERAGE,
                as_of=as_of,
                company_id=company_id,
                detail="Tradabot has not ingested SEC filings for this company",
            )

        live = [e for e in known if _live(e, as_of=as_of)]
        unclassified = sum(1 for e in live if e.event_kind is EventKind.UNCLASSIFIED_SEC_FILING)
        periodic = sum(1 for e in live if e.event_kind is EventKind.PERIODIC_REPORT)
        shown = [e for e in live if e.event_kind in SHOWN_KINDS]
        if not shown:
            status = (
                CoverageStatus.SOURCE_LIMITATION
                if unclassified
                else CoverageStatus.NO_CURRENT_EVENTS
            )
            return CurrentDevelopments(
                status=status,
                as_of=as_of,
                company_id=company_id,
                unclassified_current=unclassified,
                periodic_current=periodic,
                detail=_no_events_detail(status, known, periodic=periodic),
            )

        groups = self._group(shown, cik=cik, company_key=company_key, as_of=as_of)
        selected = groups[: self._max]
        return CurrentDevelopments(
            status=CoverageStatus.PARTIAL if unclassified else CoverageStatus.AVAILABLE,
            as_of=as_of,
            company_id=company_id,
            developments=tuple(selected),
            suppressed=len(groups) - len(selected),
            unclassified_current=unclassified,
            periodic_current=periodic,
        )

    def _group(
        self,
        events: list[ResearchEvent],
        *,
        cik: str,
        company_key: str | None,
        as_of: str,
    ) -> list[Development]:
        """One :class:`Development` per accession, in the order shown.

        Two tiers, fixed in advance: anything CRITICAL first, then everything
        else by recency, with the accession as a final tiebreak so the same
        store always produces the same card.

        The tiers exist for opposite reasons. *Current developments* means
        recent ones, and ranking strictly by materiality produced a card where
        a completed acquisition from ten months earlier sat above a management
        change from a fortnight ago -- technically current under the M&A
        freshness window, and not what the heading promises. But a restatement
        or a change of control must not be pushed off the card by three routine
        filings either, because the card turns orange for those and would then
        be orange with nothing on it to explain why. So CRITICAL is reserved,
        and the rest is chronological.
        """
        by_accession: dict[str, list[ResearchEvent]] = {}
        for event in sorted(events, key=lambda e: (e.classifying_item or "", e.event_id)):
            by_accession.setdefault(event.accession, []).append(event)

        built: list[Development] = []
        for accession, group in by_accession.items():
            first = group[0]
            built.append(
                Development(
                    accession=accession,
                    form=first.form,
                    published_at=first.published_at,
                    occurred_at=first.occurred_at,
                    items=tuple(
                        DevelopmentItem(
                            kind=e.event_kind,
                            label=KIND_LABELS.get(e.event_kind, str(e.event_kind)),
                            item=e.classifying_item,
                            materiality=e.materiality,
                            summary=e.fact_summary,
                        )
                        for e in group
                    ),
                    figures=self._figures(group, company_key=company_key, as_of=as_of),
                    evidence_available=any(e.evidence for e in group) or bool(first.source_url),
                    source_url=filing_url(cik or first.cik, accession),
                    amends_accession=first.amends_accession,
                )
            )
        built.sort(
            key=lambda d: (
                d.materiality is Materiality.CRITICAL,
                d.published_at,
                d.accession,
            ),
            reverse=True,
        )
        return built

    def _figures(
        self, group: list[ResearchEvent], *, company_key: str | None, as_of: str
    ) -> tuple[DevelopmentFigure, ...]:
        """Facts safely extracted from this filing's documents, if any.

        Only facts already in the store, reached through events that passed the
        point-in-time filter -- so a figure can never be newer than the filing
        that carries it. Refusals are not read here at all: the reasons a
        sentence was declined are diagnostics, and a card is not a diagnostic.
        """
        out: list[DevelopmentFigure] = []
        store = self._store
        if store is None:  # pragma: no cover - unreachable; the caller checked
            return ()
        for event in group:
            try:
                facts = store.facts_for_event(event.event_id)
            except Exception:
                continue
            for fact in sorted(facts, key=lambda f: f.metric):
                out.append(self._figure(fact, company_key=company_key, as_of=as_of))
        # One row per metric and period. Two exhibits stating the same figure
        # corroborate; printing it twice just looks like two different numbers.
        unique: dict[tuple[str, str, float], DevelopmentFigure] = {}
        for figure in out:
            unique.setdefault((figure.metric, figure.period, figure.value), figure)
        return tuple(unique.values())

    def _figure(
        self, fact: ResearchFact, *, company_key: str | None, as_of: str
    ) -> DevelopmentFigure:
        return DevelopmentFigure(
            metric=fact.metric,
            label=METRIC_LABELS.get(fact.metric, fact.metric.replace("_", " ").capitalize()),
            value=fact.value,
            unit=fact.unit,
            currency=fact.currency,
            period=describe_period(fact),
            context=self._context(fact, company_key=company_key, as_of=as_of),
        )

    def _context(
        self, fact: ResearchFact, *, company_key: str | None, as_of: str
    ) -> MagnitudeContext | None:
        if self._facts is None or not company_key:
            return None
        try:
            result = magnitude_context.magnitude(
                fact, store=self._facts, symbol=company_key, as_of=as_of
            )
        except Exception:
            return None
        return result if result.status is ContextStatus.COMPUTED else None


def _horizon(as_of: str) -> str:
    """The oldest publication date that could still be current at ``as_of``."""
    try:
        moment = datetime.strptime(as_of[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return ""
    return (moment - timedelta(days=MAX_WINDOW_DAYS)).strftime("%Y-%m-%d")


def _live(event: ResearchEvent, *, as_of: str) -> bool:
    """Whether the event is both current and not yet superseded at ``as_of``.

    Supersession is read against ``as_of`` rather than against now, so a
    ``/check`` dated before an amendment still shows the original filing as the
    current state -- which is what someone reading that date actually saw.
    """
    if event.superseded_at and event.superseded_at <= as_of:
        return False
    return is_current(event, as_of=as_of)


def _no_events_detail(
    status: CoverageStatus, known: list[ResearchEvent], *, periodic: int = 0
) -> str:
    if status is CoverageStatus.SOURCE_LIMITATION:
        # Base forms: "6-K, 6-K/A" names one reporting regime twice.
        forms = sorted(
            {e.base_form for e in known if e.event_kind is EventKind.UNCLASSIFIED_SEC_FILING}
        )
        naming = ", ".join(forms[:3]) or "6-K"
        return (
            f"this issuer reports mainly through SEC {naming} filings, which carry "
            f"no item codes to classify"
        )
    if periodic:
        return (
            "only routine periodic reports were filed recently; their contents "
            "are the fundamentals above"
        )
    return "no filing within its currency window qualifies"


def describe_period(fact: ResearchFact) -> str:
    """``Quarter ended 26 Jul 2026``. The period, never inferred."""
    phrase = _PERIOD_PHRASES.get(fact.fiscal_period) if fact.fiscal_period else None
    date = fact.instant or fact.period_end
    if phrase is None or not date:
        return ""
    return f"{phrase} {pretty_date(date)}"


def pretty_date(value: str) -> str:
    """``2026-07-26`` -> ``26 Jul 2026``; an ISO instant to its date."""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=UTC).strftime("%d %b %Y")
    except ValueError:
        return value
