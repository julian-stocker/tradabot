"""Aggregation primitives: what mattered over a period.

A daily run answers "is anything happening right now". A weekly read is a
different question — "of everything that happened, what were the few things
worth knowing" — and answering it by concatenating seven daily lists produces
exactly the wall of text this engine exists to avoid.

So a digest ranks and truncates. Each section answers one question, returns at
most a handful of rows, and says how many it left out rather than silently
dropping them.

Unresolved risks are not events
-------------------------------
The other sections look backwards at transitions. ``unresolved_risks`` looks at
the *current* baseline and reports conditions that are still true: a data store
that is still unsynced, an account still above the heavy-sector level, a company
still at low confidence. A condition that was reported on Monday and persisted
all week generates no further events, which is correct, and would therefore
vanish from a purely event-driven summary at exactly the point it had lasted
long enough to matter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.monitoring import materiality as rules
from app.monitoring.schemas import (
    COMPANY_KINDS,
    MARKET_KINDS,
    MATERIALITY_ORDER,
    PORTFOLIO_KINDS,
    EventKind,
)

DEFAULT_LIMIT = 5


@dataclass(frozen=True, slots=True)
class DigestSection:
    """One question, its answer, and what was left out."""

    title: str
    question: str
    rows: tuple[dict[str, Any], ...] = ()
    omitted: int = 0

    @property
    def empty(self) -> bool:
        return not self.rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "question": self.question,
            "rows": list(self.rows),
            "omitted": self.omitted,
        }


@dataclass(frozen=True, slots=True)
class Digest:
    """A period summary. Empty sections are kept, so silence is visible."""

    since: str
    until: str
    sections: tuple[DigestSection, ...] = ()
    events_considered: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def quiet(self) -> bool:
        return all(section.empty for section in self.sections)

    def as_dict(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "until": self.until,
            "quiet": self.quiet,
            "events_considered": self.events_considered,
            "sections": [s.as_dict() for s in self.sections],
            "notes": list(self.notes),
        }


def _magnitude(row: Mapping[str, Any]) -> float:
    best = 0.0
    for item in row.get("evidence") or []:
        change = item.get("change")
        if isinstance(change, (int, float)):
            best = max(best, abs(float(change)))
    return best


def _rank(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(r) for r in rows),
        key=lambda r: (
            -MATERIALITY_ORDER.get(str(r.get("materiality")), 0),
            -_magnitude(r),
            str(r.get("subject")),
        ),
    )


def _collapse(ranked: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per subject and kind: the most significant occurrence.

    Over a week the same stock can be unusually volatile on four sessions. Each
    is a real event and belongs in the journal, but four lines saying the same
    thing crowd out three other subjects the reader has not heard about yet.
    Input must already be ranked, so the first occurrence seen is the strongest.
    """
    seen: set[tuple[str, str, str | None]] = set()
    out: list[dict[str, Any]] = []
    for event in ranked:
        key = (
            str(event.get("kind")),
            str(event.get("subject")),
            (event.get("scope") or {}).get("account"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(event))
    return out


def _row(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": event.get("kind"),
        "subject": event.get("subject"),
        "previous": event.get("previous_state"),
        "current": event.get("current_state"),
        "materiality": event.get("materiality"),
        "confidence": event.get("confidence"),
        "occurred_at": event.get("occurred_at"),
        "summary": event.get("summary"),
        "account": (event.get("scope") or {}).get("account"),
    }


def _section(
    title: str,
    question: str,
    events: Sequence[Mapping[str, Any]],
    kinds: Iterable[str],
    limit: int,
) -> DigestSection:
    wanted = {str(k) for k in kinds}
    matching = _collapse(_rank(e for e in events if str(e.get("kind")) in wanted))
    return DigestSection(
        title=title,
        question=question,
        rows=tuple(_row(e) for e in matching[:limit]),
        omitted=max(0, len(matching) - limit),
    )


def biggest_market_changes(events: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT
                           ) -> DigestSection:
    return _section(
        "Market",
        "What were the biggest market changes?",
        events,
        {
            EventKind.MARKET_REGIME_CHANGE,
            EventKind.UNUSUAL_VOLUME,
            EventKind.UNUSUAL_VOLATILITY,
            EventKind.RELATIVE_STRENGTH_CHANGE,
        },
        limit,
    )


def biggest_sector_changes(events: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT
                           ) -> DigestSection:
    return _section(
        "Sectors",
        "Which sectors moved most?",
        events,
        {EventKind.SECTOR_MOVE},
        limit,
    )


def biggest_fundamental_changes(
    events: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT
) -> DigestSection:
    return _section(
        "Company fundamentals",
        "Which company fundamentals changed most?",
        events,
        {EventKind.FUNDAMENTAL_CHANGE, EventKind.NEW_SEC_FILING},
        limit,
    )


def biggest_valuation_changes(
    events: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT
) -> DigestSection:
    return _section(
        "Valuation",
        "Which valuations moved band?",
        events,
        {EventKind.VALUATION_STATE_CHANGE, EventKind.COMPANY_CONFIDENCE_CHANGE},
        limit,
    )


def most_important_portfolio_changes(
    events: Sequence[Mapping[str, Any]], limit: int = DEFAULT_LIMIT
) -> DigestSection:
    return _section(
        "Portfolios",
        "What changed in the accounts?",
        events,
        PORTFOLIO_KINDS,
        limit,
    )


def unresolved_risks(
    state: Mapping[str, Mapping[str, Mapping[str, Any]]], limit: int = DEFAULT_LIMIT
) -> DigestSection:
    """Conditions still true right now, whether or not they changed.

    Args:
        state: the current baseline, as ``{scope: {key: state}}``.
    """
    rows: list[dict[str, Any]] = []

    health = (state.get("health") or {}).get("sec_fact_store") or {}
    if health and health.get("status") not in (None, "READY"):
        rows.append(
            {
                "risk": "DATA_NOT_READY",
                "subject": "sec_fact_store",
                "detail": f"fact store is {health.get('status')}",
                "materiality": "CRITICAL",
            }
        )

    for account, portfolio in sorted((state.get("portfolio") or {}).items()):
        for sector, weight in sorted((portfolio.get("sector_weights") or {}).items()):
            if isinstance(weight, (int, float)) and weight >= rules.SECTOR_HEAVY_LEVEL:
                rows.append(
                    {
                        "risk": "SECTOR_CONCENTRATED",
                        "subject": f"{account}:{sector}",
                        "detail": f"{weight * 100:.1f}% of equity in {sector}",
                        "materiality": "SIGNIFICANT",
                    }
                )
        correlation = portfolio.get("average_correlation")
        if (
            isinstance(correlation, (int, float))
            and correlation >= rules.CORRELATION_BANDS["p90"]
        ):
            rows.append(
                {
                    "risk": "HIGH_INTERNAL_CORRELATION",
                    "subject": account,
                    "detail": (
                        f"average internal correlation {correlation:.2f}, at or above "
                        f"the 90th percentile of real equity pairs"
                    ),
                    "materiality": "SIGNIFICANT",
                }
            )
        if str(portfolio.get("concentration")) == "HIGH_CONCENTRATION":
            rows.append(
                {
                    "risk": "CONCENTRATED_PORTFOLIO",
                    "subject": account,
                    "detail": f"top three are {(portfolio.get('top3_pct') or 0) * 100:.1f}%",
                    "materiality": "SIGNIFICANT",
                }
            )

    for symbol, company in sorted((state.get("company") or {}).items()):
        if company.get("available") and company.get("confidence") in ("LOW", "INSUFFICIENT"):
            rows.append(
                {
                    "risk": "WEAK_COMPANY_DATA",
                    "subject": symbol,
                    "detail": f"company-analysis confidence is {company.get('confidence')}",
                    "materiality": "NOTABLE",
                }
            )

    ordered = sorted(
        rows, key=lambda r: (-MATERIALITY_ORDER[str(r["materiality"])], str(r["subject"]))
    )
    return DigestSection(
        title="Unresolved risks",
        question="What is still true and still worth watching?",
        rows=tuple(ordered[:limit]),
        omitted=max(0, len(ordered) - limit),
    )


def build(
    events: Sequence[Mapping[str, Any]],
    state: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    since: str,
    until: str,
    limit: int = DEFAULT_LIMIT,
) -> Digest:
    """Every section, in reading order."""
    return Digest(
        since=since,
        until=until,
        sections=(
            biggest_market_changes(events, limit),
            biggest_sector_changes(events, limit),
            biggest_fundamental_changes(events, limit),
            biggest_valuation_changes(events, limit),
            most_important_portfolio_changes(events, limit),
            unresolved_risks(state, limit),
        ),
        events_considered=len(events),
        notes=(
            "Descriptive only. No section expresses an action or a forecast.",
            f"Sections show at most {limit} rows and state how many were omitted.",
        ),
    )


__all__ = [
    "COMPANY_KINDS",
    "MARKET_KINDS",
    "Digest",
    "DigestSection",
    "biggest_fundamental_changes",
    "biggest_market_changes",
    "biggest_sector_changes",
    "biggest_valuation_changes",
    "build",
    "most_important_portfolio_changes",
    "unresolved_risks",
]
