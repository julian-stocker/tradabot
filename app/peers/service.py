"""Cross-sectional comparison over canonical Advisor figures.

This service computes no fundamental of its own. Every value it ranks was
produced by :class:`~app.advisor.service.AdvisorService` for the peer whose
value it is, through the same market identity ``/check`` would use -- so a peer
that cannot be given a price-to-earnings does not contribute one, and a Xetra
listing contributes margins without contributing multiples.

That is the whole safety argument, and it is structural rather than careful:
there is no path through this module that divides a price by a reported figure.
The one derived value, year-on-year revenue growth, is a same-company ratio of
two point-in-time fact-store reads, which carries no cross-entity or
cross-currency exposure.

Cost
----
A peer group is computed lazily and cached by ``(group, as_of)``. Measured on
the current registry, one full ``AdvisorReport`` costs ~51 ms, so the largest
four-digit group (55 companies, SIC 7372) costs ~2.8 s once and nothing
thereafter. Precomputing all 858 comparable issuers up front was rejected: it
would move ~45 s into bot startup to serve groups nobody asked for, and
``/check`` already defers its interaction.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.advisor.facts import company_key
from app.advisor.service import MarketIdentity
from app.core.logging import get_logger
from app.fundamentals.sectors import is_financial
from app.instruments.registry import Candidate, market_inputs
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
from app.peers.statistics import MIN_PEERS, percentile_rank, quantile, usable
from app.peers.universe import PeerCompany, PeerUniverse

logger = get_logger(__name__)

_YEAR_DAYS = 365
_HIGH_BAND = 75.0
_LOW_BAND = 25.0


def _shift_year(as_of: str) -> str:
    """One year before ``as_of``, for the year-on-year growth denominator."""
    from datetime import date, timedelta  # noqa: PLC0415

    return (date.fromisoformat(as_of) - timedelta(days=_YEAR_DAYS)).isoformat()


class PeerComparisonService:
    """Answers where one company sits among comparable companies.

    Args:
        universe: who is comparable to whom.
        advisor: the production Advisor. Peer values come from it unmodified.
        facts: the point-in-time fact store, for the one derived metric.
    """

    def __init__(self, *, universe: PeerUniverse, advisor: Any, facts: Any) -> None:
        self._universe = universe
        self._advisor = advisor
        self._facts = facts
        self._cache: dict[tuple[str, str], dict[int, dict[str, float]]] = {}

    # ------------------------------------------------------------------ public
    def compare(self, listing: Candidate, report: Any, *, as_of: str) -> PeerComparison:
        """One company's cross-sectional position. **Never raises.**

        ``report`` is the subject's own already-computed ``AdvisorReport``, so
        ``/check`` pays for it once. Peer reports are computed here and cached.
        """
        try:
            return self._compare(listing, report, as_of=as_of)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "peer comparison failed",
                symbol=listing.symbol,
                reason=type(exc).__name__,
            )
            return PeerComparison(
                company_id=listing.company_id,
                symbol=listing.symbol,
                as_of=as_of,
                outcome=PeerOutcome.NO_COMPARABLE_METRIC,
                detail="the peer comparison could not be completed",
            )

    # --------------------------------------------------------------- internals
    def _compare(self, listing: Candidate, report: Any, *, as_of: str) -> PeerComparison:
        if is_financial(listing.sic):
            return self._refuse(
                listing,
                as_of,
                PeerOutcome.SECTOR_MODEL_REQUIRED,
                "peers are not compared for a financial company: deposits and "
                "borrowings are the business rather than a way of funding it, so "
                "margin and multiple comparisons describe nothing here",
            )
        if not listing.sic or not listing.sec_identity:
            return self._refuse(
                listing,
                as_of,
                PeerOutcome.NO_CLASSIFICATION,
                "no SEC industry classification for this issuer, so no peer group can be formed",
            )

        group = self._group(listing, as_of)
        if group is None:
            return self._refuse(
                listing,
                as_of,
                PeerOutcome.INSUFFICIENT_SAMPLE,
                f"fewer than {MIN_PEERS} comparable companies in this industry "
                f"or its industry group; the peer definition is not widened "
                f"further to produce a result",
            )

        values = self._group_values(group, as_of)
        subject = self._subject_values(listing, report, as_of)

        comparisons: list[MetricComparison] = []
        refusals: list[MetricRefusal] = []
        for spec in V1_METRICS:
            outcome = self._compare_metric(spec, subject, values, listing.company_id)
            if isinstance(outcome, MetricComparison):
                comparisons.append(outcome)
            else:
                refusals.append(outcome)

        if not comparisons:
            return PeerComparison(
                company_id=listing.company_id,
                symbol=listing.symbol,
                as_of=as_of,
                outcome=PeerOutcome.NO_COMPARABLE_METRIC,
                group=group,
                refusals=tuple(refusals),
                detail="no metric had enough comparable peers",
            )
        return PeerComparison(
            company_id=listing.company_id,
            symbol=listing.symbol,
            as_of=as_of,
            outcome=PeerOutcome.AVAILABLE,
            group=group,
            comparisons=tuple(comparisons),
            refusals=tuple(refusals),
        )

    def _refuse(
        self, listing: Candidate, as_of: str, outcome: PeerOutcome, detail: str
    ) -> PeerComparison:
        return PeerComparison(
            company_id=listing.company_id,
            symbol=listing.symbol,
            as_of=as_of,
            outcome=outcome,
            detail=detail,
        )

    def _group(self, listing: Candidate, as_of: str) -> PeerGroup | None:
        """The most specific industry group that clears the minimum, or none."""
        sic = str(listing.sic)
        own = listing.sic_description or f"SIC {sic}"
        for basis, members, code, label in (
            (PeerBasis.SIC_4, self._universe.by_sic4(sic), sic, own),
            # Naming the fallback by its number alone would hide that the group
            # widened. Say what it widened from.
            (
                PeerBasis.SIC_3,
                self._universe.by_sic3(sic),
                sic[:3],
                f"SIC {sic[:3]}, the broader group containing {own}",
            ),
        ):
            group = self._build_group(basis, code, label, members, listing, as_of)
            if group.size >= MIN_PEERS:
                return group
        return None

    def _build_group(
        self,
        basis: PeerBasis,
        code: str,
        label: str,
        members: tuple[PeerCompany, ...],
        subject: Candidate,
        as_of: str,
    ) -> PeerGroup:
        rows: list[PeerMember] = []
        for company in members:
            if company.company_id == subject.company_id:
                rows.append(
                    PeerMember(
                        company.company_id, company.symbol, company.name, False, "the subject"
                    )
                )
                continue
            reason = self._universe.eligible(company)
            rows.append(
                PeerMember(company.company_id, company.symbol, company.name, reason is None, reason)
            )
        included = {m.company_id for m in rows if m.included}
        taxonomies = Counter(
            c.listing.taxonomy for c in members if c.company_id in included and c.listing.taxonomy
        )
        return PeerGroup(
            basis=basis,
            code=code,
            label=label,
            as_of=as_of,
            members=tuple(rows),
            subject_taxonomy=subject.taxonomy,
            peer_taxonomy=taxonomies.most_common(1)[0][0] if taxonomies else None,
        )

    # ------------------------------------------------------------------ values
    def _group_values(self, group: PeerGroup, as_of: str) -> dict[int, dict[str, float]]:
        """Metric values for every included peer, computed once per group."""
        key = (f"{group.basis}:{group.code}", as_of)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        values: dict[int, dict[str, float]] = {}
        for member in group.included:
            company = self._universe.company(member.company_id)
            if company is None:
                continue
            values[member.company_id] = self._values_for(company, as_of)
        self._cache[key] = values
        logger.info("peer group computed", group=key[0], as_of=as_of, peers=len(values))
        return values

    def _values_for(self, company: PeerCompany, as_of: str) -> dict[str, float]:
        """One peer's canonical metrics, from its own Advisor report."""
        listing = company.listing
        series, benchmark, mismatch = market_inputs(listing)
        report = self._advisor.analyse(
            listing.symbol,
            as_of=as_of,
            company_key=company_key(int(company.cik)) if company.cik else None,
            market=MarketIdentity(series=series, benchmark=benchmark, unit_mismatch=mismatch),
        )
        return self._extract(report, listing, as_of)

    def _subject_values(self, listing: Candidate, report: Any, as_of: str) -> dict[str, float]:
        return self._extract(report, listing, as_of)

    def _extract(self, report: Any, listing: Candidate, as_of: str) -> dict[str, float]:
        """Canonical values off one report. Reads only; computes no ratio."""
        sections = {s.name: s for s in report.company_quality}
        sections["VALUATION"] = report.valuation
        out: dict[str, float] = {}
        for spec in V1_METRICS:
            if spec.section is None:
                continue
            section = sections.get(spec.section)
            metric = (section.metrics or {}).get(spec.field) if section else None
            if metric is not None and metric.value is not None:
                out[spec.key] = float(metric.value)
        growth = self._revenue_growth(listing, as_of)
        if growth is not None:
            out["revenue_growth_ttm_yoy"] = growth
        return out

    def _revenue_growth(self, listing: Candidate, as_of: str) -> float | None:
        """Trailing revenue against the same measure a year earlier.

        The only value this layer derives. Both terms are the same company's
        own trailing revenue read point-in-time from the fact store, so the
        ratio is dimensionless and cannot mix entities. It can still mix
        currencies if an issuer changed presentation currency inside the
        window, which is rare and recorded here rather than silently handled.
        """
        if not listing.cik:
            return None
        key = company_key(int(listing.cik))
        now = self._facts.ttm(key, "revenue", as_of)
        prior = self._facts.ttm(key, "revenue", _shift_year(as_of))
        if now.value is None or prior.value is None or prior.value <= 0:
            return None
        return float(now.value) / float(prior.value) - 1.0

    # ------------------------------------------------------------------ ranking
    def _compare_metric(
        self,
        spec: MetricSpec,
        subject: dict[str, float],
        values: dict[int, dict[str, float]],
        subject_id: int,
    ) -> MetricComparison | MetricRefusal:
        own = subject.get(spec.key)
        if own is None:
            return MetricRefusal(spec.key, spec.label, "not available for this company")
        if not usable(own, positive_only=spec.positive_only):
            return MetricRefusal(spec.key, spec.label, "not meaningful at this value")
        peers = [
            v[spec.key]
            for cid, v in values.items()
            if cid != subject_id and usable(v.get(spec.key), positive_only=spec.positive_only)
        ]
        if len(peers) < MIN_PEERS:
            return MetricRefusal(
                spec.key,
                spec.label,
                f"only {len(peers)} comparable peers have this metric; {MIN_PEERS} are required",
            )
        return MetricComparison(
            metric=spec.key,
            label=spec.label,
            value=own,
            percentile=percentile_rank(own, peers),
            median=quantile(peers, 0.5),
            p25=quantile(peers, 0.25),
            p75=quantile(peers, 0.75),
            peer_count=len(peers),
            unit=spec.unit,
        )


def describe(comparison: PeerComparison) -> str:
    """A deterministic sentence about position. Never about merit.

    Every phrase states where a value sits in a distribution. None of them say
    better, worse, cheap, expensive, attractive or undervalued, because a
    percentile supports none of those: Tradabot has no validated relationship
    between any of these metrics and future returns, and phase 12.25 established
    that directly. A structural test asserts the vocabulary.
    """
    if not comparison.available:
        return ""
    fundamentals = [c for c in comparison.comparisons if c.unit == "PERCENT"]
    multiples = [c for c in comparison.comparisons if c.unit == "MULTIPLE"]
    parts: list[str] = []

    high = [_phrase(c.label) for c in fundamentals if c.percentile >= _HIGH_BAND]
    low = [_phrase(c.label) for c in fundamentals if c.percentile < _LOW_BAND]
    if high:
        parts.append(f"{_sentence(_join(high))} above most comparable peers.")
    if low:
        parts.append(f"{_sentence(_join(low))} below most comparable peers.")

    # One clause per direction rather than one per metric: a card listing three
    # separate multiple sentences reads as emphasis the data does not carry.
    higher = [c.label for c in multiples if c.percentile >= _HIGH_BAND]
    lower = [c.label for c in multiples if c.percentile < _LOW_BAND]
    if higher and lower:
        parts.append(
            f"The shares trade at higher {_plain(higher)} multiples than most "
            f"comparable peers, and at a lower {_plain(lower)} multiple."
            if len(lower) == 1
            else f"The shares trade at higher {_plain(higher)} and lower "
            f"{_plain(lower)} multiples than most comparable peers."
        )
    elif higher:
        parts.append(
            f"The shares trade at {'a ' if len(higher) == 1 else ''}higher "
            f"{_plain(higher)} multiple{'' if len(higher) == 1 else 's'} than "
            f"most comparable peers."
        )
    elif lower:
        parts.append(
            f"The shares trade at {'a ' if len(lower) == 1 else ''}lower "
            f"{_plain(lower)} multiple{'' if len(lower) == 1 else 's'} than "
            f"most comparable peers."
        )

    if not parts:
        parts.append("Every compared metric sits within the middle range of comparable peers.")
    return " ".join(parts)


def _phrase(label: str) -> str:
    """A metric label as it reads mid-sentence.

    Lower-cased, unless the label opens with an acronym -- "FCF margin" must
    not become "fcf margin" just because it landed after a comma.
    """
    return label if label[:2].isupper() else label[0].lower() + label[1:]


def _join(items: list[str]) -> str:
    """``['a', 'b', 'c']`` -> ``'a, b and c'``, with the verb agreeing."""
    verb = " is" if len(items) == 1 else " are"
    if len(items) == 1:
        return items[0] + verb
    return ", ".join(items[:-1]) + " and " + items[-1] + verb


def _sentence(text: str) -> str:
    """Capitalise the opening character only, leaving acronyms intact."""
    return text[0].upper() + text[1:] if text else text


def _plain(items: list[str]) -> str:
    """``['P/S', 'P/FCF']`` -> ``'P/S and P/FCF'``. Labels kept verbatim."""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
