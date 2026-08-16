"""How a Tradabot state looks and what it means, decided once.

Two mappings live here and nowhere else: the semantic colour of a state, and the
plain-English sentence that explains it. Scattering either across formatters is
how the same condition ends up amber in one channel and red in another, and
explained in one place but not the next.

Colour is a category, never a forecast
--------------------------------------
This is the constraint that shapes the whole mapping. It is tempting to colour
rising volume green because "activity is good" or rising volatility red because
"risk is bad", and both would be quietly asserting something about what happens
next. Tradabot has no validated predictive edge -- Phase 12.25 established that
directly -- so a colour that implied one would be the single most misleading
thing on the screen.

So the categories describe the *current observed condition* only:

``GREEN``  a condition that is presently favourable: net cash, reduced
           concentration, a healthy component, data that is ready.
``RED``    a condition that is presently unfavourable: material dilution,
           excessive concentration, a failing component.
``ORANGE`` unusual and worth a look, with no inherent direction: unusual volume,
           unusual volatility, a regime change.
``YELLOW`` uncertainty: low confidence, partial coverage, stale data.
``BLUE``   ordinary descriptive information.
``GREY``   unavailable, not applicable, or deliberately refused.

"Trending up" is describable as a positive *current* trend state. It is not a
prediction, and the explanation attached to it says so.

Explanations are explanations
-----------------------------
Each sentence says what the state means about what has already been observed. It
does not say what to do about it, because that would be a recommendation, and no
evidence in this repository supports one.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final


class Semantic(StrEnum):
    """The six presentation categories. Meaning first, colour second."""

    GOOD = "GREEN"
    BAD = "RED"
    UNUSUAL = "ORANGE"
    UNCERTAIN = "YELLOW"
    NEUTRAL = "BLUE"
    UNAVAILABLE = "GREY"


COLOURS: Final[dict[Semantic, int]] = {
    Semantic.GOOD: 0x2ECC71,
    Semantic.BAD: 0xE74C3C,
    Semantic.UNUSUAL: 0xE67E22,
    Semantic.UNCERTAIN: 0xF1C40F,
    Semantic.NEUTRAL: 0x3498DB,
    Semantic.UNAVAILABLE: 0x95A5A6,
}

CATEGORY_MEANING: Final[dict[Semantic, str]] = {
    Semantic.GOOD: "a presently favourable condition",
    Semantic.BAD: "a presently unfavourable condition",
    Semantic.UNUSUAL: "unusual and worth inspecting; no direction implied",
    Semantic.UNCERTAIN: "incomplete data or reduced confidence",
    Semantic.NEUTRAL: "ordinary descriptive information",
    Semantic.UNAVAILABLE: "unavailable, not applicable, or deliberately refused",
}


class State:
    """One displayable state: its label, its category and its explanation."""

    __slots__ = ("explanation", "internal", "label", "semantic", "short")

    def __init__(
        self,
        internal: str,
        label: str,
        semantic: Semantic,
        explanation: str | None,
        short: str | None = None,
    ) -> None:
        self.internal = internal
        self.label = label
        self.semantic = semantic
        self.explanation = explanation
        self.short = short or label
        """Wording for use inside a sentence. A label that reads well as a
        heading often reads badly mid-prose: "moved to high vs its own history
        against its own history" says it twice."""

    @property
    def colour(self) -> int:
        return COLOURS[self.semantic]

    def as_dict(self) -> dict[str, Any]:
        return {
            "internal_state": self.internal,
            "display_label": self.label,
            "semantic_category": str(self.semantic),
            "colour": f"#{self.colour:06X}",
            "human_explanation": self.explanation,
            "directional_forecast": False,
            "recommendation": False,
        }


def _s(
    internal: str,
    label: str,
    semantic: Semantic,
    explanation: str | None = None,
    short: str | None = None,
) -> State:
    return State(internal, label, semantic, explanation, short)


STATES: Final[dict[str, State]] = {
    # -- market regime ----------------------------------------------------
    "TRENDING_UP": _s(
        "TRENDING_UP", "Trending up", Semantic.GOOD,
        "The benchmark is trading above its long-term average. A description of "
        "the current trend, not a forecast.",
    ),
    "TRENDING_DOWN": _s(
        "TRENDING_DOWN", "Trending down", Semantic.BAD,
        "The benchmark is trading below its long-term average. A description of "
        "the current trend, not a forecast.",
    ),
    "RANGE_BOUND": _s(
        "RANGE_BOUND", "Range-bound", Semantic.NEUTRAL,
        "The benchmark is close to its long-term average, with no clear trend "
        "either way.",
    ),
    "INSUFFICIENT_HISTORY": _s(
        "INSUFFICIENT_HISTORY", "Insufficient history", Semantic.UNAVAILABLE,
        "There is not enough price history to describe a regime.",
    ),
    # -- market events ----------------------------------------------------
    "UNUSUAL_VOLUME": _s(
        "UNUSUAL_VOLUME", "Unusual volume", Semantic.UNUSUAL,
        "Traded far more than its recent typical volume. An activity signal, "
        "not a direction.",
    ),
    "UNUSUAL_VOLATILITY": _s(
        "UNUSUAL_VOLATILITY", "Unusual volatility", Semantic.UNUSUAL,
        "Recent price variability is materially above its normal historical "
        "level. This is an activity and risk signal, not a directional forecast.",
    ),
    "MARKET_REGIME_CHANGE": _s(
        "MARKET_REGIME_CHANGE", "Market regime change", Semantic.UNUSUAL,
        "The benchmark moved to a different trend state and held it long enough "
        "to be reported.",
    ),
    "SECTOR_MOVE": _s(
        "SECTOR_MOVE", "Sector move", Semantic.UNUSUAL,
        "A sector basket moved materially over five sessions.",
    ),
    "RELATIVE_STRENGTH_CHANGE": _s(
        "RELATIVE_STRENGTH_CHANGE", "Relative strength change", Semantic.UNUSUAL,
        "Its twelve-month return crossed from one side of the benchmark's to the "
        "other.",
    ),
    "NEW_SEC_FILING": _s(
        "NEW_SEC_FILING", "New SEC filing", Semantic.NEUTRAL,
        "A filing appeared that was not present at the previous observation.",
    ),
    "FUNDAMENTAL_CHANGE": _s(
        "FUNDAMENTAL_CHANGE", "Fundamental change", Semantic.UNUSUAL,
        "A trailing twelve-month figure moved materially between observations.",
    ),
    "VALUATION_STATE_CHANGE": _s(
        "VALUATION_STATE_CHANGE", "Valuation state change", Semantic.UNUSUAL,
        "Its valuation moved into a different band of its own history.",
    ),
    "COMPANY_CONFIDENCE_CHANGE": _s(
        "COMPANY_CONFIDENCE_CHANGE", "Data confidence change", Semantic.UNCERTAIN,
        "The quality of the data behind the company analysis changed. This is "
        "about the data, not about the company.",
    ),
    "DATA_HEALTH_CHANGE": _s(
        "DATA_HEALTH_CHANGE", "Data health change", Semantic.BAD,
        "The state of the fundamentals data store changed.",
    ),
    # -- valuation --------------------------------------------------------
    "VERY_HIGH_VS_HISTORY": _s(
        "VERY_HIGH_VS_HISTORY", "Very high vs its own history", Semantic.UNUSUAL,
        "Priced near the top of its own historical range. Expensive relative to "
        "its past, which says nothing about where it goes next.",
        short="very high",
    ),
    "HIGH_VS_HISTORY": _s(
        "HIGH_VS_HISTORY", "High vs its own history", Semantic.UNUSUAL,
        "Priced above its own historical norm.",
        short="high",
    ),
    "NORMAL_VS_HISTORY": _s(
        "NORMAL_VS_HISTORY", "Normal vs its own history", Semantic.NEUTRAL,
        "Priced in line with its own historical range.",
        short="normal",
    ),
    "LOW_VS_HISTORY": _s(
        "LOW_VS_HISTORY", "Low vs its own history", Semantic.UNUSUAL,
        "Priced below its own historical norm. Cheap relative to its past is not "
        "the same as underpriced.",
        short="low",
    ),
    "VERY_LOW_VS_HISTORY": _s(
        "VERY_LOW_VS_HISTORY", "Very low vs its own history", Semantic.UNUSUAL,
        "Priced near the bottom of its own historical range.",
        short="very low",
    ),
    # -- balance sheet and capital structure -------------------------------
    "NET_CASH": _s(
        "NET_CASH", "Net cash", Semantic.GOOD,
        "Cash and liquid resources exceed debt.",
    ),
    "ACCEPTABLE": _s(
        "ACCEPTABLE", "Acceptable balance sheet", Semantic.NEUTRAL,
        "Debt is present but modest relative to the balance sheet.",
    ),
    "LEVERAGED": _s(
        "LEVERAGED", "Leveraged", Semantic.BAD,
        "Debt is large relative to equity and cash.",
    ),
    "MATERIAL_DILUTION": _s(
        "MATERIAL_DILUTION", "Material dilution", Semantic.BAD,
        "The share count increased materially, reducing each existing share's "
        "ownership percentage.",
    ),
    "BUYBACK_REDUCING_SHARE_COUNT": _s(
        "BUYBACK_REDUCING_SHARE_COUNT", "Share count reducing", Semantic.GOOD,
        "The share count fell, so each remaining share represents a larger "
        "ownership percentage.",
    ),
    "STABLE": _s(
        "STABLE", "Share count stable", Semantic.NEUTRAL,
        "The share count has not changed materially.",
    ),
    # -- portfolio --------------------------------------------------------
    "HIGH_CONCENTRATION": _s(
        "HIGH_CONCENTRATION", "High concentration", Semantic.BAD,
        "A large share of the portfolio sits in very few positions, so single-name "
        "outcomes dominate the result.",
    ),
    "MODERATE_CONCENTRATION": _s(
        "MODERATE_CONCENTRATION", "Moderate concentration", Semantic.NEUTRAL,
        "The largest holdings carry a meaningful but not dominant share.",
    ),
    "LOW_CONCENTRATION": _s(
        "LOW_CONCENTRATION", "Low concentration", Semantic.GOOD,
        "Holdings are spread widely enough that no single one dominates.",
    ),
    "EXTREME_OVERLAP": _s(
        "EXTREME_OVERLAP", "Extreme overlap", Semantic.BAD,
        "Moves almost in lockstep with existing holdings, so they behave as one "
        "position rather than several.",
    ),
    "HIGH_OVERLAP": _s(
        "HIGH_OVERLAP", "High overlap", Semantic.BAD,
        "Moves similarly to existing holdings and increases concentration risk.",
    ),
    "ELEVATED_OVERLAP": _s(
        "ELEVATED_OVERLAP", "Elevated overlap", Semantic.UNUSUAL,
        "Moves somewhat together with existing holdings; more shared exposure "
        "than a typical pair of stocks.",
    ),
    "NORMAL_OVERLAP": _s(
        "NORMAL_OVERLAP", "Normal overlap", Semantic.NEUTRAL,
        "Shares about as much movement with the portfolio as any two stocks do.",
    ),
    "IMPROVES_DIVERSIFICATION": _s(
        "IMPROVES_DIVERSIFICATION", "Improves diversification", Semantic.GOOD,
        "Historically moves relatively independently of the existing portfolio.",
    ),
    "INCREASES_CONCENTRATION": _s(
        "INCREASES_CONCENTRATION", "Increases concentration", Semantic.BAD,
        "Adds to an exposure the portfolio already carries heavily.",
    ),
    "NEUTRAL_FIT": _s(
        "NEUTRAL_FIT", "Neutral fit", Semantic.NEUTRAL,
        "Changes neither concentration nor correlation materially.",
    ),
    "ALREADY_HELD": _s(
        "ALREADY_HELD", "Already held", Semantic.NEUTRAL,
        "This position is already in the portfolio.",
    ),
    "CASH ONLY": _s(
        "CASH ONLY", "Cash only", Semantic.NEUTRAL,
        "The account holds no positions, so there is no market exposure to describe.",
    ),
    # -- coverage ---------------------------------------------------------
    "FULL_PORTFOLIO": _s(
        "FULL_PORTFOLIO", "Full portfolio", Semantic.NEUTRAL,
        "Configured as representing the complete portfolio.",
    ),
    "PARTIAL_PORTFOLIO": _s(
        "PARTIAL_PORTFOLIO", "Partial portfolio", Semantic.UNCERTAIN,
        "This account is one part of a larger portfolio, so percentages describe "
        "this account only.",
    ),
    "US_ONLY_VIEW": _s(
        "US_ONLY_VIEW", "US-listed holdings only", Semantic.UNCERTAIN,
        "Only US-listed holdings are visible here; anything held elsewhere is not "
        "included in these percentages.",
    ),
    "ALPACA_ACCOUNT_ONLY": _s(
        "ALPACA_ACCOUNT_ONLY", "This account only", Semantic.UNCERTAIN,
        "These figures describe this brokerage account. They are not a view of "
        "total holdings or wealth.",
    ),
    # -- confidence and data ----------------------------------------------
    "HIGH": _s("HIGH", "High confidence", Semantic.GOOD,
               "The data behind this assessment is complete and consistent."),
    "MEDIUM": _s("MEDIUM", "Medium confidence", Semantic.NEUTRAL,
                 "The data behind this assessment is usable but not complete."),
    "LOW": _s("LOW", "Low confidence", Semantic.UNCERTAIN,
              "The underlying data is incomplete or weaker than required for a "
              "high-confidence assessment."),
    "INSUFFICIENT": _s("INSUFFICIENT", "Insufficient data", Semantic.UNAVAILABLE,
                       "There is not enough data to make this assessment."),
    "INSUFFICIENT_DATA": _s(
        "INSUFFICIENT_DATA", "Insufficient data", Semantic.UNAVAILABLE,
        "Tradabot does not have enough comparable data to make this assessment.",
    ),
    "ADVISOR_CONTEXT_UNAVAILABLE": _s(
        "ADVISOR_CONTEXT_UNAVAILABLE", "Company context unavailable",
        Semantic.UNAVAILABLE,
        "No company analysis is available for this symbol. The portfolio figures "
        "are unaffected.",
    ),
    "SECTOR_SPECIFIC_MODEL_REQUIRED": _s(
        "SECTOR_SPECIFIC_MODEL_REQUIRED", "Not applicable to this sector",
        Semantic.UNAVAILABLE,
        "This metric is not meaningful for this kind of company, so it is refused "
        "rather than estimated.",
    ),
    "READY": _s("READY", "Ready", Semantic.GOOD, "Data is present and current."),
    "DATA_NOT_SYNCED": _s(
        "DATA_NOT_SYNCED", "Not synced", Semantic.BAD,
        "The fundamentals store has never been built. Company analysis is "
        "unavailable until it is synced.",
    ),
    "DATA_STALE": _s(
        "DATA_STALE", "Stale", Semantic.UNCERTAIN,
        "The fundamentals store is readable but its newest filing is old, so "
        "recent quarters may be missing.",
    ),
    "DATA_CORRUPT": _s(
        "DATA_CORRUPT", "Corrupt", Semantic.BAD,
        "The fundamentals store cannot be read as expected and is not being used.",
    ),
    # -- materiality ------------------------------------------------------
    "CRITICAL": _s("CRITICAL", "Critical", Semantic.BAD, None),
    "SIGNIFICANT": _s("SIGNIFICANT", "Significant", Semantic.UNUSUAL, None),
    "NOTABLE": _s("NOTABLE", "Notable", Semantic.NEUTRAL, None),
    "ROUTINE": _s("ROUTINE", "Routine", Semantic.NEUTRAL, None),
    # -- operational ------------------------------------------------------
    "HEALTHY": _s("HEALTHY", "Healthy", Semantic.GOOD, None),
    "DEGRADED": _s("DEGRADED", "Degraded", Semantic.BAD,
                   "Some messages could not be delivered. Analysis is unaffected."),
    "NOT CONFIGURED": _s(
        "NOT CONFIGURED", "Not configured", Semantic.UNCERTAIN,
        "This check is not set up, so its state is unknown rather than good.",
    ),
    "UP": _s("UP", "Up", Semantic.GOOD, None),
    "LATE": _s("LATE", "Late", Semantic.UNCERTAIN,
               "A heartbeat is overdue but still inside the grace period."),
    "DOWN": _s("DOWN", "Down", Semantic.BAD,
               "No heartbeat has arrived for longer than the grace period."),
    "RECOVERED": _s("RECOVERED", "Recovered", Semantic.GOOD, None),
    "UNKNOWN": _s("UNKNOWN", "Unknown", Semantic.UNAVAILABLE, None),
}

_UNKNOWN = _s("", "", Semantic.NEUTRAL, None)


def state(internal: str | None) -> State:
    """The presentation for one internal state, or a neutral fallback.

    An unmapped state renders as itself in blue with no explanation, which is
    visibly plain rather than wrong. A test asserts every state the system can
    actually emit is mapped.
    """
    if not internal:
        return _UNKNOWN
    key = str(internal).strip()
    held = STATES.get(key)
    if held is not None:
        return held
    return _s(key, key.replace("_", " ").capitalize(), Semantic.NEUTRAL, None)


def label(internal: str | None) -> str:
    return state(internal).label or str(internal or "")


def explain(internal: str | None) -> str | None:
    return state(internal).explanation


def semantic(internal: str | None) -> Semantic:
    return state(internal).semantic


def colour(internal: str | None) -> int:
    return state(internal).colour


def worst(*internals: str | None) -> Semantic:
    """The most attention-worthy category among several states.

    Used to colour a whole card from its contents: one unfavourable condition in
    an otherwise ordinary report should tint the spine, or the reader has to find
    it by reading.
    """
    order = [
        Semantic.BAD,
        Semantic.UNUSUAL,
        Semantic.UNCERTAIN,
        Semantic.GOOD,
        Semantic.NEUTRAL,
        Semantic.UNAVAILABLE,
    ]
    found = [semantic(i) for i in internals if i]
    for candidate in order:
        if candidate in found:
            return candidate
    return Semantic.NEUTRAL


_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")


def humanise(text: str) -> str:
    """Replace internal state names in prose with their display labels.

    The monitoring engine writes summaries in its own vocabulary, and that
    vocabulary is correct for a journal and wrong for a reader. Translating at
    render time keeps the two concerns apart: presentation never edits what the
    engine concluded, only how it is spelled.
    """
    def swap(match: re.Match[str]) -> str:
        held = STATES.get(match.group(0))
        return held.short.lower() if held else match.group(0)

    return _TOKEN.sub(swap, text)


def as_dicts() -> list[dict[str, Any]]:
    """Every mapped state, for the vocabulary audit artifact."""
    return [s.as_dict() for s in STATES.values()]
