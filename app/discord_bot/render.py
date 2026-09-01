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

_PEER_EXAMPLES: Final = 5
"""Peer symbols named on the card. Enough to show what kind of company is in
the group; the full membership always stays on the ``PeerComparison`` for any
consumer that wants to audit it."""

_DISPLAY_TOLERANCE: Final = 0.0005
"""Half of the smallest visible step at one-decimal percentage precision.

A value below this renders as ``0.0%``, so describing it as ahead or behind
would contradict the figure printed directly above the sentence. This is a
*display* tolerance derived from the rendered precision, not a financial
threshold about what counts as a meaningful move."""

_ABSENT_STATES: Final[frozenset[str]] = frozenset(
    {
        "INSUFFICIENT_DATA",
        "INSUFFICIENT_HISTORY",
        "SPLIT_ADJUSTMENT_REQUIRED",
        "SECTOR_SPECIFIC_MODEL_REQUIRED",
        "UNAVAILABLE",
    }
)
"""States that mean "we do not know". They belong in Data quality, where the
limitation is stated once, not in a summary of what was found."""

_CONFIDENCE_RANK: Final[dict[str, int]] = {
    "INSUFFICIENT": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
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
    {
        "revenue_ttm",
        "operating_income_ttm",
        "free_cash_flow",
        "cash",
        "total_debt",
        "net_cash_or_debt",
    }
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


SYMBOLS: Final[dict[str, str]] = {
    "USD": "$",
    "EUR": "€",
    "CAD": "CA$",
    "GBP": "£",
    "CHF": "CHF ",
    "JPY": "¥",
    "AUD": "A$",
}
"""Reporting-currency notation. A company that reports in euros must not have
its revenue printed with a dollar sign: the number would be right and the
sentence wrong, which is the harder error to notice. An unlisted code renders
as the code itself rather than a guessed glyph."""


def _symbol_for(currency: str | None) -> str:
    if not currency:
        return "$"
    return SYMBOLS.get(currency.upper(), f"{currency.upper()} ")


def _money(value: float, currency: str | None = None) -> str:
    # Sign outside the currency symbol: "-$42.76B" reads as a negative amount,
    # "$-42.76B" reads as a typo.
    unit = _symbol_for(currency)
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if magnitude >= scale:
            return f"{sign}{unit}{magnitude / scale:,.2f}{suffix}"
    return f"{sign}{unit}{magnitude:,.0f}"


def _count(value: float) -> str:
    for scale, suffix in ((1e9, "B"), (1e6, "M")):
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{suffix}"
    return f"{value:,.0f}"


def _value(name: str, raw: float, currency: str | None = None) -> str:
    if name in _MONEY:
        return _money(raw, currency)
    if name in _MULTIPLE:
        return f"{raw:,.2f}\u00d7"
    if name in _PER_SHARE:
        return f"{_symbol_for(currency)}{raw:,.2f}"
    if name in _COUNT:
        return _count(raw)
    return f"{raw * 100:.1f}%"


def _metric(section: Any, name: str, currency: str | None = None) -> str | None:
    metric = (getattr(section, "metrics", {}) or {}).get(name)
    if metric is None or metric.value is None:
        return None
    return _value(name, float(metric.value), currency)


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


IFRS_REFUSED_BY_SECTION: Final[dict[str, str]] = {
    "BALANCE SHEET": (
        "Leverage is not assessed for this issuer: IFRS borrowing concepts vary "
        "by filer and routinely include lease liabilities, so a total-debt "
        "figure would not be comparable."
    ),
    "CAPITAL STRUCTURE": (
        "Share-count trend is not assessed for this issuer: IFRS reports shares "
        "issued rather than shares outstanding, and the two differ by treasury "
        "holdings."
    ),
    "CASH GENERATION": (
        "Free cash flow is not assessed for this issuer: IFRS capital-expenditure "
        "lines differ in what they include, so the figure would not be "
        "comparable."
    ),
}
"""Why an IFRS filer's section is thin.

Phase 13.2 refused these metrics for stated semantic reasons, but the card said
"Insufficient data" -- which reads as *Tradabot is missing something* when the
truth is *this number would not mean what you think it means*. The distinction
is the whole point of refusing, so it belongs on the card."""


def _sector_note(section: Any) -> str | None:
    """The explanation behind a sector refusal, not just its label.

    "Not assessed for a financial company" is accurate and tells a reader
    nothing. The vocabulary already carries the reason; the card simply was not
    showing it.
    """
    labels = {str(v) for v in (getattr(section, "labels", {}) or {}).values()}
    if _SECTOR_REFUSAL not in labels:
        return None
    state = presentation.state(_SECTOR_REFUSAL)
    return f"{state.label}. {state.explanation}" if state.explanation else state.label


def _ifrs_note(section_name: str, section: Any, taxonomy: str | None) -> str | None:
    """The refusal wording for an IFRS filer, when that is the real reason."""
    if taxonomy != "ifrs-full":
        return None
    note = IFRS_REFUSED_BY_SECTION.get(section_name)
    if note is None:
        return None
    # Only when the section is actually thin. A sector refusal or a genuine data
    # gap has its own wording and must not be overwritten by this one.
    labels = {str(v) for v in (getattr(section, "labels", {}) or {}).values()}
    if _SECTOR_REFUSAL in labels:
        return None
    if not any(state in labels for state in _THIN_STATES):
        return None
    return note


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
    if own and own != overall and _CONFIDENCE_RANK.get(own, 9) < _CONFIDENCE_RANK.get(overall, 9):
        lines.append(f"_Data here: {presentation.label(own).lower()}_")
    return lines


def _net_position(section: Any, currency: str | None = None) -> tuple[str, str] | None:
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
    return label, _money(abs(value), currency)


def _quality_fields(
    report: Any,
    overall: str,
    currency: str | None = None,
    taxonomy: str | None = None,
) -> dict[str, str]:
    """One field per company-quality section, figures first, state after."""
    fields: dict[str, str] = {}
    for name, label, metrics in _QUALITY_SECTIONS:
        section = next((s for s in report.company_quality if s.name == name), None)
        if section is None:
            continue
        rows = [
            (_LABELS[key], rendered)
            for key in metrics
            if (rendered := _metric(section, key, currency))
        ]
        if name == "BALANCE SHEET" and (net := _net_position(section, currency)) is not None:
            rows.append(net)
        states = _state_lines(section, overall=overall)
        note = _sector_note(section) or _ifrs_note(name, section, taxonomy)
        if note is not None:
            # Replace the generic state, do not stack a second sentence on it.
            states = [f"_{note}_"]
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
        parts.append("Unavailable — not enough valuation history for a comparison.")
        return "\n".join(parts)
    context = presentation.state(raw)
    if context.label:
        rows.append(("vs own history", context.short.upper()))
    parts = [_aligned(rows)] if rows else ["Unavailable."]
    if context.explanation:
        parts.append(f"_{context.explanation}_")
    return "\n".join(parts)


def _peer_field(check: StockCheck) -> str | None:
    """Where this company sits among comparable companies, or nothing.

    Returns ``None`` when the peer layer is not wired in at all -- an absent
    section rather than a section announcing its own absence. A *refused*
    comparison does render, with its reason, because "no comparable peer group
    exists" is information about the company.
    """
    peers = getattr(check, "peers", None)
    if peers is None:
        return None
    if not peers.available:
        return f"Unavailable — {peers.detail or 'no comparable peer group'}."

    # `_value` already owns how a multiple and a percentage render on this card,
    # and the peer metric keys are the Advisor's own, so the peer row and the
    # row above it cannot disagree about what 33.82x looks like.
    rows = [
        (
            c.label,
            f"{_ordinal(c.percentile)} pct",
            f"peer median {_value(c.metric, c.median)}",
            _value(c.metric, c.value),
        )
        for c in peers.comparisons
    ]
    width = max(len(r[0]) for r in rows)
    lines = [
        f"{label.ljust(width)}  {value:>8}  {pct:>9}  {median}"
        for label, pct, median, value in rows
    ]
    group = peers.group
    block = "```\n" + "\n".join(lines) + "\n```"
    # Named peers, alphabetically. Deterministic and, more to the point,
    # unrelated to how the comparison came out -- an ordering that surfaced the
    # closest or most flattering members would make the sample look chosen.
    shown = sorted(m.symbol for m in group.included)[:_PEER_EXAMPLES]
    examples = ", ".join(shown)
    if group.size > _PEER_EXAMPLES:
        examples += f" and {group.size - _PEER_EXAMPLES} more"
    parts = [block, f"_Peers: {group.size} · {group.label} — {examples}_"]
    if group.mixed_taxonomy:
        # An industry group is assembled from what a company does, not from how
        # it reports. Saying so is the honest middle ground between refusing
        # international comparison and pretending the difference is not there.
        parts.append(
            f"_This company reports under {group.subject_taxonomy} while most of "
            f"the group reports under {group.peer_taxonomy}; the percentages are "
            f"comparable but not drawn to identical definitions._"
        )
    sentence = presentation_describe(peers)
    if sentence:
        parts.append(sentence)
    return "\n".join(parts)


_MAX_FIELD: Final = 1024
"""Discord's hard limit on one embed field value."""

_MAX_FIGURES: Final = 2
"""Figures printed per filing. An earnings release that yielded revenue and a
margin says enough in two lines; a third turns a development into a table the
card already has above it."""

_DEVELOPMENT_COVERAGE: Final[dict[str, str]] = {
    "NO_CURRENT_EVENTS": (
        "No SEC filing within its currency window. Tradabot covers this "
        "company's filings; none recent enough qualifies."
    ),
    "NO_COVERAGE": (
        "Unavailable — Tradabot has not ingested SEC filings for this company yet. "
        "This is a gap in coverage, not a statement about the company."
    ),
    "UNAVAILABLE": "Unavailable — the research store has not been built.",
    "NOT_APPLICABLE": ("Not applicable — this is a fund, which files no company reports."),
}
"""One sentence per coverage state, and they are deliberately not
interchangeable. "No qualifying filing", "we never ingested this company" and
"there is no research store" look identical on a card that says only
*unavailable*, and a reader who cannot tell them apart cannot tell whether the
silence is about the company or about Tradabot."""


def _developments_field(check: StockCheck) -> str | None:
    """What this company is currently known to have filed, or why nothing shows.

    Returns ``None`` only when the research layer was never wired in -- an
    absent section rather than a section announcing its own absence, the same
    convention peer context uses.

    Nothing here is directional. The filing kind, the SEC item and the
    materiality band are all statements about *what was disclosed and how much
    attention it warrants*, never about whether it is good news, and the section
    closes by saying outright that no historical price evidence backs any of it.
    """
    report = getattr(check, "developments", None)
    if report is None:
        return None
    status = str(report.status)
    if not report.has_developments:
        if status == "SOURCE_LIMITATION":
            return (
                f"Partial coverage — {report.detail}. "
                f"This does not mean nothing happened at this company."
            )
        if status == "NO_CURRENT_EVENTS" and report.periodic_current:
            return f"No recent event filing — {report.detail}."
        base = _DEVELOPMENT_COVERAGE.get(status, "Unavailable.")
        note = _stale_note(report)
        return f"{base}\n{note}" if note else base

    blocks = [_stale_note(report), *(_development(d) for d in report.developments)]
    blocks = [b for b in blocks if b]
    # Stated once for the section rather than once per filing: it is true of
    # every event kind here, and repeating it three times would make the one
    # thing the section refuses to claim its most prominent feature.
    footers = [_more(report), "_Historical price evidence: not established for these event types._"]
    return _within_limit(blocks, [f for f in footers if f])


def _stale_note(report: Any) -> str:
    """One line when ingestion itself has fallen behind, and nothing otherwise.

    A quiet company and a scheduler that stopped running produce the same empty
    section, and only one of them is a fact about the company. The events
    already stored stay exactly as they are -- this adds a caveat, it does not
    withdraw anything.
    """
    state = getattr(report, "ingestion", None)
    if state is None or state.current:
        return ""
    if str(state.status) == "NEVER_RUN":
        return "_Automatic SEC ingestion has not run; coverage may be incomplete._"
    days = (state.age_hours or 0) / 24
    return (
        f"_SEC ingestion last succeeded {days:.0f} day(s) ago; newer filings may not be included._"
    )


def _more(report: Any) -> str:
    """One line for everything not shown, rather than two."""
    parts = []
    if report.suppressed:
        parts.append(f"{report.suppressed} further qualifying filing(s)")
    if str(report.status) == "PARTIAL" and report.unclassified_current:
        parts.append(f"{report.unclassified_current} recent filing(s) carrying no item codes")
    return f"_Not shown: {' · '.join(parts)}._" if parts else ""


def _within_limit(blocks: list[str], footers: list[str]) -> str:
    """Whole filings, dropped from the end until the field fits.

    Discord truncates a field over 1,024 characters mid-word, which on this
    section would cut a figure away from the period it belongs to or a source
    link away from the claim it supports. Dropping the least recent filing
    whole, and saying so, keeps every line that survives complete.
    """
    dropped = 0
    while blocks:
        tail = list(footers)
        if dropped:
            tail.insert(0, f"_{dropped} further filing(s) omitted for length._")
        candidate = "\n".join([*blocks, *tail])
        if len(candidate) <= _MAX_FIELD:
            return candidate
        blocks.pop()
        dropped += 1
    return "\n".join(footers)


def _development(development: Any) -> str:
    """One filing: what it reported, when, and what backs it."""
    labels = " · ".join(dict.fromkeys(i.label for i in development.items))
    when = _pretty_date(development.published_at[:10])
    items = ", ".join(f"Item {i.item}" for i in development.items if i.item)
    source = f"{development.form}{f' · {items}' if items else ''}"
    lines = [
        f"**{labels}** · {when}",
        f"{source} · Materiality: {str(development.materiality).capitalize()}",
    ]
    for figure in development.figures[:_MAX_FIGURES]:
        lines.append(f"{figure.label}: {_figure_value(figure)} — {figure.period}")
    if not development.figures:
        lines.append(
            "Primary-source evidence available; structured figures were not extracted safely."
            if development.evidence_available
            else "No primary-source document was retrieved for this filing."
        )
    if development.source_url:
        lines.append(f"[SEC filing]({development.source_url})")
    if development.amends_accession:
        lines.append("_This filing amends an earlier one._")
    return "\n".join(lines)


def _figure_value(figure: Any) -> str:
    """A reported figure, formatted the way the rest of the card formats money."""
    if figure.unit == "PERCENT":
        return f"{figure.value:.1f}%"
    if figure.unit == "CURRENCY_PER_SHARE":
        return f"{_symbol_for(figure.currency)}{figure.value:,.2f}"
    return _money(figure.value, figure.currency)


def _ordinal(percentile: float) -> str:
    """``90.0`` -> ``90th``. Rounded to whole points: the sample supporting a
    percentile is never fine enough to justify a decimal place."""
    value = round(percentile)
    teens = 10 <= value % 100 <= 20  # noqa: PLR2004 - 11th, 12th, 13th
    suffix = "th" if teens else {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def presentation_describe(peers: Any) -> str:
    """The deterministic sentence, imported lazily to keep the boundary clean."""
    from app.peers.service import describe  # noqa: PLC0415

    return describe(peers)


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
    return presentation.COLOURS[_attention(check, report)]


def _attention(check: StockCheck, report: Any) -> presentation.Semantic:
    """How much attention the card asks for, once nothing is presently wrong.

    ORANGE means "unusual and worth inspecting; no direction implied" -- the
    existing vocabulary's only non-directional attention state, and exactly what
    a restatement or a change of control is. It is deliberately limited to
    CRITICAL developments: SIGNIFICANT covers every earnings release, so letting
    it colour the card would turn it orange four times a year and mean nothing.

    A development can never produce GREEN or RED. An SEC item establishes what
    was disclosed, never whether it was welcome, and colouring a filing by its
    materiality would turn an attention band into a verdict.
    """
    if _has_critical_development(check):
        return presentation.Semantic.UNUSUAL

    confidence = str((getattr(report, "confidence", {}) or {}).get("company_analysis", ""))
    if confidence in ("LOW", "INSUFFICIENT"):
        return presentation.Semantic.UNCERTAIN

    valuation = getattr(report, "valuation", None)
    context = (getattr(valuation, "labels", {}) or {}).get("ps_context", "")
    if context in ("VERY_HIGH_VS_HISTORY", "VERY_LOW_VS_HISTORY"):
        return presentation.Semantic.UNUSUAL

    # A descriptive report about a sound company is still a descriptive report.
    return presentation.Semantic.NEUTRAL


def _has_critical_development(check: StockCheck) -> bool:
    """Whether a current filing sits in the highest attention band."""
    report = getattr(check, "developments", None)
    if report is None:
        return False
    return any(str(d.materiality) == "CRITICAL" for d in report.developments)


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

    dilution = (getattr(by_name.get("CAPITAL STRUCTURE"), "labels", {}) or {}).get("dilution")
    if dilution and dilution not in _ABSENT_STATES:
        state = presentation.state(dilution)
        bullets.append(
            f"Share count is {state.short}." if state.short != state.label else f"{state.label}."
        )

    context = (report.valuation.labels or {}).get("ps_context")
    # Only when this listing has its own prices. The Advisor computes the
    # valuation band from whatever price series the ticker names, so for an
    # unpriced foreign listing that band came from a different security -- the
    # same leak the Valuation field already refuses.
    if check.market_data is not Availability.AVAILABLE or getattr(check, "valuation_refusal", None):
        context = None
    if context and context not in _ABSENT_STATES:
        bullets.append(
            f"Valuation is {presentation.state(context).short.lower()} relative to "
            f"its available history."
        )

    if check.market_data is Availability.AVAILABLE:
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


_SECTOR_REFUSAL: Final = "SECTOR_SPECIFIC_MODEL_REQUIRED"
_THIN_STATES: Final[frozenset[str]] = frozenset({"INSUFFICIENT_DATA", "INSUFFICIENT"})


def _taxonomy(check: StockCheck) -> str | None:
    """The accounting taxonomy of the company whose figures are shown."""
    listing = getattr(check, "listing", None)
    return getattr(listing, "taxonomy", None) if listing else None


def _currency(check: StockCheck) -> str | None:
    """The reporting currency of the company whose figures are shown."""
    listing = getattr(check, "listing", None)
    return getattr(listing, "reporting_currency", None) if listing else None


def _by_design(check: StockCheck) -> bool:
    """Whether thin coverage is a deliberate refusal rather than missing data.

    True when every section Tradabot declined to assess carries a stated reason
    -- a sector model it does not have, or an IFRS concept that is not
    comparable -- and at least one section did report figures.

    The distinction matters because "Insufficient data" under a card showing
    reconciled euro revenue reads as *these numbers are not trustworthy*, which
    is the opposite of what the state means.
    """
    report = getattr(check, "report", None)
    if report is None:
        return False
    # A refused valuation is a stated refusal like any other, and it alone can
    # drag the overall reading down: Canadian National files complete us-gaap
    # figures in CAD and trades in USD, so every quality section is solid and
    # the card still said "Insufficient data" because the one ratio Tradabot
    # declines to mix currencies for is missing.
    refused_valuation = bool(getattr(check, "valuation_refusal", None))
    taxonomy = _taxonomy(check)
    # The Advisor states a financial-sector refusal once, for the whole report;
    # it explains every section it silences, not only the one it is attached to.
    sector = any(
        "financial-sector" in str(r) for r in (getattr(report, "risks", {}) or {}).get("data", ())
    )
    thin = solid = 0
    for section in report.company_quality:
        labels = {str(v) for v in (section.labels or {}).values()}
        values = [m for m in (section.metrics or {}).values() if m.value is not None]
        if str(section.confidence) not in _THIN_STATES:
            solid += 1
            continue
        thin += 1
        explained = (
            sector
            or _SECTOR_REFUSAL in labels
            or (taxonomy == "ifrs-full" and section.name in IFRS_REFUSED_BY_SECTION)
        )
        if not explained and not values:
            return False
    if refused_valuation:
        thin += 1
    return thin > 0 and solid > 0


def _data_quality_field(check: StockCheck) -> str:
    rows: list[tuple[str, str]] = []
    explain: str | None = None
    if check.report is not None:
        overall = str((check.report.confidence or {}).get("company_analysis", "INSUFFICIENT"))
        if overall in _THIN_STATES and _by_design(check):
            # Same analytical state, honest label. The figures on this card were
            # reconciled to their filings; what is partial is the coverage.
            rows.append(("Coverage", "Partial"))
            explain = (
                "Partial coverage, not unreliable figures: what is shown is "
                "taken from the company's own filings. The sections above say "
                "why they were not assessed."
            )
        else:
            rows.append(("Confidence", presentation.label(overall).replace(" confidence", "")))
    rows.append(("As of", _pretty_date(check.as_of)))
    parts = [_aligned(rows)]
    if explain is not None:
        parts.append(f"_{explain}_")
    if check.fundamentals is Availability.UNAVAILABLE:
        # A fund has no fundamentals to be missing. Saying "an absence of data"
        # about an index tracker invites the reader to wait for a sync that
        # will never produce revenue for it.
        parts.append(
            "_This is a fund, not an operating company: it has holdings and a "
            "net asset value rather than revenue, margins and a balance sheet._"
            if _is_fund(check)
            else "_Fundamentals unavailable — an absence of data, not a judgement "
            "about the company._"
        )
    parts.extend(f"_{note}_" for note in check.notes)
    return "\n".join(parts)


def _is_fund(check: StockCheck) -> bool:
    """Whether the resolved listing is a pooled vehicle rather than a company."""
    listing = getattr(check, "listing", None)
    return str(getattr(listing, "asset_type", "STOCK")) in {"ETF", "FUND", "ETN"}


def _pretty_date(value: str) -> str:
    """``2026-08-14`` as ``14 Aug 2026``. Dates are read, not parsed."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).strftime("%d %b %Y")
    except ValueError:
        return value or "unknown"


def check_message(check: StockCheck) -> NotificationMessage:
    """One embed for one ``/check``.

    Company analysis only. Portfolio context is a different question with a
    different owner, and mixing the two meant a reader had to separate them by
    eye on every card.
    """
    if check.resolution is Resolution.AMBIGUOUS_SYMBOL:
        return _ambiguous_message(check)
    if check.resolution in (
        Resolution.UNKNOWN_SYMBOL,
        Resolution.MALFORMED_SYMBOL,
        Resolution.UNSUPPORTED_LISTING,
    ):
        return _unknown_message(check)

    fields: dict[str, str] = {}
    report = check.report
    if report is not None:
        overall = str((report.confidence or {}).get("company_analysis", "INSUFFICIENT"))
        fields.update(_quality_fields(report, overall, _currency(check), _taxonomy(check)))
        refusal = getattr(check, "valuation_refusal", None)
        if check.market_data is Availability.AVAILABLE and not refusal:
            fields["Valuation"] = _valuation_field(report)
            fields["Market position"] = _market_field(report)
        elif check.market_data is Availability.AVAILABLE:
            # Priced, but not in the currency the company reports in. The ratio
            # is arithmetically computable and semantically meaningless.
            fields["Valuation"] = f"Unavailable — {refusal}."
            fields["Market position"] = _market_field(report)
        else:
            # Withheld, not borrowed. The Advisor is fed by ticker, so a Xetra
            # listing would otherwise show its US ADR's price history,
            # benchmarked against SPY -- a plausible-looking number measuring a
            # different security in a different currency against a benchmark
            # validated only for US listings.
            fields["Valuation"] = (
                "Unavailable — Tradabot holds no market data for this listing, "
                "and will not price it from another venue."
            )
            fields["Market position"] = (
                "Unavailable — no market data for this listing. Relative "
                "strength is unavailable outside the US in any case: SPY was "
                "validated against US listings and means nothing here."
            )
    else:
        fields["Fundamentals"] = "Unavailable."
        fields["Valuation"] = "Unavailable."
    peer_field = _peer_field(check)
    if peer_field is not None:
        fields["Peer context"] = peer_field
    developments = _developments_field(check)
    if developments is not None:
        fields["Current developments"] = developments
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


def _ambiguous_message(check: StockCheck) -> NotificationMessage:
    """Several companies answer to this ticker, so name them and stop.

    No selection, no ranking, no "did you mean". Picking would be exactly the
    behaviour that produced a confident report about DTE Energy when Deutsche
    Telekom was meant.
    """
    lines = [check.detail or f'"{check.symbol}" names more than one listing.']
    fields: dict[str, str] = {}
    for candidate in check.candidates:
        row = candidate if isinstance(candidate, dict) else candidate.as_dict()
        data = []
        if row.get("has_prices"):
            data.append("market data")
        if row.get("has_fundamentals"):
            data.append("fundamentals")
        # Keyed by the qualified listing, never by the company name. Embed
        # fields are a mapping, so two listings of one issuer -- which carry
        # the same name by definition -- collapsed into one entry: `/check SAP`
        # said "2 listings, name the venue" and then offered only SAP.US, with
        # SAP.DE nowhere on the card that existed to point at it. `(mic,
        # symbol)` is unique by database constraint, so this cannot recur.
        fields[str(row["qualified"])[:250]] = (
            f"{row['company']}\n"
            f"`/check symbol:{row['qualified']}`\n"
            f"{row['mic']} · {row['country']} · {row['quote_currency']}\n"
            f"{'Available: ' + ', '.join(data) if data else 'No data available yet'}"
        )
    fields["Why"] = (
        "Tradabot will not choose between companies. Analysing the wrong one "
        "would look exactly like analysing the right one."
    )
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        colour=presentation.COLOURS[presentation.Semantic.UNCERTAIN],
        title=f"\U0001f50e {check.symbol} — which listing?",
        body="\n".join(lines),
        event_type=EventType.MARKET_TRENDS,
        occurred_at=check.checked_at,
        key=f"check-ambiguous:{check.symbol}",
        footer=FOOTER,
        show_timestamp=False,
        fields=fields,
    )


def _unknown_message(check: StockCheck) -> NotificationMessage:
    """A refusal that names what was asked for and never acts on a guess.

    Two different refusals share this shape and must not share its wording. A
    symbol that matches nothing is unknown; a symbol that matches a listing
    Tradabot holds no data for is *known and unsupported*. Saying "no supported
    instrument matches MBG" directly above a field naming Mercedes-Benz Group
    on Xetra contradicted itself, and left the reader unsure whether the symbol
    or the data was the problem.
    """
    known = check.listing is not None
    lines = [
        check.detail
        or (
            f'"{check.symbol}" is a listing Tradabot knows and holds no data for yet.'
            if known
            else f'No supported instrument matches "{check.symbol}".'
        )
    ]
    fields: dict[str, str] = {}
    if check.listing is not None:
        fields["Listing"] = (
            f"{check.listing.company_name}\n{check.listing.mic} · "
            f"{check.listing.country} · {check.listing.quote_currency}\n"
            "Tradabot holds neither prices nor filings for this listing yet."
        )
    if check.suggestion:
        fields["Possible match"] = (
            f"`{check.suggestion}`\n"
            f"If you meant this, send `/check symbol:{check.suggestion}`. "
            "Tradabot will not substitute it for you."
        )
    fields["Why"] = (
        "The listing is recognised; Tradabot simply holds no prices or filings "
        "for it. Reporting another listing's figures under this one would look "
        "exactly like reporting this one."
        if known
        else "Analysing a different instrument than the one asked about would "
        "produce a confident report about the wrong company."
    )
    return NotificationMessage(
        category=EventCategory.MARKET,
        severity=Severity.INFO,
        colour=presentation.COLOURS[presentation.Semantic.UNAVAILABLE],
        title=(
            f"🔎 {check.symbol} — not available"
            if known
            else f"🔎 {check.symbol} — symbol not found"
        ),
        body="\n".join(lines),
        event_type=EventType.MARKET_TRENDS,
        occurred_at=check.checked_at,
        key=f"check-unknown:{check.symbol}",
        footer=FOOTER,
        show_timestamp=False,
        fields=fields,
    )
