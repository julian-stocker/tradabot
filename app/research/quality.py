"""Classifying how much a recorded spread can be believed.

Phase 4 recorded after-hours spreads of 883-1118 bps on mega-caps with
``data_quality=OK``, and that flag was not wrong: the *bars* were fine. Bar
staleness and quote sanity are different questions, and conflating them is what
let an unusable number through unlabelled.

A wide spread is not evidence of a broken feed. At 21:30 UTC on IEX it is an
accurate report of a nearly empty book. The number is real; what it is not is an
*executable transaction cost*, and the difference only shows up once the session
is taken into account. So nothing here deletes or rewrites an observation --
:class:`~app.domain.enums.SpreadQuality` is attached alongside it, and research
queries decide what to exclude.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.core.time import ensure_utc
from app.domain.enums import SpreadQuality
from app.market_data.calendars import TradingCalendar
from app.scanner.enums import SessionPhase
from app.scanner.sessions import session_phase

QUOTE_QUALITY_VERSION: Final = "quote-quality-v1"

SUSPICIOUS_SPREAD_BPS: Final = 100.0
"""Above this, a regular-session spread is treated as a feed artefact.

100 bps is 1% of price to cross once. No S&P-scale instrument trades like that
during regular hours; on the names in this universe a normal figure is single
-digit bps. The threshold is intentionally far above anything legitimate rather
than tuned -- its job is to catch the 900 bps observations, not to police the
boundary between 4 and 6 bps.
"""

MAX_QUOTE_AGE: Final = timedelta(seconds=60)
"""Older than this and the quote describes a different market than the bar."""


@dataclass(frozen=True, slots=True)
class QuoteAssessment:
    """A spread observation with the context needed to judge it."""

    quality: SpreadQuality
    session_phase: SessionPhase
    spread_bps: float | None
    quote_age_seconds: float | None
    version: str = QUOTE_QUALITY_VERSION

    @property
    def is_reliable(self) -> bool:
        return self.quality.is_reliable


def classify_spread(
    *,
    spread_bps: float | None,
    observed_at: datetime,
    quote_age_seconds: float | None,
    calendar: TradingCalendar,
    suspicious_above_bps: float = SUSPICIOUS_SPREAD_BPS,
) -> QuoteAssessment:
    """Judge one spread observation.

    Order of checks is deliberate. Missing beats everything (there is nothing to
    judge), then staleness (an old quote's width describes the wrong instant),
    then the session (an after-hours spread is *expected* to be wide and saying
    "suspicious" would be wrong), and only then plausibility. Checking width
    before session is the mistake that would relabel every legitimate
    extended-hours quote as broken.
    """
    phase = session_phase(calendar, observed_at)

    if spread_bps is None:
        return QuoteAssessment(
            quality=SpreadQuality.MISSING,
            session_phase=phase,
            spread_bps=None,
            quote_age_seconds=quote_age_seconds,
        )

    if quote_age_seconds is not None and quote_age_seconds > MAX_QUOTE_AGE.total_seconds():
        return QuoteAssessment(
            quality=SpreadQuality.STALE,
            session_phase=phase,
            spread_bps=spread_bps,
            quote_age_seconds=quote_age_seconds,
        )

    if phase is not SessionPhase.REGULAR:
        return QuoteAssessment(
            quality=SpreadQuality.EXTENDED_HOURS,
            session_phase=phase,
            spread_bps=spread_bps,
            quote_age_seconds=quote_age_seconds,
        )

    quality = (
        SpreadQuality.SUSPICIOUS_SPREAD
        if spread_bps > suspicious_above_bps or spread_bps < 0
        else SpreadQuality.REGULAR_SESSION
    )
    return QuoteAssessment(
        quality=quality,
        session_phase=phase,
        spread_bps=spread_bps,
        quote_age_seconds=quote_age_seconds,
    )


def is_regular_session(moment: datetime, calendar: TradingCalendar) -> bool:
    """Whether ``moment`` falls in the regular session.

    The primary benchmark filter (part L). Kept here rather than inlined so the
    backtest, the labeller and the exporter cannot drift apart on the definition.
    """
    return session_phase(calendar, ensure_utc(moment)) is SessionPhase.REGULAR
