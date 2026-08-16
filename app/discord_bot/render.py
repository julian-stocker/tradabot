"""The ``/check`` embed.

Presentation only, and borrowed presentation at that: the colours, the state
labels and the explanations all come from :mod:`app.publishing.presentation`,
which Phase 12.39 established as their single owner. Nothing new is defined
here, so a state that is orange in the weekly newsletter is orange in a
``/check`` and carries the same sentence underneath it.

Why the card is usually blue
----------------------------
The overall colour is chosen from what *dominates*, not from a tally of
favourable characteristics. A company with strong margins, net cash and a good
balance sheet would otherwise produce a solid green card, and a green card is
read as approval — which is exactly the recommendation this system does not
make and cannot support.

So the rule is deliberately narrow:

* **yellow** when data limitations dominate — a partial answer should look
  partial;
* **red** when a severe present factual risk dominates;
* **orange** when unusual market activity dominates;
* **blue** otherwise, because a stock report is descriptive information.

Individual fields still carry their own positive and negative wording. The green
is on the line that says "net cash", where it means something specific, and not
on the card, where it would mean something unsupported.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from app.core.events import EventCategory, EventType, Severity
from app.discord_bot.analysis import StockCheck
from app.discord_bot.resolve import Availability, Resolution
from app.notifications.models import NotificationMessage
from app.publishing import presentation

FOOTER: Final = "Descriptive analysis only · No forecast or investment recommendation."

_INTERNAL_LABELS: Final[frozenset[str]] = frozenset(
    {"TRUE_TTM", "FY_FALLBACK", "UNAVAILABLE", "PERIOD_END_SHARES", "COVER_PAGE_SHARES"}
)
"""Provenance codes the Advisor carries for auditing. Correct to record, wrong
to show: a reader does not need to know which share family a figure came from."""

_MAX_BULLETS: Final = 4

_DISPLAY_TOLERANCE: Final = 0.0005
"""Half of the smallest visible step at one-decimal percentage precision.

A value below this renders as ``0.0%``, so describing it as ahead or behind
would contradict the figure printed directly above the sentence. This is a
*display* tolerance derived from the rendered precision, not a financial
threshold about what counts as a meaningful move."""

_ABSENT_STATES: Final[frozenset[str]] = frozenset(
    {"INSUFFICIENT_DATA", "INSUFFICIENT_HISTORY", "SPLIT_ADJUSTMENT_REQUIRED",
     "SECTOR_SPECIFIC_MODEL_REQUIRED", "UNAVAILABLE"}
)
"""States that mean "we do not know". They belong in Data quality, where the
limitation is stated once, not in a summary of what was found."""

_CONFIDENCE_RANK: Final[dict[str, int]] = {
    "INSUFFICIENT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3,
}

_QUALITY_SECTIONS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("GROWTH", "Growth", ("revenue_ttm", "eps_ttm", "operating_income_ttm")),
    ("PROFITABILITY", "Profitability", ("operating_margin", "gross_margin")),
    ("CASH GENERATION", "Cash generation", ("free_cash_flow", "fcf_margin")),
    ("BALANCE SHEET", "Balance sheet", ("cash", "total_debt")),
    ("CAPITAL STRUCTURE", "Capital structure", ("shares_outstanding",)),
)

_LABELS: Final[dict[str, str]] = {
    "revenue_ttm": "Revenue TTM",
    "eps_ttm": "EPS TTM",
    "operating_income_ttm": "Operating income",
    "operating_margin": "Operating margin",
    "gross_margin": "Gross margin",
    "free_cash_flow": "Free cash flow",
    "fcf_margin": "FCF margin",
    "cash": "Cash",
    "total_debt": "Total debt",
    "net_cash_or_debt": "Net cash / debt",
    "shares_outstanding": "Shares",
    "pe_ttm": "P/E",
    "ps_ttm": "P/S",
    "p_fcf": "P/FCF",
}

_MONEY: Final[frozenset[str]] = frozenset(
    {"revenue_ttm", "operating_income_ttm", "free_cash_flow", "cash", "total_debt",
     "net_cash_or_debt"}
)
_MULTIPLE: Final[frozenset[str]] = frozenset({"pe_ttm", "ps_ttm", "p_fcf"})
_PER_SHARE: Final[frozenset[str]] = frozenset({"eps_ttm"})
"""Per-share currency amounts. Without this they fall through to the percentage
default and earnings of $8.71 render as 871.0%."""
_COUNT: Final[frozenset[str]] = frozenset({"shares_outstanding"})

_LABEL_WIDTH: Final = 18
"""Columns reserved for the metric name inside a fenced block. Discord renders
those blocks monospaced, which is the only way to get real alignment; 18 keeps
the longest label and its value on one line on a phone."""


def _money(value: float) -> str:
    # Sign outside the currency symbol: "-$42.76B" reads as a negative amount,
    # "$-42.76B" reads as a typo.
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if magnitude >= scale:
            return f"{sign}${magnitude / scale:,.2f}{suffix}"
    return f"{sign}${magnitude:,.0f}"


def _count(value: float) -> str:
    for scale, suffix in ((1e9, "B"), (1e6, "M")):
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.0f}"


def _value(name: str, raw: float) -> str:
    if name in _MONEY:
        return _money(raw)
    if name in _MULTIPLE:
        return f"{raw:,.2f}\u00d7"
    if name in _PER_SHARE:
        return f"${raw:,.2f}"
    if name in _COUNT:
        return _count(raw)
    return f"{raw * 100:.1f}%"


def _metric(section: Any, name: str) -> str | None:
    metric = (getattr(section, "metrics", {}) or {}).get(name)
    if metric is None or metric.value is None:
        return None
    return _value(name, float(metric.value))


def _aligned(rows: Sequence[tuple[str, str]]) -> str:
    """Metric lines in a fenced block, so labels and values line up.

    Discord renders fenced blocks monospaced. Without one, proportional text
    turns a column of figures into ragged prose that is genuinely harder to
    scan than a sentence would have been.
    """
    if not rows:
        return ""
    body = "\n".join(f"{label:<{_LABEL_WIDTH}}{value}" for label, value in rows)
    return f"```\n{body}\n```"


def _state_lines(section: Any, *, overall: str) -> list[str]:
    """Named states for a section, plus its confidence only when it is worse.

    Repeating "high confidence" under every block said the same thing five
    times and pushed the figures off a phone screen. The exception is what
    matters: a section that is materially less trustworthy than the report as a
    whole has to say so where it is read.
    """
    lines: list[str] = []
    for value in (getattr(section, "labels", {}) or {}).values():
        raw = str(value)
        if raw.isdigit() or raw in _INTERNAL_LABELS:
            continue
        state = presentation.state(raw)
        lines.append(state.label)
    own = str(getattr(section, "confidence", "") or "")
    if own and own != overall and _CONFIDENCE_RANK.get(own, 9) < _CONFIDENCE_RANK.get(
        overall, 9
    ):
        lines.append(f"_Data here: {presentation.label(own).lower()}_")
    return lines


def _net_position(section: Any) -> tuple[str, str] | None:
    """Net cash or net debt, named by which it is and shown as a positive figure.

    ``net_cash_or_debt`` is one signed number, so the card previously read
    "Net cash / debt  -$42.76B" and asked the reader to decode both the label
    and the sign. The value here is the same canonical metric; only its
    presentation splits into a name and a magnitude.
    """
    metric = (getattr(section, "metrics", {}) or {}).get("net_cash_or_debt")
    if metric is None or metric.value is None:
        return None
    value = float(metric.value)
    label = "Net cash" if value >= 0 else "Net debt"
    return label, _money(abs(value))


def _quality_fields(report: Any, overall: str) -> dict[str, str]:
    """One field per company-quality section, figures first, state after."""
    fields: dict[str, str] = {}
    for name, label, metrics in _QUALITY_SECTIONS:
        section = next((s for s in report.company_quality if s.name == name), None)
        if section is None:
            continue
        rows = [
            (_LABELS[key], rendered)
            for key in metrics
            if (rendered := _metric(section, key))
        ]
        if name == "BALANCE SHEET" and (net := _net_position(section)) is not None:
            rows.append(net)
        states = _state_lines(section, overall=overall)
        parts = [part for part in (_aligned(rows), *states) if part]
        if parts:
            fields[label] = "\n".join(parts)
    return fields


def _valuation_field(report: Any) -> str:
    valuation = report.valuation
    rows = [
        (_LABELS[key], rendered)
        for key in ("pe_ttm", "ps_ttm", "p_fcf")
        if (rendered := _metric(valuation, key))
    ]
    raw = str((valuation.labels or {}).get("ps_context", ""))
    if raw == "INSUFFICIENT_HISTORY":
        # `INSUFFICIENT_HISTORY` is also a market-regime state, whose explanation
        # is about price history and would be wrong here. Valuation has its own
        # reason for having none.
        parts = [_aligned(rows)] if rows else []
        parts.append(
            "Unavailable — not enough valuation history for a comparison."
        )
        return "\n".join(parts)
    context = presentation.state(raw)
    if context.label:
        rows.append(("vs own history", context.short.upper()))
    parts = [_aligned(rows)] if rows else ["Unavailable."]
    if context.explanation:
        parts.append(f"_{context.explanation}_")
    return "\n".join(parts)


def _market_field(report: Any) -> str:
    metrics = report.market_position.metrics or {}
    rows: list[tuple[str, str]] = []
    for key, label in (
        ("relative_strength_252d", "1Y vs benchmark"),
        ("distance_from_ma200", "vs 200-day avg"),
    ):
        metric = metrics.get(key)
        if metric is not None and metric.value is not None:
            rows.append((label, f"{metric.value * 100:+.1f}%"))

    high = metrics.get("drawdown_from_252d_high")
    if high is not None and high.value is not None:
        # A drawdown is never positive, so a signed figure invites the reader to
        # wonder what "+0.0% from the high" could mean. Name the position
        # instead and give the distance only when there is one.
        rows.append(
            ("52-week high", "at the high")
            if abs(high.value) < _DISPLAY_TOLERANCE
            else ("Below 52w high", f"{abs(high.value) * 100:.1f}%")
        )
    return _aligned(rows) if rows else "Unavailable."


def _dominant_colour(check: StockCheck) -> int:
    """The card's colour, from what dominates rather than from a tally."""
    if not check.analysable or check.resolution in (
        Resolution.UNKNOWN_SYMBOL,
        Resolution.MALFORMED_SYMBOL,
        Resolution.ANALYSIS_FAILED,
    ):
        return presentation.COLOURS[presentation.Semantic.UNAVAILABLE]
    if (
        check.fundamentals is Availability.UNAVAILABLE
        or check.resolution is Resolution.DATA_NOT_SYNCED
    ):
        return presentation.COLOURS[presentation.Semantic.UNCERTAIN]

    report = check.report
    states = [
        str(value)
        for section in (getattr(report, "company_quality", ()) or ())
        for value in (getattr(section, "labels", {}) or {}).values()
    ]
    if {"MATERIAL_DILUTION", "LEVERAGED"} & set(states):
        return presentation.COLOURS[presentation.Semantic.BAD]

    confidence = str((getattr(report, "confidence", {}) or {}).get("company_analysis", ""))
    if confidence in ("LOW", "INSUFFICIENT"):
        return presentation.COLOURS[presentation.Semantic.UNCERTAIN]

    valuation = getattr(report, "valuation", None)
    context = (getattr(valuation, "labels", {}) or {}).get("ps_context", "")
    if context in ("VERY_HIGH_VS_HISTORY", "VERY_LOW_VS_HISTORY"):
        return presentation.COLOURS[presentation.Semantic.UNUSUAL]

    # A descriptive report about a sound company is still a descriptive report.
    return presentation.COLOURS[presentation.Semantic.NEUTRAL]


def _summary_bullets(check: StockCheck) -> list[str]:
    """Up to four sentences, each restating a state the Advisor already reached.

    Deliberately **states, not numbers**. The figures are two lines above; a
    summary that repeats them is a second copy of the card rather than a
    reading of it.

    Deliberately also **no grades**. There is no "strong profitability" state in
    the Advisor -- calling a margin strong would need a threshold, and Phase
    12.25 established that no such threshold in this data survives validation.
    So the summary says what is known and stops.
    """
    report = check.report
    if report is None:
        return []
    bullets: list[str] = []
    by_name = {s.name: s for s in report.company_quality}

    if check.fundamentals is Availability.UNAVAILABLE:
        # Absent sections are one limitation, not several findings. Listing
        # "balance sheet: insufficient data" beside "insufficient data" says
        # nothing twice and crowds out what is actually known.
        bullets.append("Company fundamentals are unavailable for this instrument.")
        if check.market_data is Availability.AVAILABLE:
            bullets.append("Market data and market-position analysis are available.")
        trend = _market_direction(report)
        if trend:
            bullets.append(trend)
        return bullets[:_MAX_BULLETS]

    balance = (getattr(by_name.get("BALANCE SHEET"), "labels", {}) or {}).get("assessment")
    if balance and balance not in _ABSENT_STATES:
        state = presentation.state(balance)
        bullets.append(
            f"Balance sheet {state.short}."
            if state.short != state.label
            else f"Balance sheet: {state.label.lower()}."
        )

    dilution = (getattr(by_name.get("CAPITAL STRUCTURE"), "labels", {}) or {}).get(
        "dilution"
    )
    if dilution and dilution not in _ABSENT_STATES:
        state = presentation.state(dilution)
        bullets.append(
            f"Share count is {state.short}."
            if state.short != state.label
            else f"{state.label}."
        )

    context = (report.valuation.labels or {}).get("ps_context")
    if context and context not in _ABSENT_STATES:
        bullets.append(
            f"Valuation is {presentation.state(context).short.lower()} relative to "
            f"its available history."
        )

    trend = _market_direction(report)
    if trend:
        bullets.append(trend)
    return bullets[:_MAX_BULLETS]


def _direction(value: float, above: str, below: str, level: str) -> str:
    """Wording for the sign of a displayed figure, including "neither".

    A figure that prints as ``+0.0%`` must not be narrated as ahead or behind:
    the sentence would contradict the number three lines above it.
    """
    if abs(value) < _DISPLAY_TOLERANCE:
        return level
    return above if value > 0 else below


def _market_direction(report: Any) -> str | None:
    """Which side of its own trend the stock currently sits on.

    Reads the sign of two figures the Advisor already computed. No threshold is
    applied and no direction is projected -- "above its 200-day average" is a
    statement about today.
    """
    metrics = report.market_position.metrics or {}
    relative = metrics.get("relative_strength_252d")
    average = metrics.get("distance_from_ma200")
    parts: list[str] = []
    if relative is not None and relative.value is not None:
        parts.append(
            _direction(
                relative.value,
                "ahead of the benchmark over a year",
                "behind the benchmark over a year",
                "in line with the benchmark over a year",
            )
        )
    if average is not None and average.value is not None:
        parts.append(
            _direction(
                average.value,
                "above its 200-day average",
                "below its 200-day average",
                "at its 200-day average",
            )
        )
    if not parts:
        return None
    return "Currently trading " + " and ".join(parts) + "."


def _data_quality_field(check: StockCheck) -> str:
    rows: list[tuple[str, str]] = []
    if check.report is not None:
        overall = str(
            (check.report.confidence or {}).get("company_analysis", "INSUFFICIENT")
        )
        rows.append(("Confidence", presentation.label(overall).replace(" confidence", "")))
    rows.append(("As of", _pretty_date(check.as_of)))
    parts = [_aligned(rows)]
    if check.fundamentals is Availability.UNAVAILABLE:
        parts.append(
            "_Fundamentals unavailable — an absence of data, not a judgement about "
            "the company._"
        )
    parts.extend(f"_{note}_" for note in check.notes)
    return "\n".join(parts)


def _pretty_date(value: str) -> str:
    """``2026-08-14`` as ``14 Aug 2026``. Dates are read, not parsed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).strftime(
            "%d %b %Y"
        )
    except ValueError:
        return value or "unknown"


def check_message(check: StockCheck) -> NotificationMessage:
    """One embed for one ``/check``.

    Company analysis only. Portfolio context is a different question with a
    different owner, and mixing the two meant a reader had to separate them by
    eye on every card.
    """
    if check.resolution in (Resolution.UNKNOWN_SYMBOL, Resolution.MALFORMED_SYMBOL):
        return _unknown_message(check)

    fields: dict[str, str] = {}
    report = check.report
    if report is not None:
        overall = str((report.confidence or {}).get("company_analysis", "INSUFFICIENT"))
        fields.update(_quality_fields(report, overall))
        fields["Valuation"] = _valuation_field(report)
        fields["Market position"] = _market_field(report)
    else:
        fields["Fundamentals"] = "Unavailable."
        fields["Valuation"] = "Unavailable."
    fields["Data quality"] = _data_quality_field(check)

    bullets = _summary_bullets(check)
    if bullets:
        fields["Summary"] = "\n".join(f"\u2022 {line}" for line in bullets)

    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        colour=_dominant_colour(check),
        title=f"\U0001f50e {check.symbol} — stock analysis",
        body="",
        event_type=EventType.MARKET_TRENDS,
        occurred_at=check.checked_at,
        key=f"check:{check.symbol}:{check.as_of}",
        footer=FOOTER,
        # No embed timestamp. Discord renders footer text and timestamp on one
        # line joined by a bullet, which ran the disclaimer straight into
        # "today at 21:40". The observation date has its own row in Data
        # quality, where it is labelled.
        show_timestamp=False,
        fields=fields,
    )


def _unknown_message(check: StockCheck) -> NotificationMessage:
    """A refusal that names what was asked for and never acts on a guess."""
    lines = [check.detail or f'No supported instrument matches "{check.symbol}".']
    fields: dict[str, str] = {}
    if check.suggestion:
        fields["Possible match"] = (
            f"`{check.suggestion}`\n"
            f"If you meant this, send `/check symbol:{check.suggestion}`. "
            "Tradabot will not substitute it for you."
        )
    fields["Why"] = (
        "Analysing a different instrument than the one asked about would produce "
        "a confident report about the wrong company."
    )
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        colour=presentation.COLOURS[presentation.Semantic.UNAVAILABLE],
        title=f"🔎 {check.symbol} — symbol not found",
        body="\n".join(lines),
        event_type=EventType.MARKET_TRENDS,
        occurred_at=check.checked_at,
        key=f"check-unknown:{check.symbol}",
        footer=FOOTER,
        show_timestamp=False,
        fields=fields,
    )
