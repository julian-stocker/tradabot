"""Answering ``/check`` by asking the layers that already know.

This module orchestrates. It does not analyse.

Every figure in a ``/check`` response was computed by a service that owns it:
trailing revenue, margins, cash generation, the balance sheet, dilution and the
valuation percentile come from :class:`~app.advisor.service.AdvisorService`;
weights, concentration and correlation come from Portfolio Fit. Nothing here
recomputes any of them, and a structural test asserts that this package contains
no such arithmetic.

The reason is not tidiness. A second implementation of trailing revenue would
agree with the first for about a quarter, and then a company would change its
reporting taxonomy and Discord would quietly start disagreeing with the Advisor
about the same company on the same day.

Company analysis only
---------------------
``/check`` answers "what do we know about this company and its stock". It
deliberately does **not** answer "how would this fit my portfolio": that is a
different question with a different owner, and putting both on one card meant a
reader had to separate them by eye every time.

The practical consequence is that ``/check`` needs no broker at all. It cannot
be slowed by an Alpaca timeout, cannot be degraded by an unconfigured account,
and works identically whether or not any paper slot is readable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.discord_bot.resolve import Availability, Resolution, Resolved, resolve
from app.discord_bot.timing import Timings
from app.instruments import registry as reg

logger = get_logger(__name__)


def _valuation_refusal(listing: Any) -> str | None:
    """Why this listing may not be given a valuation ratio, if it may not.

    ``valuation_allowed`` has existed since the identity model landed; until it
    was called, Canadian National showed a P/E of 16.75x built from CAD earnings
    and a USD price, and Novo Nordisk showed 1.99x -- a number that looks like a
    bargain and is a unit error.
    """
    if listing is None:
        return None
    from app.instruments.registry import valuation_allowed  # noqa: PLC0415

    allowed, reason = valuation_allowed(listing)
    return None if allowed else reason


def _company_key(listing: Any) -> str | None:
    """The fact-store key for a resolved listing's company, when it has one."""
    cik = getattr(listing, "cik", None)
    if not cik:
        return None
    from app.advisor.facts import company_key  # noqa: PLC0415

    return company_key(int(cik))


def _market_identity(listing: Any) -> Any:
    """Which price series this listing may be reported from, and against what.

    ``None`` for a caller with no registry, which means "the symbol names its
    own prices" -- correct when the only thing known is a bare US ticker.

    For a resolved listing the answer comes from the listing itself: its own
    series when it has one, and nothing when it does not. Passing the symbol
    through instead is what let ``SAP.DE`` reach the ADR's price history and
    ``CNR.TO`` reach a different company's altogether. The renderer already
    declined to print either; the report should not have contained them.
    """
    if listing is None:
        return None
    from app.advisor.service import MarketIdentity  # noqa: PLC0415
    from app.instruments.registry import market_inputs  # noqa: PLC0415

    # One owner for all three rules. `market_inputs` composes `valuation_allowed`
    # and `benchmark_for`, so a consumer reading `pe_ttm` directly cannot get
    # Novo Nordisk's 1.99x while the card is refusing to show it -- and the peer
    # layer builds its market identities from the same function.
    series, benchmark, mismatch = market_inputs(listing)
    return MarketIdentity(series=series, benchmark=benchmark, unit_mismatch=mismatch)


@dataclass(frozen=True, slots=True)
class StockCheck:
    """Everything one ``/check`` produced, ready to render.

    The two availability flags are independent on purpose. A security may have
    prices without fundamentals or the reverse, and flattening them into a
    single "supported" would lose exactly the distinction a user needs when
    something is missing.
    """

    requested: str
    symbol: str
    resolution: Resolution
    market_data: Availability
    fundamentals: Availability
    as_of: str
    checked_at: datetime
    report: Any = None
    """The Advisor's own report object, unmodified."""
    suggestion: str | None = None
    detail: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    listing: Any = None
    """The resolved listing, when the registry supplied one."""
    candidates: tuple[Any, ...] = ()
    """Every listing a bare ticker named. Populated when the answer is a refusal
    to choose."""
    valuation_refusal: str | None = None
    """Why a valuation ratio must not be shown for this listing, when it must
    not. Distinct from having no prices: a US-listed foreign issuer *has* prices
    and still cannot be given a P/E, because the price and the earnings are in
    different currencies and Tradabot performs no conversion."""
    peers: Any = None
    """The :class:`~app.peers.schemas.PeerComparison`, when one was computed.
    Carries its own refusal, so ``None`` means the peer layer was not wired in
    at all rather than that the comparison was declined."""
    developments: Any = None
    """The :class:`~app.research_intelligence.developments.CurrentDevelopments`
    report. Same convention as ``peers``: ``None`` means the research layer was
    not wired in, while a report carrying ``UNAVAILABLE`` means it was and the
    store is missing. Those are different things and the card says so."""

    @property
    def analysable(self) -> bool:
        return self.report is not None or self.market_data is Availability.AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "symbol": self.symbol,
            "resolution": str(self.resolution),
            "market_data": str(self.market_data),
            "valuation_refusal": self.valuation_refusal,
            "fundamentals": str(self.fundamentals),
            "as_of": self.as_of,
            "checked_at": self.checked_at.isoformat(),
            "suggestion": self.suggestion,
            "detail": self.detail,
            "notes": list(self.notes),
        }


class StockAnalyst:
    """Turns a typed ticker into a :class:`StockCheck`.

    Args:
        advisor: the production Advisor. Not a subset of it.
        universe: canonical instrument symbols with price history.
        fundamentals: symbols the fact store holds, for resolution.
        fact_store_ready: whether the fact store is usable.
        as_of: latest session the data covers.
    """

    def __init__(
        self,
        *,
        advisor: Any,
        universe: Sequence[str],
        fundamentals: frozenset[str] | None,
        fact_store_ready: bool,
        as_of: str,
        registry: Any = None,
        peers: Any = None,
        developments: Any = None,
    ) -> None:
        self._advisor = advisor
        self._universe = universe
        self._fundamentals = fundamentals
        self._ready = fact_store_ready
        self._as_of = as_of
        self._registry = registry
        self._peers = peers
        """Peer comparison service. Optional: a bot wired without one answers
        exactly as before, so the card degrades by omitting a section rather
        than by failing."""
        self._developments = developments
        """Current-developments service. Optional on the same terms, and it
        reads only the local research store -- ``/check`` performs no network
        request, so SEC being slow or unreachable cannot delay a card."""
        """Company/listing registry. When present it owns resolution, so a bare
        ticker naming two companies refuses instead of picking one."""

    def check(
        self, raw: str, *, now: datetime | None = None, timings: Timings | None = None
    ) -> StockCheck:
        """Analyse one symbol. **Never raises.**

        A command handler that could throw would leave the user staring at
        Discord's "the application did not respond", which says nothing about
        what went wrong.
        """
        moment = now or datetime.now(UTC)
        clock = timings or Timings()
        with clock.stage("resolve"):
            if self._registry is not None:
                found, listing = self._from_registry(raw)
            else:
                found, listing = self._legacy(raw), None
        if found.resolution in (
            Resolution.UNKNOWN_SYMBOL,
            Resolution.MALFORMED_SYMBOL,
            Resolution.AMBIGUOUS_SYMBOL,
            Resolution.UNSUPPORTED_LISTING,
        ):
            return self._empty(found, moment, listing)

        report = None
        notes: list[str] = []
        if found.fundamentals is Availability.AVAILABLE or self._ready:
            try:
                with clock.stage("advisor"):
                    # Facts belong to the company, prices to the listing, and
                    # both are named explicitly. When the registry knows a CIK
                    # the Advisor is asked by company identity, so every listing
                    # of one issuer sees the same filings and no ticker
                    # collision can reach the fact store; the market identity
                    # does the same job for prices in the other direction.
                    report = self._advisor.analyse(
                        found.symbol,
                        as_of=self._as_of,
                        company_key=_company_key(listing),
                        market=_market_identity(listing),
                    )
            except Exception as exc:
                logger.warning(
                    "advisor analysis failed",
                    symbol=found.symbol,
                    reason=type(exc).__name__,
                )
                return StockCheck(
                    requested=found.requested,
                    symbol=found.symbol,
                    resolution=Resolution.ANALYSIS_FAILED,
                    market_data=found.market_data,
                    fundamentals=found.fundamentals,
                    as_of=self._as_of,
                    checked_at=moment,
                    detail="The analysis could not be completed for this symbol.",
                )

        if found.resolution is Resolution.DATA_NOT_SYNCED:
            notes.append("Company fundamentals are unavailable until the fact store is synced.")
        peers = None
        if self._peers is not None and listing is not None and report is not None:
            with clock.stage("peers"):
                peers = self._peers.compare(listing, report, as_of=self._as_of)
        developments = None
        if self._developments is not None and listing is not None:
            with clock.stage("developments"):
                # Asked by resolved company identity, never by ticker, so every
                # listing of one issuer sees one event history and a ticker
                # collision cannot reach another company's filings.
                developments = self._developments.for_company(
                    company_id=getattr(listing, "company_id", None),
                    cik=getattr(listing, "cik", None),
                    as_of=self._as_of,
                    company_key=_company_key(listing),
                    asset_type=str(getattr(listing, "asset_type", "STOCK")),
                )
        return StockCheck(
            requested=found.requested,
            symbol=found.symbol,
            resolution=found.resolution,
            market_data=found.market_data,
            fundamentals=found.fundamentals,
            as_of=self._as_of,
            checked_at=moment,
            report=report,
            suggestion=found.suggestion,
            detail=found.detail,
            notes=tuple(notes),
            listing=listing,
            candidates=found.candidates,
            valuation_refusal=_valuation_refusal(listing),
            peers=peers,
            developments=developments,
        )

    # ------------------------------------------------------------- internals
    def _legacy(self, raw: str) -> Resolved:
        """Price-universe resolution, kept for callers with no registry."""
        return resolve(
            raw,
            universe=self._universe,
            fundamentals=self._fundamentals,
            fact_store_ready=self._ready,
        )

    def _from_registry(self, raw: str) -> tuple[Resolved, Any]:
        """Registry resolution, mapped onto the bot's own outcome vocabulary."""
        found = self._registry.resolve(raw)
        listing = found.listing
        mapped = {
            "SUPPORTED": Resolution.SUPPORTED,
            "MARKET_DATA_ONLY": Resolution.MARKET_DATA_ONLY,
            "FUNDAMENTALS_ONLY": Resolution.FUNDAMENTALS_ONLY,
            "AMBIGUOUS_SYMBOL": Resolution.AMBIGUOUS_SYMBOL,
            "UNKNOWN_SYMBOL": Resolution.UNKNOWN_SYMBOL,
            "MALFORMED_SYMBOL": Resolution.MALFORMED_SYMBOL,
            "UNSUPPORTED_LISTING": Resolution.UNSUPPORTED_LISTING,
        }[str(found.resolution)]
        symbol = listing.symbol if listing else reg.split_suffix(raw)[0]
        return (
            Resolved(
                requested=raw,
                symbol=symbol,
                resolution=mapped,
                market_data=(
                    Availability.AVAILABLE
                    if listing and listing.has_prices
                    else Availability.UNAVAILABLE
                ),
                fundamentals=(
                    Availability.AVAILABLE
                    if listing and listing.has_fundamentals
                    else Availability.UNAVAILABLE
                ),
                detail=found.detail,
                candidates=tuple(c.as_dict() for c in found.candidates),
            ),
            listing,
        )

    def _empty(self, found: Resolved, moment: datetime, listing: Any = None) -> StockCheck:
        return StockCheck(
            requested=found.requested,
            symbol=found.symbol,
            resolution=found.resolution,
            market_data=found.market_data,
            fundamentals=found.fundamentals,
            as_of=self._as_of,
            checked_at=moment,
            suggestion=found.suggestion,
            detail=found.detail,
            listing=listing,
            candidates=found.candidates,
        )
