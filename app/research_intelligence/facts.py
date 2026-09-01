"""Reading figures out of a disclosure document, and mostly declining to.

Why this is not a table parser
------------------------------
The obvious way to read an earnings release is to parse its summary table. It
does not survive contact with a real one. NVIDIA's EX-99.1 was measured, and
three properties of it defeat table parsing after HTML flattening:

1. The GAAP and non-GAAP summaries both label a row ``Net income``, with
   different values ($59,688 M and $53,954 M). The label alone does not say
   which basis a number is on.
2. The income statement carries ``Three Months Ended`` beside ``Six Months
   Ended``. Which column a cell belongs to is spatial information, and
   flattening destroys it.
3. The scale lives in a caption -- ``($ in millions, except earnings per
   share)`` -- arbitrarily far from any individual figure.

Every one of those is a *column* fact, and a flattened document has no columns.
A parser that guessed would be right often enough to look correct and wrong
often enough to matter, in the one direction that matters: a confident,
precise, unsourceable number.

So: sentence-scoped extraction
------------------------------
A fact is emitted only when a **single sentence** establishes metric, value,
unit, currency and basis together. Prose does this routinely -- *"NVIDIA today
reported revenue for the second quarter ended July 27, 2025, of $46.7
billion"* names all of them in one breath -- and a table row does not. Anything
less is a refusal with a reason, not a lower-confidence guess.

The period is scoped to the paragraph, and why
----------------------------------------------
Period is the one exception, and it was measured into existence rather than
assumed. Apple's release opens: *"Apple today announced financial results for
its fiscal 2026 third quarter ended June 27, 2026. The Company posted quarterly
revenue of $109.4 billion."* The period is in the first sentence and the figure
is in the second. Requiring both in one sentence extracted **nothing** from
Apple, Microsoft or AMD -- not because the documents were unclear, but because
publishers state a period once and then write about it.

So a sentence may take its period from the paragraph it sits in -- one
flattened block, a unit the publisher chose -- under three conditions that keep
the original guarantee intact:

* the paragraph names **exactly one** dated period. Two, and everything in it
  is refused, which is what happens to an income statement showing three months
  beside six;
* the sentence names no period of its own that conflicts with it. A sentence
  saying *"fourth-quarter revenue"* inside a paragraph dated to a full year
  does not inherit the year -- it is refused;
* nothing is inherited across a block boundary. NVIDIA's outlook bullet
  *"Revenue is expected to be $108.0 billion"* is its own line carrying no
  date, so it stays refused, as it must -- the paragraph above it is about a
  quarter that has already happened.

Consequences, accepted deliberately:

* Any sentence carrying a tab is discarded unread. Tabs come from table cells,
  and a table cell is exactly the context this module cannot interpret.
* Any sentence mentioning a non-GAAP measure is refused whole. Tradabot's
  canonical history is as-reported GAAP, and a non-GAAP figure filed next to it
  would be silently incomparable.
* A period must be pinned to a calendar date. *"Second quarter revenue was up
  6%"* establishes no period this store can align to anything.
* Nothing is derived. Free cash flow is not operating cash flow minus capital
  expenditure here, even though it is by definition, because a derived figure
  has no sentence to cite.

The refusal rate is high, and it is the point. A layer whose output is a
citation is worth only as much as the citation.

Untrusted input
---------------
Document text is data. It is matched against fixed patterns, never evaluated,
and nothing it contains selects a code path, a URL, a file path or a query.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

from app.research_intelligence.schemas import (
    UNKNOWN_CURRENCY,
    Confidence,
    EvidenceReference,
    FactStatus,
    FiscalPeriod,
    ResearchDocument,
    ResearchFact,
)

EXTRACTION_METHOD: Final = "sentence-scoped-deterministic"

MAX_SENTENCE = 400
"""Longer than this and it is not a sentence -- it is a flattened block whose
internal structure was lost. Refused unread, like anything containing a tab."""


# --------------------------------------------------------------- metrics
class Unit:
    CURRENCY = "CURRENCY"
    CURRENCY_PER_SHARE = "CURRENCY_PER_SHARE"
    PERCENT = "PERCENT"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One thing worth recognising, and the exact words that establish it."""

    metric: str
    unit: str
    labels: tuple[str, ...]
    requires_scale: bool
    """Whether a bare amount is ambiguous. ``$46.7 billion`` states its own
    scale; ``$46.7`` for revenue does not, and the caption that would have
    resolved it is not in the sentence. Per-share figures are exempt because
    they are quoted unscaled by universal convention."""


V1_METRICS: Final[tuple[MetricSpec, ...]] = (
    MetricSpec(
        "revenue",
        Unit.CURRENCY,
        ("total revenue", "net revenue", "total net sales", "net sales", "revenue"),
        requires_scale=True,
    ),
    MetricSpec(
        "net_income",
        Unit.CURRENCY,
        ("net income", "net earnings", "net loss"),
        requires_scale=True,
    ),
    MetricSpec(
        "operating_income",
        Unit.CURRENCY,
        ("operating income", "income from operations", "operating loss"),
        requires_scale=True,
    ),
    MetricSpec(
        "eps_diluted",
        Unit.CURRENCY_PER_SHARE,
        (
            "diluted earnings per share",
            "earnings per diluted share",
            "diluted earnings per diluted share",
            "diluted eps",
        ),
        requires_scale=False,
    ),
    MetricSpec(
        "eps_basic",
        Unit.CURRENCY_PER_SHARE,
        ("basic earnings per share", "earnings per basic share", "basic eps"),
        requires_scale=False,
    ),
    MetricSpec(
        "gross_margin",
        Unit.PERCENT,
        ("gross margin",),
        requires_scale=False,
    ),
)
"""V1 vocabulary. Small on purpose: every entry is a line item whose name is
standardised across issuers, so a match is a match on the same concept. Terms
issuers define for themselves -- "bookings", "ARR", "adjusted EBITDA" -- are
absent, because the same word means different things at different companies and
a store of them would look comparable without being so."""

_METRIC_BY_LABEL: Final[tuple[tuple[str, MetricSpec], ...]] = tuple(
    (label, spec) for spec in V1_METRICS for label in spec.labels
)


NON_GAAP_MARKERS: Final[tuple[str, ...]] = (
    "non-gaap",
    "non gaap",
    "adjusted",
    "pro forma",
    "excluding",
    "before special items",
    "constant currency",
    "organic",
    "normalized",
    "normalised",
)
"""Any of these in the sentence refuses it whole. Over-broad on purpose: the
cost of refusing a GAAP sentence is one missing fact, and the cost of accepting
a non-GAAP one is a number stored as comparable to a history it is not
comparable to."""


FORWARD_LOOKING: Final[tuple[str, ...]] = (
    "expect",
    "expects",
    "expected",
    "expectation",
    "anticipate",
    "anticipates",
    "anticipated",
    "outlook",
    "guidance",
    "forecast",
    "project",
    "projects",
    "projected",
    "estimate",
    "estimates",
    "estimated",
    "target",
    "targets",
    "will be",
    "intends",
    "plans to",
    "believes",
    "should be",
)
"""A sentence containing any of these states an expectation, not a result.

Two reasons it is refused outright rather than merely blocked from inheriting a
period. First, a forecast is not a fact, and this store holds only what a
document reported. Second -- the reason it is a *safety* rule rather than a
scope preference -- guidance sits in the same paragraph as results often
enough that period inheritance would silently stamp the reported quarter onto
it. Measured: *"Revenue for the quarter ended June 30, 2026 was $90.0 billion.
Operating income is expected to be $40.0 billion."* emitted the guidance figure
as that quarter's operating income, correctly cited to a sentence that says
"expected"."""


# ------------------------------------------------------------- currency
_DOLLAR_QUALIFIERS: Final[tuple[str, ...]] = ("c$", "a$", "nz$", "s$", "hk$", "r$", "cad$")

CURRENCY_SYMBOLS: Final[dict[str, str]] = {
    "us$": "USD",
    "c$": "CAD",
    "cad$": "CAD",
    "a$": "AUD",
    "nz$": "NZD",
    "s$": "SGD",
    "hk$": "HKD",
    "r$": "BRL",
    "€": "EUR",
    "£": "GBP",
}
"""Symbols that name exactly one currency. ``¥`` is deliberately absent: it is
used for both the yen and the renminbi, and a document using it has not said
which."""


def document_currency(text: str) -> str:
    """What a bare ``$`` means in this document, or ``UNKNOWN``.

    A dollar sign is not a currency. It is used by at least eight, and a
    document that writes ``C$`` somewhere is a document where an unqualified
    ``$`` cannot be assumed to be American. So the bare symbol resolves to USD
    only where nothing in the document contests it -- never merely because the
    filing was made with the SEC, which foreign issuers do in their own
    currency.
    """
    lowered = text.lower()
    if any(q in lowered for q in _DOLLAR_QUALIFIERS):
        return UNKNOWN_CURRENCY
    return "USD"


# --------------------------------------------------------------- numbers
_SCALES: Final[dict[str, float]] = {
    "thousand": 1e3,
    "thousands": 1e3,
    "million": 1e6,
    "millions": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "trillion": 1e12,
    "trillions": 1e12,
}

_MONEY = re.compile(
    r"(?P<sym>US\$|CAD\$|C\$|A\$|NZ\$|S\$|HK\$|R\$|\$|€|£)\s?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?P<scale>thousands?|millions?|billions?|trillions?))?",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(?P<num>\d+(?:\.\d+)?)\s?(?:%|percent\b)")


@dataclass(frozen=True, slots=True)
class Amount:
    value: float
    currency: str
    scaled: bool


def _amounts(sentence: str, fallback_currency: str) -> list[Amount]:
    found: list[Amount] = []
    for match in _MONEY.finditer(sentence):
        symbol = match.group("sym").lower()
        currency = CURRENCY_SYMBOLS.get(symbol, fallback_currency)
        scale = match.group("scale")
        multiplier = _SCALES[scale.lower()] if scale else 1.0
        found.append(
            Amount(
                value=float(match.group("num").replace(",", "")) * multiplier,
                currency=currency,
                scaled=scale is not None,
            )
        )
    return found


# ---------------------------------------------------------------- periods
_MONTHS: Final[dict[str, int]] = {
    m: i + 1
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        )
    )
}
MAX_DAY = 31
_DATE = r"(?P<month>[A-Z][a-z]+)\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"

_PERIOD_RULES: Final[tuple[tuple[re.Pattern[str], FiscalPeriod], ...]] = (
    (
        re.compile(rf"trailing\s+twelve\s+months?\s+ended\s+{_DATE}"),
        FiscalPeriod.TRAILING_TWELVE_MONTHS,
    ),
    (re.compile(rf"(?:three|3)[-\s]months?\s+ended\s+{_DATE}"), FiscalPeriod.QUARTER),
    (re.compile(rf"quarter\s+ended\s+{_DATE}"), FiscalPeriod.QUARTER),
    (re.compile(rf"(?:six|nine|6|9)[-\s]months?\s+ended\s+{_DATE}"), FiscalPeriod.YEAR_TO_DATE),
    (re.compile(rf"(?:twelve|12)[-\s]months?\s+ended\s+{_DATE}"), FiscalPeriod.YEAR),
    (re.compile(rf"year\s+ended\s+{_DATE}"), FiscalPeriod.YEAR),
    (re.compile(rf"as\s+of\s+{_DATE}"), FiscalPeriod.INSTANT),
)
"""Ordered longest-phrase-first, because ``second quarter ended`` also matches
``quarter ended`` and the specific reading must win. Every rule requires a
calendar date: a fiscal-quarter name on its own identifies a period only to
someone who already knows the issuer's calendar."""


def _iso(match: re.Match[str]) -> str | None:
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    day, year = int(match.group("day")), int(match.group("year"))
    if not 1 <= day <= MAX_DAY:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _periods(sentence: str) -> set[tuple[FiscalPeriod, str]]:
    """Every distinct period the sentence names. More than one is a refusal."""
    found: set[tuple[FiscalPeriod, str]] = set()
    claimed: list[tuple[int, int]] = []
    for pattern, kind in _PERIOD_RULES:
        for match in pattern.finditer(sentence):
            if any(s <= match.start() < e for s, e in claimed):
                # A longer rule already read these words. ``three months ended``
                # and ``quarter ended`` describe the same span, not two periods.
                continue
            date = _iso(match)
            if date is None:
                continue
            claimed.append((match.start(), match.end()))
            found.add((kind, date))
    return found


_COMPARISONS = re.compile(r"(?i)year[-\s]over[-\s]year|from\s+a\s+year\s+ago|year\s+on\s+year")

_PERIOD_WORDS: Final[dict[FiscalPeriod, tuple[str, ...]]] = {
    FiscalPeriod.QUARTER: ("quarter", "quarterly", "three months", "3 months"),
    FiscalPeriod.YEAR: (
        "full year",
        "full-year",
        "fiscal year",
        "year ended",
        "annual",
        "twelve months",
        "12 months",
    ),
    FiscalPeriod.YEAR_TO_DATE: (
        "year to date",
        "year-to-date",
        "six months",
        "nine months",
        "first half",
        "first nine months",
    ),
}
"""Period vocabulary by shape, used only to *block* inheritance -- never to
establish a period. ``year over year`` and ``from a year ago`` are stripped
first: they compare periods rather than naming one, and reading them as an
annual claim would refuse most of every earnings release."""


def _conflicts(sentence: str, kind: FiscalPeriod) -> str | None:
    """A period shape the sentence names that is not the paragraph's shape.

    This is what stops *"Revenue for the year ended December 31, 2025 was $X.
    Fourth-quarter revenue was $Y."* from stamping the annual date onto the
    quarterly figure. The second sentence says *quarter*, the paragraph says
    *year*, and disagreement is a refusal.
    """
    lowered = _COMPARISONS.sub(" ", sentence.lower())
    for shape, words in _PERIOD_WORDS.items():
        if shape is kind:
            continue
        found = next((w for w in words if w in lowered), None)
        if found:
            return found
    return None


# -------------------------------------------------------------- sentences
_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "inc.",
    "corp.",
    "ltd.",
    "co.",
    "no.",
    "nos.",
    "mr.",
    "ms.",
    "dr.",
    "st.",
    "jr.",
    "sr.",
    "u.s.",
    "e.g.",
    "i.e.",
)
_SPLIT = re.compile(r"(?<![0-9])(?<=[.!?])\s+(?=[\"(A-Z])")


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence, and the paragraph it belongs to."""

    start: int
    end: int
    text: str
    paragraph: str
    """The whole flattened block. Consulted only for the period -- see
    :func:`_resolve_period`. Never for the metric, the value or the basis."""


def sentences(text: str) -> list[Sentence]:
    """Sentences over the normalised text, each carrying its paragraph.

    Offsets are into the text as given, so they stay valid as evidence. Line
    breaks end a sentence: after normalisation a newline came from a
    block-level tag, which is a stronger boundary than a full stop.
    """
    out: list[Sentence] = []
    for line_start, line in _lines(text):
        cursor = 0
        first = len(out)
        for piece in _SPLIT.split(line):
            index = line.find(piece, cursor)
            if index < 0:
                continue
            cursor = index + len(piece)
            if _joins_abbreviation(line, index) and len(out) > first:
                prior = out.pop()
                out.append(
                    Sentence(prior.start, line_start + cursor, f"{prior.text} {piece}", line)
                )
                continue
            stripped = piece.strip()
            if stripped:
                out.append(Sentence(line_start + index, line_start + cursor, stripped, line))
    return out


def _lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    offset = 0
    for line in text.split("\n"):
        lines.append((offset, line))
        offset += len(line) + 1
    return lines


def _joins_abbreviation(line: str, index: int) -> bool:
    """Whether the split before ``index`` cut an abbreviation, not a sentence."""
    before = line[:index].rstrip().lower()
    return any(before.endswith(a) for a in _ABBREVIATIONS)


# --------------------------------------------------------------- outcomes
@dataclass(frozen=True, slots=True)
class FactRefusal:
    """A sentence that looked extractable and was not. Kept, and countable."""

    status: FactStatus
    metric: str | None
    detail: str
    excerpt: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": str(self.status),
            "metric": self.metric,
            "detail": self.detail,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    facts: tuple[ResearchFact, ...]
    refusals: tuple[FactRefusal, ...]

    @property
    def yield_rate(self) -> float:
        total = len(self.facts) + len(self.refusals)
        return len(self.facts) / total if total else 0.0


def fact_id(document_id: str, metric: str, period: str, version: str) -> str:
    """Deterministic identity: one document states one value per metric period.

    ``extraction_version`` is part of it, so a parser change produces new rows
    beside the old ones rather than silently rewriting history under the same
    identity. Two versions disagreeing about the same sentence is a thing a
    reader should be able to see.
    """
    return hashlib.sha256(f"{document_id}|{metric}|{period}|{version}".encode()).hexdigest()[:32]


NEUTRAL_BEFORE: Final[frozenset[str]] = frozenset(
    {
        # Determiners and possessives -- they point at the company, not a part.
        "the",
        "a",
        "an",
        "its",
        "our",
        "their",
        "this",
        # Words naming the whole entity.
        "company",
        "companys",
        "consolidated",
        "corporate",
        "group",
        "total",
        "overall",
        "aggregate",
        "worldwide",
        "global",
        # Accounting basis. Only GAAP survives this far; the rest is refused
        # earlier by NON_GAAP_MARKERS.
        "gaap",
        "unaudited",
        "reported",
        # Period words. These narrow *when*, never *what*, and cannot name a
        # segment -- so they pass here and are then held to the far stricter
        # period rule, which is where "Fourth-quarter revenue" inside a
        # year-dated paragraph is actually caught.
        "quarterly",
        "annual",
        "yearly",
        "monthly",
        "quarter",
        "quarters",
        "year",
        "years",
        "half",
        "interim",
        # Verbs, prepositions and conjunctions: grammatical glue, never a
        # modifier of the metric's scope.
        "was",
        "were",
        "is",
        "are",
        "had",
        "has",
        "posted",
        "announced",
        "delivered",
        "generated",
        "achieved",
        "recorded",
        "reports",
        "of",
        "in",
        "for",
        "and",
        "with",
        "to",
        "on",
        "at",
        "by",
        "from",
        "that",
        "which",
        "as",
        "while",
        "including",
    }
)
"""The complete set of words that may precede a metric label.

**An allowlist, and the direction is the point.** The first version of this
rule was a blacklist of scope nouns -- ``segment``, ``business``, ``product`` --
and it failed open: ``business-unit revenue``, ``recurring revenue`` and
``Client business-unit revenue`` all passed straight through it, because no
list of English nouns is ever finished. Every omission from a blacklist emits a
wrong number; every omission from an allowlist costs a fact. Only one of those
is survivable, so the unknown word refuses.

The words here are not a vocabulary of business terms. They are determiners,
entity words, basis words, period adjectives and grammatical glue -- the
categories that cannot narrow *what* is being measured. Anything outside them
is a modifier, and a modifier means the figure is about a part of the
company."""

_CLAUSE_EDGE: Final = ".:;,•()[]\u2014\u2013-\u201c\u201d\"'"
"""Punctuation a metric label may sit immediately after. A label opening a
clause has nothing modifying it."""

_WORD = re.compile(r"[^A-Za-z']+")


def _qualifier(sentence: str, start: int) -> str | None:
    """The word narrowing a metric label to something smaller, if any.

    Without this, ``Data Center segment revenue was $6.7 billion`` extracts as
    the company's revenue -- a figure four times too small, filed under the
    right metric name, with a citation that reads correctly to anyone who does
    not notice the two words before it. AMD's release was measured carrying six
    such sentences, Apple's and NVIDIA's several more. It is the one way this
    module could produce a wrong number rather than no number.
    """
    before = sentence[:start].rstrip()
    if not before or before[-1] in _CLAUSE_EDGE:
        return None
    word = _WORD.split(before)[-1]
    if not word:
        return None
    return None if word.lower() in NEUTRAL_BEFORE else word


def _match_metric(sentence: str) -> tuple[MetricSpec | None, int, str | None]:
    """The metric named, how many were named, and any word narrowing it."""
    lowered = sentence.lower()
    hits: dict[str, MetricSpec] = {}
    qualifier: str | None = None
    for label, spec in _METRIC_BY_LABEL:
        position = lowered.find(label)
        if position < 0:
            continue
        if spec.metric not in hits:
            hits[spec.metric] = spec
            qualifier = qualifier or _qualifier(sentence, position)
    if len(hits) != 1:
        return None, len(hits), None
    return next(iter(hits.values())), 1, qualifier


def _resolve_period(sentence: str, paragraph: str) -> tuple[FiscalPeriod, str] | str:
    """The period governing ``sentence``, or the reason there isn't one.

    The sentence is asked first. Only if it names none does the paragraph
    answer, and only when the paragraph names exactly one and the sentence says
    nothing that contradicts it.
    """
    own = _periods(sentence)
    if len(own) == 1:
        return next(iter(own))
    if own:
        return f"{len(own)} dated periods named in one sentence"

    inherited = _periods(paragraph)
    if not inherited:
        return "no dated period in the sentence or its paragraph"
    if len(inherited) > 1:
        return f"{len(inherited)} dated periods in the paragraph; none in the sentence"
    kind, date = next(iter(inherited))
    conflict = _conflicts(sentence, kind)
    if conflict:
        return f"sentence says '{conflict}', paragraph is dated to a {str(kind).lower()}"
    return kind, date


def extract(
    text: str,
    *,
    document: ResearchDocument,
    event_id: str,
    extraction_version: str,
) -> ExtractionOutcome:
    """Every fact the document states outright, and why the rest were refused."""
    fallback = document_currency(text)
    facts: dict[str, ResearchFact] = {}
    conflicting: set[str] = set()
    refusals: list[FactRefusal] = []

    for found in sentences(text):
        sentence, paragraph, start, end = found.text, found.paragraph, found.start, found.end
        if "\t" in sentence or len(sentence) > MAX_SENTENCE:
            continue
        lowered = sentence.lower()
        spec, count, qualifier = _match_metric(sentence)
        if count == 0:
            continue
        excerpt = sentence[:MAX_SENTENCE]
        if spec is None:
            refusals.append(
                FactRefusal(
                    FactStatus.AMBIGUOUS_METRIC,
                    None,
                    f"{count} metrics named in one sentence",
                    excerpt,
                )
            )
            continue
        candidate = _value_for(spec, sentence, fallback)
        if candidate is None:
            continue
        ahead = next((f for f in FORWARD_LOOKING if f in lowered), None)
        if ahead is not None:
            refusals.append(
                FactRefusal(
                    FactStatus.NO_STRUCTURED_FACT,
                    spec.metric,
                    f"forward-looking ('{ahead}'); an expectation, not a reported figure",
                    excerpt,
                )
            )
            continue
        marker = next((m for m in NON_GAAP_MARKERS if m in lowered), None)
        if marker:
            refusals.append(
                FactRefusal(FactStatus.NON_GAAP_BASIS, spec.metric, f"names '{marker}'", excerpt)
            )
            continue
        if qualifier is not None:
            refusals.append(
                FactRefusal(
                    FactStatus.AMBIGUOUS_METRIC,
                    spec.metric,
                    f"narrowed by '{qualifier}'; not the company-level figure",
                    excerpt,
                )
            )
            continue
        if isinstance(candidate, FactRefusal):
            refusals.append(candidate)
            continue
        value, currency = candidate

        resolved = _resolve_period(sentence, paragraph)
        if isinstance(resolved, str):
            refusals.append(
                FactRefusal(FactStatus.AMBIGUOUS_PERIOD, spec.metric, resolved, excerpt)
            )
            continue
        fiscal_period, date = resolved

        key = f"{spec.metric}|{fiscal_period}|{date}"
        fact = ResearchFact(
            fact_id=fact_id(document.document_id, spec.metric, key, extraction_version),
            event_id=event_id,
            company_id=document.company_id,
            metric=spec.metric,
            value=value,
            unit=spec.unit,
            currency=currency,
            fiscal_period=fiscal_period,
            period_end=None if fiscal_period is FiscalPeriod.INSTANT else date,
            instant=date if fiscal_period is FiscalPeriod.INSTANT else None,
            basis="GAAP",
            document_id=document.document_id,
            evidence=EvidenceReference(
                document=document.filename,
                url=document.source_url,
                role=str(document.role),
                content_sha256=document.content_hash,
                byte_size=document.raw_size,
                text_start=start,
                text_end=end,
                evidence_text=excerpt,
            ),
            extraction_method=EXTRACTION_METHOD,
            extraction_confidence=Confidence.HIGH,
            extraction_version=extraction_version,
        )
        prior = facts.get(key)
        if prior is not None and prior.value != fact.value:
            # The same document said two different things about one metric and
            # period. Both are dropped: there is no rule for picking, and one of
            # them is wrong.
            conflicting.add(key)
            refusals.append(
                FactRefusal(
                    FactStatus.AMBIGUOUS_VALUE,
                    spec.metric,
                    f"document states {prior.value} and {fact.value} for the same period",
                    excerpt,
                )
            )
            continue
        facts[key] = fact

    for key in conflicting:
        facts.pop(key, None)
    return ExtractionOutcome(
        facts=tuple(facts[k] for k in sorted(facts)),
        refusals=tuple(refusals),
    )


def _value_for(
    spec: MetricSpec, sentence: str, fallback: str
) -> tuple[float, str] | FactRefusal | None:
    """The value, a refusal, or ``None`` when the sentence made no claim at all.

    The three outcomes are genuinely different, and measurement showed why the
    distinction matters. A line reading only ``Diluted earnings per share`` is a
    flattened table header: it names a metric and asserts nothing. Counting
    those as refusals made them the single largest "reason" on NVIDIA's press
    release, and the reason was that a heading is not a claim. They are skipped
    in silence; nothing that was refused becomes accepted.
    """
    if spec.unit == Unit.PERCENT:
        return _percentage(spec, sentence)
    return _money(spec, sentence, fallback)


def _percentage(spec: MetricSpec, sentence: str) -> tuple[float, str] | FactRefusal | None:
    percents = _PERCENT.findall(sentence)
    if not percents:
        return None
    if len(percents) != 1:
        return FactRefusal(
            FactStatus.AMBIGUOUS_VALUE,
            spec.metric,
            f"{len(percents)} percentages in one sentence",
            sentence[:MAX_SENTENCE],
        )
    return float(percents[0]), UNKNOWN_CURRENCY


def _money(
    spec: MetricSpec, sentence: str, fallback: str
) -> tuple[float, str] | FactRefusal | None:
    excerpt = sentence[:MAX_SENTENCE]
    amounts = _amounts(sentence, fallback)
    if not amounts:
        return None
    if len(amounts) != 1:
        return FactRefusal(
            FactStatus.AMBIGUOUS_VALUE,
            spec.metric,
            f"{len(amounts)} currency amounts in one sentence",
            excerpt,
        )
    amount = amounts[0]
    if amount.currency == UNKNOWN_CURRENCY:
        return FactRefusal(
            FactStatus.UNKNOWN_CURRENCY,
            spec.metric,
            "document uses an unqualified '$' alongside another dollar currency",
            excerpt,
        )
    if spec.requires_scale and not amount.scaled:
        return FactRefusal(
            FactStatus.AMBIGUOUS_UNIT,
            spec.metric,
            "amount states no scale, and the caption that would is not in the sentence",
            excerpt,
        )
    return amount.value, amount.currency
