"""Assembling one company's evidence, deterministically and within a budget.

Tradabot decides what a synthesis sees. The model never fetches, never browses
and never asks for more: it receives a packet built here from services that
already own their numbers and already know when to refuse them.

Bounded on purpose
------------------
The stores behind this hold 2.3 million canonical facts, ten years of series and
29,783 research events. A packet that carried a fraction of that would still be
too large to reason over and would bury the handful of figures that matter.
Selection is therefore fixed and stated: current level and the longest available
window per metric, the peer group the company is actually in, three
developments, and a small number of quoted sentences from filings.

Everything omitted carries a reason, so a shorter packet never reads as a
quieter company.
"""

from __future__ import annotations

from typing import Any, Final

from app.core.logging import get_logger
from app.synthesis.evidence import (
    ConflictStatus,
    ConflictType,
    EvidenceClass,
    EvidenceConflict,
    EvidenceItem,
    EvidencePacket,
    Freshness,
    Omission,
    OmissionReason,
    PacketIdentity,
    Provenance,
)

logger = get_logger(__name__)

MAX_DEVELOPMENTS: Final = 3
"""Filings carried. The same budget ``/check`` already applies, for the same
reason: a fourth turns a report into a feed."""

MAX_SOURCE_EXCERPTS: Final = 4
MAX_EXCERPT_CHARS: Final = 320
"""Characters of quoted filing text per item. Excerpts are cut at a sentence or
word boundary, never mid-number: a truncated "revenue of $96.2 bil" would be a
different claim from the one the filing made."""

TRAJECTORY_METRICS: Final[tuple[str, ...]] = (
    "revenue",
    "operating_margin",
    "fcf_margin",
    "gross_margin",
    "share_count",
)
TRAJECTORY_WINDOWS: Final[tuple[str, ...]] = ("3y", "1y")
"""Longest first. One window per metric keeps the packet small; the
three-year view is the one that distinguishes companies, and the one-year is
the fallback where a series is short."""

MARKET_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("relative_strength_252d", "1Y relative strength vs benchmark"),
    ("distance_from_ma200", "Distance from 200-day average"),
    ("drawdown_from_252d_high", "Below 52-week high"),
)
VALUATION_METRICS: Final[tuple[tuple[str, str], ...]] = (
    ("pe_ttm", "P/E"),
    ("ps_ttm", "P/S"),
    ("p_fcf", "P/FCF"),
    ("ps_percentile_own_history", "P/S percentile of own history"),
)
FUNDAMENTAL_METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("revenue_ttm", "Revenue TTM", "CURRENCY"),
    ("operating_margin", "Operating margin", "PERCENT"),
    ("gross_margin", "Gross margin", "PERCENT"),
    ("fcf_margin", "FCF margin", "PERCENT"),
    ("free_cash_flow", "Free cash flow", "CURRENCY"),
    ("net_cash_or_debt", "Net cash or debt", "CURRENCY"),
    ("shares_outstanding", "Shares outstanding", "COUNT"),
)

STANDING_LIMITATIONS: Final[tuple[str, ...]] = (
    "Tradabot holds no validated mapping from any of this evidence to future "
    "returns. No claim about what follows is supported.",
    "Research events carry historical_evidence = NOT_ESTABLISHED: no event "
    "study over these event kinds exists in this system.",
    "Peer position is a rank within a declared industry group, not a judgement.",
)
"""True of every packet. Stated in the packet rather than only in a prompt, so
a synthesis cannot treat their absence as permission."""

FUND_TYPES: Final[frozenset[str]] = frozenset({"ETF", "FUND", "ETN"})


def _excerpt(text: str) -> str:
    """A quoted sentence, cut at a boundary rather than mid-number."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    cut = text[:MAX_EXCERPT_CHARS]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop > MAX_EXCERPT_CHARS // 2:
        return cut[: stop + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut) + " …"


class EvidencePacketBuilder:
    """Builds one company's packet from the services that own each number.

    Args:
        registry_snapshot: identity and sector.
        history: :class:`~app.history.CompanyHistoryService`.
        advisor: the Advisor, for fundamentals, valuation and market context.
        developments: the current-developments service.
        peers: the peer comparison service. Optional -- omitted with a reason
            rather than silently absent.
    """

    def __init__(
        self,
        *,
        registry_snapshot: Any,
        history: Any,
        advisor: Any = None,
        developments: Any = None,
        peers: Any = None,
    ) -> None:
        self._registry = registry_snapshot
        self._history = history
        self._advisor = advisor
        self._developments = developments
        self._peers = peers

    def build(self, listing: Any, *, as_of: str) -> EvidencePacket:
        """One packet. **Never raises.**"""
        identity = self._identity(listing)
        if str(listing.asset_type).upper() in FUND_TYPES:
            return EvidencePacket(
                identity=identity,
                as_of=as_of,
                omissions=(
                    Omission(
                        "company_evidence",
                        "Company research",
                        OmissionReason.NOT_APPLICABLE,
                        "a fund has no company economics to synthesise",
                    ),
                ),
                limitations=STANDING_LIMITATIONS,
            )
        from app.advisor.facts import company_key  # noqa: PLC0415

        key = company_key(int(listing.cik)) if listing.cik else str(listing.symbol)
        omissions: list[Omission] = []
        report = self._report(listing, key, as_of, omissions)
        trajectory, own_history = self._trajectory(listing, key, as_of, omissions)
        developments, primary = self._developments_for(listing, key, as_of, omissions)
        return EvidencePacket(
            identity=identity,
            as_of=as_of,
            fundamentals=self._fundamentals(report, listing, omissions),
            trajectory=trajectory,
            own_history=own_history,
            peer_context=self._peer_context(listing, report, as_of, omissions),
            market_context=self._market_context(report, listing, omissions),
            developments=developments,
            primary_source=primary,
            omissions=tuple(omissions),
            conflicts=self._conflicts(primary, report),
            freshness=self._freshness(report, as_of),
            limitations=STANDING_LIMITATIONS,
        )

    # ------------------------------------------------------------- identity
    def _identity(self, listing: Any) -> PacketIdentity:
        from app.advisor.facts import company_key  # noqa: PLC0415
        from app.instruments.registry import market_inputs  # noqa: PLC0415

        series = market_inputs(listing)[0]
        return PacketIdentity(
            company_id=getattr(listing, "company_id", None),
            company_key=company_key(int(listing.cik)) if listing.cik else str(listing.symbol),
            company_name=str(listing.company_name),
            cik=str(listing.cik) if listing.cik else None,
            sic=str(listing.sic) if listing.sic else None,
            sic_description=getattr(listing, "sic_description", None),
            listing=str(listing.qualified) if series else None,
            listing_reason=(
                "this listing's own price series"
                if series
                else "no listing carries market data; company evidence only"
            ),
            reporting_currency=getattr(listing, "reporting_currency", None),
            quote_currency=getattr(listing, "quote_currency", None) if series else None,
        )

    # ---------------------------------------------------------------- layers
    def _report(self, listing: Any, key: str, as_of: str, omissions: list[Omission]) -> Any:
        if self._advisor is None:
            omissions.append(
                Omission(
                    "advisor",
                    "Fundamentals, valuation and market context",
                    OmissionReason.NOT_AVAILABLE,
                    "the advisor was not supplied",
                )
            )
            return None
        try:
            from app.advisor.service import MarketIdentity  # noqa: PLC0415
            from app.instruments.registry import market_inputs  # noqa: PLC0415

            series, benchmark, mismatch = market_inputs(listing)
            return self._advisor.analyse(
                listing.symbol,
                as_of=as_of,
                company_key=key,
                market=MarketIdentity(series=series, benchmark=benchmark, unit_mismatch=mismatch),
            )
        except Exception as exc:
            logger.warning("advisor unavailable", symbol=listing.symbol, reason=type(exc).__name__)
            omissions.append(
                Omission(
                    "advisor",
                    "Fundamentals, valuation and market context",
                    OmissionReason.NOT_AVAILABLE,
                    "the report could not be built",
                )
            )
            return None

    def _fundamentals(
        self, report: Any, listing: Any, omissions: list[Omission]
    ) -> tuple[EvidenceItem, ...]:
        if report is None:
            return ()
        values: dict[str, Any] = {}
        for section in report.company_quality:
            values.update(section.metrics)
        currency = getattr(listing, "reporting_currency", None)
        items: list[EvidenceItem] = []
        for name, label, unit in FUNDAMENTAL_METRICS:
            metric = values.get(name)
            if metric is None or metric.value is None:
                omissions.append(
                    Omission(
                        f"fund.{name}",
                        label,
                        OmissionReason.NOT_AVAILABLE,
                        getattr(metric, "unavailable_reason", None),
                    )
                )
                continue
            provenance = next(iter(getattr(metric, "provenance", ()) or ()), None)
            items.append(
                EvidenceItem(
                    evidence_id=f"fund.{name}",
                    evidence_class=(
                        EvidenceClass.CANONICAL_FINANCIAL_FACT
                        if unit == "CURRENCY"
                        else EvidenceClass.DERIVED_METRIC
                    ),
                    label=label,
                    value=round(float(metric.value), 6),
                    unit=unit,
                    currency=currency if unit == "CURRENCY" else None,
                    provenance=Provenance(
                        source="AdvisorService",
                        concept=getattr(provenance, "concept", None),
                        unit=getattr(provenance, "unit", None),
                        period=getattr(provenance, "period_end", None),
                        filed=getattr(provenance, "filed", None),
                        accession=getattr(provenance, "accession", None),
                        status=str(getattr(metric, "basis", "")) or None,
                    ),
                )
            )
        for section in report.company_quality:
            for name, value in (getattr(section, "labels", {}) or {}).items():
                if name in ("assessment", "dilution", "ps_context"):
                    items.append(
                        EvidenceItem(
                            evidence_id=f"state.{name}",
                            evidence_class=EvidenceClass.DERIVED_METRIC,
                            label=f"{section.name.title()} state",
                            value=str(value),
                            detail="a named state the Advisor assigns, not a judgement",
                            provenance=Provenance(source="AdvisorService"),
                        )
                    )
        return tuple(items)

    def _trajectory(
        self, listing: Any, key: str, as_of: str, omissions: list[Omission]
    ) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]]:
        try:
            report = self._history.for_company(
                company_key=key,
                as_of=as_of,
                company_id=getattr(listing, "company_id", None),
                sic=listing.sic,
                asset_type=str(listing.asset_type),
            )
        except Exception:
            omissions.append(
                Omission(
                    "trajectory",
                    "Company trajectory",
                    OmissionReason.NOT_AVAILABLE,
                    "history could not be read",
                )
            )
            return (), ()
        moves: list[EvidenceItem] = []
        levels: list[EvidenceItem] = []
        for metric in TRAJECTORY_METRICS:
            found = report.get(metric)
            if found is None:
                continue
            if not found.available:
                omissions.append(
                    Omission(
                        f"traj.{metric}",
                        f"{metric} trajectory",
                        _omission_for(str(found.status)),
                        found.detail,
                    )
                )
                continue
            window = next((w for w in TRAJECTORY_WINDOWS if w in found.changes), None)
            if window is not None:
                change = found.changes[window]
                moves.append(
                    EvidenceItem(
                        evidence_id=f"traj.{metric}.{window}",
                        evidence_class=EvidenceClass.HISTORICAL_TRAJECTORY,
                        label=f"{metric} over {window}",
                        value={
                            "from": round(change.from_value, 6),
                            "to": round(change.to_value, 6),
                            "absolute": round(change.absolute, 6),
                            "annualised": (
                                round(change.annualised, 6)
                                if change.annualised is not None
                                else None
                            ),
                        },
                        unit=found.unit,
                        currency=found.currency,
                        period=f"{change.from_period} to {change.to_period}",
                        detail=str(found.direction) if found.direction else None,
                        provenance=Provenance(
                            source="CompanyHistoryService",
                            status=str(found.basis) if found.basis else None,
                        ),
                    )
                )
            if found.percentile is not None:
                levels.append(
                    EvidenceItem(
                        evidence_id=f"own.{metric}",
                        evidence_class=EvidenceClass.HISTORICAL_TRAJECTORY,
                        label=f"{metric} within its own recorded range",
                        value=round(found.percentile, 1),
                        unit="PERCENTILE",
                        period=found.history_span,
                        detail="position in this company's own history, not versus peers",
                        provenance=Provenance(source="CompanyHistoryService"),
                    )
                )
        return tuple(moves), tuple(levels)

    def _peer_context(
        self, listing: Any, report: Any, as_of: str, omissions: list[Omission]
    ) -> tuple[EvidenceItem, ...]:
        if self._peers is None or report is None:
            omissions.append(
                Omission(
                    "peers",
                    "Peer context",
                    OmissionReason.NOT_AVAILABLE,
                    "peer comparison was not supplied",
                )
            )
            return ()
        try:
            comparison = self._peers.compare(listing, report, as_of=as_of)
        except Exception:
            omissions.append(Omission("peers", "Peer context", OmissionReason.NOT_AVAILABLE, None))
            return ()
        if not comparison.available:
            omissions.append(
                Omission(
                    "peers",
                    "Peer context",
                    _omission_for(str(comparison.outcome)),
                    comparison.detail,
                )
            )
            return ()
        group = comparison.group
        return tuple(
            EvidenceItem(
                evidence_id=f"peer.{c.metric}",
                evidence_class=EvidenceClass.PEER_CONTEXT,
                label=f"{c.label} versus peers",
                value={
                    "value": round(c.value, 6),
                    "percentile": round(c.percentile, 1),
                    "peer_median": round(c.median, 6),
                },
                unit=c.unit,
                detail=f"{group.label}, {len(group.included)} peers",
                provenance=Provenance(source="PeerComparisonService", status=str(group.basis)),
            )
            for c in comparison.comparisons
        )

    def _market_context(
        self, report: Any, listing: Any, omissions: list[Omission]
    ) -> tuple[EvidenceItem, ...]:
        if report is None:
            return ()
        from app.instruments.registry import market_inputs, valuation_allowed  # noqa: PLC0415

        series, _benchmark, _mismatch = market_inputs(listing)
        if series is None:
            omissions.append(
                Omission(
                    "market",
                    "Market context",
                    OmissionReason.NO_MARKET_DATA,
                    "this listing carries no price series and never borrows another's",
                )
            )
            return ()
        items = [
            EvidenceItem(
                evidence_id=f"mkt.{name}",
                evidence_class=EvidenceClass.MARKET_CONTEXT,
                label=label,
                value=round(float(metric.value), 6),
                unit="PERCENT",
                detail=f"listing {listing.qualified}",
                provenance=Provenance(source="AdvisorService"),
            )
            for name, label in MARKET_METRICS
            if (metric := report.market_position.metrics.get(name)) is not None
            and metric.value is not None
        ]
        allowed, why = valuation_allowed(listing)
        if not allowed:
            omissions.append(
                Omission("valuation", "Valuation", OmissionReason.CURRENCY_BOUNDARY, why)
            )
            return tuple(items)
        items += [
            EvidenceItem(
                evidence_id=f"val.{name}",
                evidence_class=EvidenceClass.MARKET_CONTEXT,
                label=label,
                value=round(float(metric.value), 6),
                unit="PERCENTILE" if "percentile" in name else "MULTIPLE",
                detail=f"listing {listing.qualified}",
                provenance=Provenance(source="AdvisorService"),
            )
            for name, label in VALUATION_METRICS
            if (metric := report.valuation.metrics.get(name)) is not None
            and metric.value is not None
        ]
        return tuple(items)

    def _developments_for(
        self, listing: Any, key: str, as_of: str, omissions: list[Omission]
    ) -> tuple[tuple[EvidenceItem, ...], tuple[EvidenceItem, ...]]:
        if self._developments is None:
            omissions.append(
                Omission(
                    "developments",
                    "Current developments",
                    OmissionReason.NOT_AVAILABLE,
                    "research layer was not supplied",
                )
            )
            return (), ()
        try:
            report = self._developments.for_company(
                company_id=getattr(listing, "company_id", None),
                cik=listing.cik,
                as_of=as_of,
                company_key=key,
                asset_type=str(listing.asset_type),
            )
        except Exception:
            omissions.append(
                Omission("developments", "Current developments", OmissionReason.NOT_AVAILABLE, None)
            )
            return (), ()
        if not report.has_developments:
            omissions.append(
                Omission(
                    "developments",
                    "Current developments",
                    _omission_for(str(report.status)),
                    report.detail,
                )
            )
            return (), ()

        events: list[EvidenceItem] = []
        excerpts: list[EvidenceItem] = []
        for development in report.developments[:MAX_DEVELOPMENTS]:
            events.append(
                EvidenceItem(
                    evidence_id=f"dev.{development.accession}",
                    evidence_class=EvidenceClass.CURRENT_DEVELOPMENT,
                    label=" · ".join(dict.fromkeys(i.label for i in development.items)),
                    value={
                        "form": development.form,
                        "items": [i.item for i in development.items if i.item],
                        "kinds": [str(i.kind) for i in development.items],
                        "materiality": str(development.materiality),
                    },
                    period=development.published_at[:10],
                    detail="; ".join(i.summary for i in development.items)[:MAX_EXCERPT_CHARS],
                    provenance=Provenance(
                        source="CurrentDevelopmentsService",
                        accession=development.accession,
                        url=development.source_url,
                        status="historical_evidence=NOT_ESTABLISHED",
                    ),
                )
            )
            for figure in development.figures:
                if len(excerpts) >= MAX_SOURCE_EXCERPTS:
                    break
                excerpts.append(
                    EvidenceItem(
                        evidence_id=f"src.{development.accession}.{figure.metric}",
                        evidence_class=EvidenceClass.PRIMARY_SOURCE_FACT,
                        label=f"{figure.label} as the filing stated it",
                        value=round(float(figure.value), 6),
                        unit=figure.unit,
                        currency=figure.currency,
                        period=figure.period,
                        provenance=Provenance(
                            source="ResearchFact",
                            accession=development.accession,
                            url=development.source_url,
                        ),
                    )
                )
        if report.suppressed:
            omissions.append(
                Omission(
                    "developments.more",
                    "Further current filings",
                    OmissionReason.NOT_AVAILABLE,
                    f"{report.suppressed} further qualifying filing(s) not carried",
                )
            )
        return tuple(events), tuple(excerpts)

    # ------------------------------------------------------------ conflicts
    def _conflicts(
        self, primary: tuple[EvidenceItem, ...], report: Any
    ) -> tuple[EvidenceConflict, ...]:
        """Where a filing's own words disagree with the canonical fact store.

        Surfaced, never resolved. A press release states a figure on the day and
        the XBRL fact is filed alongside it; when the two differ materially the
        packet says so and leaves both visible, because picking the closer one
        would be choosing which source to believe on a reader's behalf.
        """
        if report is None:
            return ()
        canonical: dict[str, Any] = {}
        for section in report.company_quality:
            canonical.update(section.metrics)
        found: list[EvidenceConflict] = []
        for item in primary:
            metric = item.evidence_id.rsplit(".", 1)[-1]
            other = canonical.get(f"{metric}_ttm") or canonical.get(metric)
            if other is None or other.value is None or not isinstance(item.value, (int, float)):
                continue
            if item.unit == "PERCENT" or other.value == 0:
                continue
            drift = abs(float(item.value) - float(other.value)) / abs(float(other.value))
            if drift <= 0.02:  # noqa: PLR2004 - within rounding of a stated figure
                continue
            found.append(
                EvidenceConflict(
                    conflict_id=f"conflict.{metric}",
                    evidence_a=item.evidence_id,
                    evidence_b=f"fund.{metric}_ttm",
                    conflict_type=ConflictType.PERIOD_MISMATCH,
                    status=ConflictStatus.EXPLAINED_BY_PERIOD,
                    detail=(
                        "the filing states a single period while the canonical figure is "
                        "trailing twelve months; both are shown and neither replaces the other"
                    ),
                )
            )
        return tuple(found)

    def _freshness(self, report: Any, as_of: str) -> Freshness:
        health = None
        if self._developments is not None:
            try:
                from app.research_intelligence.ingest import (  # noqa: PLC0415
                    health as ingestion_health,
                )

                store = getattr(self._developments, "_store", None)
                health = str(ingestion_health(store).status) if store else None
            except Exception:
                health = None
        filed = None
        if report is not None:
            for section in report.company_quality:
                for metric in section.metrics.values():
                    for provenance in getattr(metric, "provenance", ()) or ():
                        stamp = getattr(provenance, "filed", None)
                        if stamp and (filed is None or str(stamp) > filed):
                            filed = str(stamp)
        return Freshness(
            fundamentals_as_of=filed,
            market_as_of=as_of if report is not None else None,
            research_ingestion=health,
            developments_as_of=as_of,
            peer_as_of=as_of if self._peers is not None else None,
            detail="each dimension is dated separately; there is no single freshness",
        )


_OMISSION_BY_STATUS: Final[dict[str, OmissionReason]] = {
    "SECTOR_MODEL_REQUIRED": OmissionReason.SECTOR_MODEL_REQUIRED,
    "INSUFFICIENT_HISTORY": OmissionReason.INSUFFICIENT_HISTORY,
    "SOURCE_LIMITATION": OmissionReason.SOURCE_LIMITATION,
    "NO_CURRENT_EVENTS": OmissionReason.NO_CURRENT_EVENTS,
    "NO_COVERAGE": OmissionReason.NO_COVERAGE,
    "NOT_APPLICABLE": OmissionReason.NOT_APPLICABLE,
    "UNAVAILABLE": OmissionReason.NOT_AVAILABLE,
    "CURRENCY_CHANGE": OmissionReason.CURRENCY_BOUNDARY,
    "INSUFFICIENT_SAMPLE": OmissionReason.INSUFFICIENT_HISTORY,
    "NO_CLASSIFICATION": OmissionReason.NOT_APPLICABLE,
}


def _omission_for(status: str) -> OmissionReason:
    """Map an owning layer's refusal onto a packet omission, keeping its name."""
    return _OMISSION_BY_STATUS.get(status, OmissionReason.NOT_AVAILABLE)
