"""Rendering monitored changes as Discord messages.

Presentation only
-----------------
Nothing here decides whether something is worth sending. Materiality,
deduplication, cooldowns, ranking and weekly aggregation all belong to
:mod:`app.monitoring`, which is their single owner. This module is handed things
that already survived those rules and turns them into text.

The consequence worth stating: if a threshold looks wrong, it is changed in
``app/monitoring/materiality.py`` and every channel follows. There is no second
set of rules here to drift from it.

Vocabulary
----------
Allowed: watch, monitor, review, risk, concentration, fundamental change,
valuation change, unusual activity. A message describes what moved and by how
much.

Never: an action, a price target, an expected return, a probability of profit.
No validated predictive evidence in this repository supports any of them, and a
structural test asserts that vocabulary never appears in this package.

Length
------
Discord's hard cap is 2000 characters. Messages are built compactly and
truncated from the end, because the important material — subject, what changed,
by how much — comes first. When a burst exceeds what one message can hold, the
count of what was left out is stated rather than the tail being dropped in
silence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.core.events import EventCategory, EventType, Severity
from app.monitoring.schemas import ChangeEvent, Materiality
from app.notifications.models import NotificationMessage
from app.publishing import presentation
from app.publishing.coverage import label as coverage_label

BURST_THRESHOLD = 5
"""Above this many events for one destination, a single ranked digest is sent
instead of individual messages. Phase 12.36 measured 53 genuine changes in one
earnings-season session; fifty-three separate posts is how a channel teaches
people to mute it."""

DIGEST_ROWS = 12
"""Rows shown in a burst digest. The rest are counted, never silently dropped."""

DISCLAIMER = "This is factual monitoring, not an investment recommendation."
PORTFOLIO_DISCLAIMER = "Portfolio analysis only. No order was placed, and no action is recommended."

_ICON: dict[str, str] = {
    "MARKET_REGIME_CHANGE": "🌐",
    "SECTOR_MOVE": "🧭",
    "UNUSUAL_VOLUME": "📊",
    "UNUSUAL_VOLATILITY": "📈",
    "RELATIVE_STRENGTH_CHANGE": "↔️",
    "NEW_SEC_FILING": "📄",
    "FUNDAMENTAL_CHANGE": "🏭",
    "VALUATION_STATE_CHANGE": "🏷️",
    "COMPANY_CONFIDENCE_CHANGE": "🔎",
    "DATA_HEALTH_CHANGE": "🛠️",
}

_SEVERITY: dict[str, Severity] = {
    "CRITICAL": Severity.CRITICAL,
    "SIGNIFICANT": Severity.WARNING,
    "NOTABLE": Severity.INFO,
    "ROUTINE": Severity.INFO,
}


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC")


def _severity(materiality: Materiality) -> Severity:
    return _SEVERITY.get(str(materiality), Severity.INFO)


def _threshold_line(event: ChangeEvent) -> str | None:
    for item in event.evidence:
        if item.threshold is not None:
            unit = f" {item.unit}" if item.unit and item.unit != "fraction" else ""
            return f"{item.threshold}{unit}"
    return None


def event_message(
    event: ChangeEvent, *, context: Mapping[str, Any] | None = None
) -> NotificationMessage:
    """One market or company change as a compact alert card.

    The card answers, in order: what happened, how unusual it is, why Tradabot
    surfaced it, what the state means, how confident the observation is, and
    when it was observed. Anything a reader would have to know Tradabot's
    internals to decode carries its explanation with it.
    """
    icon = _ICON.get(str(event.kind), "•")
    kind = presentation.state(str(event.kind))
    fields: dict[str, str] = {}

    if event.previous_state:
        fields["Previous"] = event.previous_state
    fields["Current"] = event.current_state

    for item in event.evidence:
        if item.threshold is not None:
            unit = f" {item.unit}" if item.unit and item.unit != "fraction" else ""
            fields["Threshold"] = f"{item.threshold}{unit}"
            if item.comparison:
                fields["Measured"] = item.comparison
            break

    fields["Materiality"] = presentation.label(str(event.materiality))
    fields["Confidence"] = presentation.label(str(event.confidence))
    for name, value in (context or {}).items():
        if value:
            fields[str(name).title()] = str(value)

    # The state's own explanation, plus the destination state's where the two
    # differ -- "unusual volatility" and "very high vs its own history" mean
    # different things and both may be on the card.
    meaning = [
        text for text in (kind.explanation, presentation.explain(event.current_state)) if text
    ]
    body = presentation.humanise(event.summary)
    if meaning:
        body += "\n\n**Meaning:** " + " ".join(dict.fromkeys(meaning))

    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=_severity(event.materiality),
        colour=presentation.colour(str(event.kind)),
        title=f"{icon} {event.subject} — {kind.label.upper()}",
        body=body,
        event_type=EventType.MARKET_TRENDS,
        occurred_at=event.occurred_at,
        key=event.key(),
        fields=fields,
    )


def burst_message(events: Sequence[ChangeEvent], *, rows: int = DIGEST_ROWS) -> NotificationMessage:
    """Many changes as one ranked digest.

    Events arrive already ranked by the monitoring engine, so truncation always
    drops the least important. The omitted count is stated because a reader who
    cannot tell a busy day from a capped list has been told something false.

    The spine takes the most attention-worthy category present, so a digest
    containing one genuinely bad condition does not read as routine.
    """
    shown = list(events[:rows])
    omitted = len(events) - len(shown)
    lines = [
        f"`{index:>2}` **{e.subject}** · {presentation.label(str(e.kind))} · "
        f"{presentation.label(str(e.materiality))}"
        for index, e in enumerate(shown, start=1)
    ]
    if omitted:
        lines.append(f"\n_+{omitted} further change(s) recorded and not shown._")
    fields = {
        "Ranked by": "materiality, then confidence, then magnitude",
        "Shown": f"{len(shown)} of {len(events)}",
    }
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=max(
            (_severity(e.materiality) for e in events),
            key=lambda s: list(Severity).index(s),
            default=Severity.INFO,
        ),
        colour=presentation.COLOURS[presentation.worst(*(str(e.kind) for e in events))],
        title=f"⚡ {len(events)} material market changes",
        body="\n".join(lines),
        event_type=EventType.MARKET_TRENDS,
        occurred_at=events[0].occurred_at,
        key=f"burst:{events[0].occurred_at.date().isoformat()}",
        fields=fields,
    )


def portfolio_message(
    account: str,
    exposure: Any,
    risk: Any,
    *,
    events: Sequence[ChangeEvent] = (),
    holdings: Sequence[Mapping[str, Any]] = (),
    cluster: Mapping[str, Any] | None = None,
    coverage: str | None = None,
    coverage_state: str | None = None,
    contexts: Mapping[str, Any] | None = None,
    confidence: str | None = None,
    occurred_at: datetime,
) -> NotificationMessage:
    """One account as a dashboard.

    Only ever this account. The caller resolves the channel from the account
    name, and a slot with no configured webhook produces no message rather than
    borrowing another slot's.

    Company quality and portfolio fit are kept in separate blocks throughout. A
    strong company can be a poor fit and a weak one can diversify, so merging
    them into a single verdict would hide exactly the cases worth seeing.
    """
    fields: dict[str, str] = {}
    coverage_text = coverage or coverage_label(account)
    coverage_explanation = presentation.explain(coverage_state or "ALPACA_ACCOUNT_ONLY")

    fields["Equity"] = f"${exposure.equity:,.2f}"
    fields["Cash"] = f"${exposure.cash:,.2f} ({exposure.cash_pct * 100:.1f}%)"
    fields["Positions"] = str(len(holdings))

    if not holdings:
        body = "This account holds no positions, so there is no exposure to describe."
    else:
        band = presentation.state(str(exposure.concentration))
        fields["Concentration"] = band.label
        fields["Top 3"] = f"{exposure.top3_pct * 100:.1f}%"
        largest = sorted(exposure.weights.items(), key=lambda kv: -kv[1])[:3]
        fields["Largest holdings"] = " · ".join(
            f"{symbol} {weight * 100:.1f}%" for symbol, weight in largest
        )
        sectors = sorted(exposure.sector_weights.items(), key=lambda kv: -kv[1])[:3]
        if sectors:
            fields["Sector exposure"] = (
                " · ".join(f"{name} {weight * 100:.1f}%" for name, weight in sectors)
                + "\n_Sector labels are proxy-derived, not an official classification._"
            )
        if risk.annualised_volatility is not None:
            fields["Historical volatility"] = f"{risk.annualised_volatility * 100:.1f}% annualised"
        if risk.average_correlation is not None:
            fields["Average correlation"] = f"{risk.average_correlation:.2f}"
        if cluster and cluster.get("symbols"):
            overlap = presentation.state(str(cluster.get("overlap")))
            fields["Overlap cluster"] = (
                f"{' · '.join(cluster['symbols'])}\n"
                f"{cluster['weight'] * 100:.1f}% of equity — {overlap.label}"
                + (f"\n_{overlap.explanation}_" if overlap.explanation else "")
            )
        body = band.explanation or ""

    if contexts:
        blocks = [
            company_context_block(symbol, ctx, exposure.weights.get(symbol))
            for symbol, ctx in list(contexts.items())[:3]
        ]
        if blocks:
            fields["Company context"] = (
                "\n\n".join(blocks) + "\n_Company quality is separate from portfolio fit._"
            )

    if events:
        flagged = []
        for e in events[:5]:
            explanation = presentation.explain(e.current_state)
            flagged.append(
                f"• **{presentation.label(str(e.kind))}** — "
                f"{presentation.humanise(e.summary)}"
                + (f"\n  _{explanation}_" if explanation else "")
            )
        if len(events) > 5:  # noqa: PLR2004 -- display cap, stated in the field
            flagged.append(f"• _+{len(events) - 5} further change(s) recorded._")
        fields["Flags"] = "\n".join(flagged)

    if confidence:
        fit = presentation.state(confidence)
        fields["Portfolio-fit confidence"] = f"{fit.label}" + (
            f" — {fit.explanation}" if fit.explanation else ""
        )

    fields["Coverage"] = coverage_text + (
        f"\n_{coverage_explanation}_" if coverage_explanation else ""
    )

    return NotificationMessage(
        category=EventCategory.PERFORMANCE,
        severity=Severity.INFO,
        colour=presentation.COLOURS[
            presentation.worst(
                str(exposure.concentration) if holdings else None,
                str(cluster.get("overlap")) if cluster else None,
                # Coverage tints the card too: a confident green dashboard that
                # silently describes a fraction of someone's holdings is the
                # misreading this whole label exists to prevent.
                coverage_state,
                *(e.current_state for e in events),
            )
        ],
        title=f"📊 {account} — portfolio",
        body=body,
        event_type=EventType.PORTFOLIO_PERFORMANCE_SUMMARY,
        occurred_at=occurred_at,
        key=f"portfolio:{account}:{occurred_at.date().isoformat()}",
        fields=fields,
    )


def status_message(fields: Mapping[str, str], *, occurred_at: datetime) -> NotificationMessage:
    """The operational dashboard, grouped so liveness reads separately.

    External liveness comes first and states its own limitation. Every other
    field here is produced by this machine, so none of them can distinguish a
    quiet market from a stopped host -- and a dashboard whose freshest line is
    "last monitor run 1m ago" invites exactly that misreading.
    """
    liveness = str(fields.get("Server heartbeat", "NOT CONFIGURED"))
    unconfigured = "NOT CONFIGURED" in liveness.upper()
    grouped: dict[str, str] = {
        "EXTERNAL LIVENESS": (
            "**NOT CONFIGURED** — this Discord status cannot detect a stopped host."
            if unconfigured
            else liveness
        ),
        "APPLICATION HEALTH": str(fields.get("Application", "UNKNOWN")),
        "DATA HEALTH": str(fields.get("Last fundamentals sync", "unknown")),
        "DISCORD DELIVERY": (
            f"{fields.get('Discord delivery', 'UNKNOWN')} · "
            f"{fields.get('Pending delivery failures', '0')} pending failure(s)"
        ),
        "MONITOR ACTIVITY": (
            f"last run {fields.get('Last monitor run', 'never')}\n"
            f"last market event {fields.get('Last market event', 'never')}"
        ),
    }
    return NotificationMessage(
        category=EventCategory.SYSTEM,
        severity=Severity.INFO,
        colour=presentation.COLOURS[
            presentation.worst(
                "NOT CONFIGURED" if unconfigured else "HEALTHY",
                str(fields.get("Application", "UNKNOWN")),
                str(fields.get("Discord delivery", "UNKNOWN")),
            )
        ],
        title="🩺 Tradabot — operational status",
        body=(
            "Activity is not liveness: every field below except external liveness "
            "is produced by this host."
        ),
        event_type=EventType.OPERATIONAL_STATUS,
        occurred_at=occurred_at,
        key="status",
        fields=grouped,
    )


def company_context_block(symbol: str, context: Any, weight: float | None = None) -> str:
    """Compact Advisor context for a held company. Borrowed, never derived."""
    if context is None or not getattr(context, "available", False):
        return f"**{symbol}**\nCompany context: unavailable"
    labels = dict(getattr(context, "labels", {}) or {})
    lines = [f"**{symbol}**", f"Company analysis: {context.confidence} confidence"]
    if context.valuation_context:
        lines.append(f"Valuation: {context.valuation_context}")
    if labels.get("assessment"):
        lines.append(f"Balance sheet: {labels['assessment']}")
    if context.market_position:
        lines.append(f"Market position: {context.market_position}")
    if weight is not None:
        lines.append(f"Portfolio: {weight * 100:.1f}% weight")
    return "\n".join(lines)


def hypothetical_message(
    account: str, fit: Any, *, amount: float, occurred_at: datetime
) -> NotificationMessage:
    """A configured diagnostic amount evaluated against one account.

    Sent only when an amount is explicitly configured. The publisher never
    invents one — a size nobody chose would read as a suggested position.
    """
    lines = [
        f"**{fit.symbol} — hypothetical ${amount:,.0f} addition to {account}**",
        "",
        "This is a **HYPOTHETICAL** diagnostic. No order was submitted.",
        "",
    ]
    if fit.after is not None:
        lines.append(
            f"**Position weight after:** {fit.after.weights.get(fit.symbol, 0.0) * 100:.1f}%"
        )
        for row in fit.deltas():
            if str(row["measure"]).startswith("sector::") and row["delta"]:
                sector = str(row["measure"]).removeprefix("sector::")
                lines.append(
                    f"**{sector} exposure:** {row['before'] * 100:.0f}% → {row['after'] * 100:.0f}%"
                )
    if fit.weighted_average_correlation is not None:
        lines.append(f"**Average correlation:** {fit.weighted_average_correlation:.2f}")
    lines.append(f"**Overlap:** {fit.state}")
    if fit.context is not None and fit.context.available:
        lines += ["", company_context_block(fit.symbol, fit.context)]
    lines += ["", "No order submitted. No recommendation generated.", PORTFOLIO_DISCLAIMER]
    return NotificationMessage(
        category=EventCategory.PERFORMANCE,
        severity=Severity.INFO,
        title=f"🧪 HYPOTHETICAL — {fit.symbol} in {account}",
        body="\n".join(lines),
        event_type=EventType.PORTFOLIO_PERFORMANCE_SUMMARY,
        occurred_at=occurred_at,
        key=f"hypothetical:{account}:{fit.symbol}:{occurred_at.date().isoformat()}",
    )


TEST_PREFIX = "TRADABOT TEST"
"""Every smoke-test message opens with this, visibly, so a reader who sees one
in a live channel knows immediately that it is a delivery check and not a
market observation."""


def smoke_test_message(
    *, destination: str, purpose: str, detail: Sequence[str] = (), occurred_at: datetime
) -> NotificationMessage:
    """A controlled delivery test for one destination.

    Deliberately carries no market content. The point of a smoke test is to
    prove the transport and the routing, and manufacturing an event to force a
    delivery would put a fabricated observation into a channel whose whole value
    is that everything in it is real.
    """
    lines = [
        "This is an **operational delivery test**.",
        "It is not a market signal, not a recommendation, and not a trading action.",
        "",
        f"**Destination:** {destination}",
        f"**Purpose:** {purpose}",
    ]
    if detail:
        lines += ["", *detail]
    lines += [
        "",
        f"Sent: {_stamp(occurred_at)}",
        "",
        "No order was placed. Normal publishing is unaffected by this message.",
    ]
    return NotificationMessage(
        category=EventCategory.SYSTEM,
        severity=Severity.INFO,
        title=f"🧪 {TEST_PREFIX} — {destination}",
        body="\n".join(lines),
        event_type=EventType.NOTIFICATION_TEST,
        occurred_at=occurred_at,
        key=f"smoke:{destination}:{occurred_at.date().isoformat()}",
    )


def recovery_message(
    *, accumulated: int, still_relevant: Sequence[ChangeEvent], occurred_at: datetime
) -> NotificationMessage:
    """One bounded notice after a delivery outage.

    Goes to the system channel, not to an alert channel: that a transport was
    down is an infrastructure fact, and discharging a backlog into market-signals
    would bury whatever is actually happening now under yesterday's news.
    """
    lines = [
        f"{accumulated} alert(s) accumulated while delivery was failing.",
        "",
    ]
    if still_relevant:
        lines.append("**Still relevant:**")
        lines += [
            f"{i}. {e.subject} — {e.kind} — {e.materiality}"
            for i, e in enumerate(still_relevant, start=1)
        ]
    else:
        lines.append("None of the accumulated alerts are still current.")
    lines += [
        "",
        "The backlog was not replayed. Normal delivery has resumed.",
    ]
    return NotificationMessage(
        category=EventCategory.SYSTEM,
        severity=Severity.WARNING,
        title="🔄 DISCORD DELIVERY RECOVERED",
        body="\n".join(lines),
        event_type=EventType.PROVIDER_RECOVERED,
        occurred_at=occurred_at,
        key=f"recovery:{occurred_at.date().isoformat()}",
    )


def failure_notice(
    *, failures: int, destinations: Sequence[str], occurred_at: datetime
) -> NotificationMessage:
    """A delivery problem, reported where infrastructure problems belong."""
    return NotificationMessage(
        category=EventCategory.SYSTEM,
        severity=Severity.WARNING,
        title="⚠️ DISCORD DELIVERY DEGRADED",
        body="\n".join(
            [
                f"{failures} message(s) failed to deliver.",
                f"Affected destinations: {', '.join(sorted(set(destinations))) or 'unknown'}",
                "",
                "Analysis, monitoring and paper accounting are unaffected;"
                " delivery is output-only.",
            ]
        ),
        event_type=EventType.CRITICAL_SYSTEM_ERROR,
        occurred_at=occurred_at,
        key=f"delivery-degraded:{occurred_at.date().isoformat()}",
    )
