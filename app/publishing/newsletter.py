"""The weekly market intelligence letter.

Built entirely from the Phase 12.36 aggregation primitives. This module chooses
an order and a wording; it does not decide what mattered, and it recomputes
nothing.

Portfolio-independent
---------------------
#market-trends is a market channel. The weekly letter therefore drops the
portfolio section and keeps only risks that are true of the market or the data —
an account's concentration is not news to a reader who does not hold it, and
posting it here would leak one account's position into a shared channel.

What "watch" means here
-----------------------
The letter carries a list of names to monitor. Inclusion means **materially
interesting based on observable changes**, and every name carries the specific
change that put it there. It does not mean the name is expected to outperform:
Phase 12.25 established that no company-quality or valuation relationship in this
data survives out-of-sample validation, so an expected-return claim would be
unsupported by anything in this repository.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.events import EventCategory, EventType, Severity
from app.monitoring.digest import Digest
from app.monitoring.schemas import MATERIALITY_ORDER
from app.notifications.models import NotificationMessage
from app.publishing import presentation

WATCH_LIMIT = 6
_LIGHT_WEEK = 5

DISCLAIMER = (
    "_Descriptive monitoring. Inclusion means something observable changed, not "
    "that a name is expected to outperform._"
)

_REASON_BY_KIND: dict[str, str] = {
    "VALUATION_STATE_CHANGE": "valuation moved band against its own history",
    "FUNDAMENTAL_CHANGE": "a trailing fundamental moved materially",
    "NEW_SEC_FILING": "filed with the SEC",
    "COMPANY_CONFIDENCE_CHANGE": "company-analysis confidence changed",
    "UNUSUAL_VOLUME": "traded on unusual volume",
    "UNUSUAL_VOLATILITY": "volatility rose against its own one-year level",
    "RELATIVE_STRENGTH_CHANGE": "relative strength against the benchmark crossed",
}


def _section(digest: Digest, title: str) -> Any:
    return next((s for s in digest.sections if s.title == title), None)


def _rows(digest: Digest, title: str) -> list[Mapping[str, Any]]:
    section = _section(digest, title)
    return list(section.rows) if section else []


def watch_list(
    events: Sequence[Mapping[str, Any]], *, limit: int = WATCH_LIMIT
) -> list[dict[str, str]]:
    """Names worth continued observation, each with the change that qualified it.

    A name appears once. Several changes on the same subject are combined into
    one reason, because three lines about one company crowd out three companies
    the reader has not heard about.
    """
    by_subject: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        kind = str(event.get("kind"))
        if kind not in _REASON_BY_KIND:
            continue
        subject = str(event.get("subject"))
        by_subject.setdefault(subject, []).append(event)

    scored = sorted(
        by_subject.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    out: list[dict[str, str]] = []
    for subject, found in scored[:limit]:
        reasons: list[str] = []
        for event in found:
            reason = _REASON_BY_KIND[str(event.get("kind"))]
            if reason not in reasons:
                reasons.append(reason)
        out.append({"symbol": subject, "reason": "; ".join(reasons) + "."})
    return out


def executive_summary(
    digest: Digest,
    regime: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """The four states the first screen must answer.

    Market, activity, risk and watch — each a short phrase, so a reader who
    stops after the first block still knows whether anything needs them.
    """
    market = presentation.label(str((regime or {}).get("regime") or "INSUFFICIENT_HISTORY"))

    counts = len(events)
    activity = (
        "Quiet — nothing material"
        if counts == 0
        else f"Light — {counts} material change(s)"
        if counts <= _LIGHT_WEEK
        else f"Busy — {counts} material change(s)"
    )

    risks = [r for r in _rows(digest, "Unresolved risks") if _market_risk(r)]
    risk = "None outstanding" if not risks else f"{len(risks)} unresolved"

    watch = watch_list(events)
    watching = "Nothing qualifies" if not watch else ", ".join(w["symbol"] for w in watch[:4])

    return {"Market": market, "Activity": activity, "Risk": risk, "Watch": watching}


def _market_risk(row: Mapping[str, Any]) -> bool:
    """Portfolio-scoped risks are excluded from a market channel."""
    return str(row.get("risk")) in {"DATA_NOT_READY", "WEAK_COMPANY_DATA"}


def _this_week(digest: Digest, limit: int = 5) -> list[str]:
    """The few observations worth reading first, across every section."""
    rows: list[Mapping[str, Any]] = []
    for title in ("Market", "Sectors", "Company fundamentals", "Valuation"):
        rows.extend(_rows(digest, title))
    ranked = sorted(
        rows,
        key=lambda r: -MATERIALITY_ORDER.get(str(r.get("materiality")), 0),
    )
    return [
        f"• **{r['subject']}** — {presentation.humanise(str(r['summary']))}" for r in ranked[:limit]
    ]


def render(
    digest: Digest,
    *,
    regime: Mapping[str, Any] | None = None,
    events: Sequence[Mapping[str, Any]] = (),
) -> str:
    """The letter body. Sections are supplied separately as embed fields.

    Data coverage is deliberately not here: it belongs in the last, smallest
    field, not on the first screen competing with market information.
    """
    # The title already carries the week ending; repeating it here would be the
    # duplication this redesign exists to remove.
    lines: list[str] = []
    summary = executive_summary(digest, regime, events)
    lines += [f"**{name}** · {value}" for name, value in summary.items()]
    this_week = _this_week(digest)
    if this_week:
        lines += ["", "**This week**", *this_week]
    return "\n".join(lines)


def sections(
    digest: Digest,
    *,
    regime: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    events: Sequence[Mapping[str, Any]] = (),
) -> dict[str, str]:
    """The detail blocks, in reading order, as embed fields."""
    fields: dict[str, str] = {}

    market: list[str] = []
    if regime:
        state = presentation.state(str(regime.get("regime")))
        market.append(f"Regime: **{state.label}**")
        if state.explanation:
            market.append(f"_{state.explanation}_")
        distance = regime.get("distance_from_ma200")
        if isinstance(distance, (int, float)):
            market.append(f"{distance * 100:+.1f}% vs its 200-day average")
        short, long = regime.get("volatility_20d"), regime.get("volatility_252d")
        if isinstance(short, (int, float)) and isinstance(long, (int, float)):
            market.append(
                f"Volatility {short * 100:.0f}% over 20 sessions vs {long * 100:.0f}% over a year"
            )
    fields["MARKET"] = "\n".join(market) if market else "No regime data."

    sector_rows = _rows(digest, "Sectors")
    fields["SECTORS"] = (
        "\n".join(f"• {presentation.humanise(str(r['summary']))}" for r in sector_rows[:4])
        if sector_rows
        else "No sector moved materially."
    )

    company_rows = _rows(digest, "Company fundamentals") + _rows(digest, "Valuation")
    fields["COMPANIES"] = (
        "\n".join(f"• {presentation.humanise(str(r['summary']))}" for r in company_rows[:5])
        if company_rows
        else "No material company changes."
    )

    watch = watch_list(events)
    fields["WATCH / MONITOR"] = (
        "\n".join(f"• **{w['symbol']}** — {w['reason']}" for w in watch)
        if watch
        else "Nothing currently qualifies."
    )

    risks = [r for r in _rows(digest, "Unresolved risks") if _market_risk(r)]
    fields["RISKS"] = (
        "\n".join(f"• {presentation.label(str(r['risk']))}: {r['detail']}" for r in risks)
        if risks
        else "No unresolved market or data risks."
    )

    # One compact line, deliberately last and deliberately small: implementation
    # statistics must not outrank market information on the page.
    if coverage:
        fields["Data"] = " · ".join(str(v) for v in coverage.values())
    return fields


def message(
    digest: Digest,
    *,
    week_ending: str,
    regime: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    events: Sequence[Mapping[str, Any]] = (),
    occurred_at: datetime,
) -> NotificationMessage:
    """The letter as a deliverable message."""
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        colour=presentation.COLOURS[
            presentation.worst(
                str((regime or {}).get("regime") or ""),
                *(str(e.get("kind")) for e in events),
            )
        ],
        title=f"📰 Weekly Market Intelligence — {week_ending}",
        body=render(digest, regime=regime, events=events),
        event_type=EventType.MARKET_TRENDS,
        occurred_at=occurred_at,
        key=f"weekly:{week_ending}",
        fields={
            **sections(digest, regime=regime, coverage=coverage, events=events),
            "Note": DISCLAIMER,
        },
    )
