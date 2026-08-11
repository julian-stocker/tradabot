"""Shared vocabulary for the whole application.

``app.domain`` is a near-leaf package: it may only import from ``app.core``
(config, logging, time helpers) and never from ``features``/``signals``/``db``/``api``.
Every other module is allowed to import from here, which is what keeps
``features``/``signals``/``costs``/``backtesting`` free of import cycles.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum


class AssetType(StrEnum):
    """Broad instrument category. Drives which cost/feature assumptions apply."""

    STOCK = "STOCK"
    ETF = "ETF"
    INDEX = "INDEX"
    FUND = "FUND"
    CRYPTO = "CRYPTO"
    FX = "FX"
    OTHER = "OTHER"


class Timeframe(StrEnum):
    """Candle aggregation interval.

    Values are the canonical string form used in the API, the database and the
    provider abstraction. Use :meth:`duration` instead of re-parsing the string.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def duration(self) -> timedelta:
        """Nominal wall-clock length of one candle.

        Note: this is the *nominal* length. It deliberately ignores exchange
        sessions, holidays and half-days -- calendar handling is a phase 2 concern
        (see docs/roadmap.md) and must not be faked here.
        """
        return _TIMEFRAME_DURATIONS[self]

    @property
    def minutes(self) -> int:
        """Nominal length in whole minutes."""
        return int(self.duration.total_seconds() // 60)

    @property
    def is_intraday(self) -> bool:
        return self.duration < timedelta(days=1)


_TIMEFRAME_DURATIONS: dict[Timeframe, timedelta] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M30: timedelta(minutes=30),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
    Timeframe.W1: timedelta(weeks=1),
}


class HorizonBucket(StrEnum):
    """Coarse grouping of forecast horizons."""

    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"


class Horizon(StrEnum):
    """Explicit, named forecast horizons.

    Horizons are first-class from day one even though phase 1 only *evaluates*
    ``D1``-based signals. A signal without a horizon is meaningless: "bullish" is
    only falsifiable once you say over what period.
    """

    # Short term
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    D1 = "1d"
    # Medium term
    D3 = "3d"
    D5 = "5d"
    D20 = "20d"
    # Long term
    MO1 = "1mo"
    MO3 = "3mo"
    MO6 = "6mo"

    @property
    def bucket(self) -> HorizonBucket:
        return _HORIZON_BUCKETS[self]

    @property
    def duration(self) -> timedelta:
        """Approximate calendar duration of the horizon.

        Months are approximated as 30 days. This is adequate for grouping and
        display; anything that needs exact bar counts must use
        :meth:`bars_for_timeframe` against real data instead.
        """
        return _HORIZON_DURATIONS[self]

    def bars_for_timeframe(self, timeframe: Timeframe) -> int:
        """How many candles of ``timeframe`` nominally span this horizon.

        Used to size forward-return windows during labelling/backtesting.
        Always at least 1.
        """
        return max(1, round(self.duration / timeframe.duration))


_HORIZON_BUCKETS: dict[Horizon, HorizonBucket] = {
    Horizon.M15: HorizonBucket.SHORT_TERM,
    Horizon.M30: HorizonBucket.SHORT_TERM,
    Horizon.H1: HorizonBucket.SHORT_TERM,
    Horizon.H2: HorizonBucket.SHORT_TERM,
    Horizon.H4: HorizonBucket.SHORT_TERM,
    Horizon.D1: HorizonBucket.SHORT_TERM,
    Horizon.D3: HorizonBucket.MEDIUM_TERM,
    Horizon.D5: HorizonBucket.MEDIUM_TERM,
    Horizon.D20: HorizonBucket.MEDIUM_TERM,
    Horizon.MO1: HorizonBucket.LONG_TERM,
    Horizon.MO3: HorizonBucket.LONG_TERM,
    Horizon.MO6: HorizonBucket.LONG_TERM,
}

_HORIZON_DURATIONS: dict[Horizon, timedelta] = {
    Horizon.M15: timedelta(minutes=15),
    Horizon.M30: timedelta(minutes=30),
    Horizon.H1: timedelta(hours=1),
    Horizon.H2: timedelta(hours=2),
    Horizon.H4: timedelta(hours=4),
    Horizon.D1: timedelta(days=1),
    Horizon.D3: timedelta(days=3),
    Horizon.D5: timedelta(days=5),
    Horizon.D20: timedelta(days=20),
    Horizon.MO1: timedelta(days=30),
    Horizon.MO3: timedelta(days=90),
    Horizon.MO6: timedelta(days=180),
}


class Classification(StrEnum):
    """Discretised signal direction."""

    STRONG_BEARISH = "STRONG_BEARISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"
    STRONG_BULLISH = "STRONG_BULLISH"

    @property
    def direction(self) -> int:
        """-1 bearish, 0 neutral, +1 bullish."""
        return _CLASSIFICATION_DIRECTION[self]


_CLASSIFICATION_DIRECTION: dict[Classification, int] = {
    Classification.STRONG_BEARISH: -1,
    Classification.BEARISH: -1,
    Classification.NEUTRAL: 0,
    Classification.BULLISH: 1,
    Classification.STRONG_BULLISH: 1,
}


class Side(StrEnum):
    """Direction of a position or order."""

    LONG = "LONG"
    SHORT = "SHORT"


class ReasonKind(StrEnum):
    """Whether an explanation supports the signal or argues against it."""

    SUPPORT = "SUPPORT"
    RISK = "RISK"


class CorporateActionType(StrEnum):
    """Kind of corporate action.

    Only ``SPLIT`` and ``CASH_DIVIDEND`` are processed today. The remaining
    members are declared so the storage schema and the API enum do not have to
    change to *record* one -- adding support means implementing its adjustment
    rule, not migrating the database.

    ``SPLIT`` covers reverse splits too: a 1-for-10 reverse split is a ratio of
    0.1, and the adjustment arithmetic handles it without a special case.
    """

    SPLIT = "SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    # Recorded but not yet adjusted for:
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPIN_OFF = "SPIN_OFF"
    MERGER = "MERGER"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"

    @property
    def affects_price_series(self) -> bool:
        """True if this action requires a price adjustment to keep a series continuous."""
        return self in _PRICE_AFFECTING_ACTIONS


_PRICE_AFFECTING_ACTIONS: frozenset[CorporateActionType] = frozenset(
    {
        CorporateActionType.SPLIT,
        CorporateActionType.STOCK_DIVIDEND,
        CorporateActionType.SPIN_OFF,
    }
)


class PriceSeriesAdjustment(StrEnum):
    """Which price series a calculation consumes.

    This is a *required, explicit* choice at the data-loading boundary. Letting
    each indicator decide for itself is how a codebase ends up with an RSI on
    raw prices and a moving average on adjusted ones, silently disagreeing.

    ``RAW``
        Exactly what the provider delivered. The only series that is a factual
        record, and the only one preserved in the database. A split appears here
        as a genuine price discontinuity, which is correct: that *is* what
        traded.

    ``SPLIT_ADJUSTED``
        Prices rescaled so the series is continuous across splits, volumes
        rescaled inversely. The right default for feature calculation and
        charting. Dividends are **not** applied.

    ``TOTAL_RETURN``
        Split-adjusted *and* dividend-reinvested. Not implemented -- see
        docs/data-adjustments.md. It is a separate member rather than a variant
        of ``SPLIT_ADJUSTED`` precisely so the two can never be mixed up: a
        dividend-adjusted price is not a price anyone ever paid.
    """

    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    TOTAL_RETURN = "TOTAL_RETURN"


class TradeDecisionType(StrEnum):
    """Verdict of a simulation profile on a signal.

    ``SKIP`` decisions are recorded as deliberately as ``TRADE`` decisions. An
    opportunity declined is evidence about the strategy, and a system that only
    stores what it did cannot measure what it missed -- see docs/simulation-design.md.
    """

    TRADE = "TRADE"
    SKIP = "SKIP"


class DecisionReason(StrEnum):
    """Why a simulation profile reached its verdict.

    A stable, machine-readable code. The human-readable explanation lives
    alongside it, but aggregate analysis ("how often does the fee gate bite on
    the 50 EUR portfolio?") needs an enumerable reason.
    """

    # TRADE
    ACCEPTED = "ACCEPTED"
    # SKIP
    SCORE_BELOW_THRESHOLD = "SCORE_BELOW_THRESHOLD"
    CONFIDENCE_BELOW_THRESHOLD = "CONFIDENCE_BELOW_THRESHOLD"
    CLASSIFICATION_NEUTRAL = "CLASSIFICATION_NEUTRAL"
    NEGATIVE_NET_EDGE = "NEGATIVE_NET_EDGE"
    POSITION_BELOW_MIN_NOTIONAL = "POSITION_BELOW_MIN_NOTIONAL"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    SHORT_NOT_PERMITTED = "SHORT_NOT_PERMITTED"
    PROFILE_DISABLED = "PROFILE_DISABLED"


class ExitReason(StrEnum):
    """Why a position was closed.

    Essential for diagnosing a strategy: one that only ever exits via
    ``STOP_LOSS`` behaves nothing like one that exits via ``SIGNAL_REVERSAL``,
    even at identical total return.
    """

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MAX_HOLDING_PERIOD = "MAX_HOLDING_PERIOD"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    SIMULATION_END = "SIMULATION_END"
    MANUAL = "MANUAL"


class OrderType(StrEnum):
    """Order types the paper broker understands.

    Only ``MARKET`` is implemented. ``LIMIT`` is declared so the persisted enum
    does not need a migration when it arrives, but placing one raises rather than
    silently behaving like a market order.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderRejectionReason(StrEnum):
    """Why the broker refused an order at **execution** time.

    Deliberately distinct from :class:`DecisionReason`, which records why a
    profile did or did not *want* a trade. The two fire at different moments
    against different state:

    * ``DecisionReason`` is computed from the signal -- conviction, expected edge.
      It has no idea how much cash the portfolio has right now.
    * ``OrderRejectionReason`` is computed from live portfolio state -- cash,
      open positions, drawdown, quote freshness. A trade the profile genuinely
      wanted can still be impossible.

    Collapsing them into one enum would lose which stage refused, and "we did not
    want it" and "we could not afford it" are very different findings.
    """

    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    MAX_OPEN_POSITIONS = "MAX_OPEN_POSITIONS"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    MAX_DAILY_LOSS = "MAX_DAILY_LOSS"
    STALE_QUOTE = "STALE_QUOTE"
    INVALID_STOP = "INVALID_STOP"
    EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    QUANTITY_TOO_SMALL = "QUANTITY_TOO_SMALL"
    INSTRUMENT_NOT_TRADABLE = "INSTRUMENT_NOT_TRADABLE"
    PROFILE_DISABLED = "PROFILE_DISABLED"
    SHORT_NOT_SUPPORTED = "SHORT_NOT_SUPPORTED"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"


class PositionStatus(StrEnum):
    """Lifecycle of a virtual position."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class TradeOutcome(StrEnum):
    """Coarse classification of a closed trade, on **net** P&L.

    Classified after costs, not before: a trade that made 3 EUR gross and paid
    4 EUR in fees is a loss, and calling it a win is how a cost-blind system
    convinces itself it is profitable.
    """

    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


class CandleAmbiguityPolicy(StrEnum):
    """How to resolve a candle that touched both stop and target.

    OHLC data cannot say which came first. The simulator must never resolve the
    ambiguity in its own favour, because doing so silently inflates every result
    in a way no summary statistic reveals.

    ``CONSERVATIVE`` (default)
        Assume the **worse** outcome happened first -- stop for a long position.
        A backtest that is pessimistic by an unknown amount is survivable; one
        that is optimistic by an unknown amount is worthless.

    ``OPTIMISTIC``
        Assume the target hit first. Provided only so the *gap* between the two
        can be measured -- it quantifies how much of a result rests on
        unresolvable ambiguity. Never use it to report performance.

    ``INTRABAR_DATA_REQUIRED``
        Refuse to guess: raise rather than resolve. The honest option once
        finer-grained data is available, and a way to find out how often the
        ambiguity actually arises.
    """

    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    INTRABAR_DATA_REQUIRED = "INTRABAR_DATA_REQUIRED"


class LabelStatus(StrEnum):
    """Whether an outcome label could actually be computed.

    A horizon that has not elapsed yet is the common case for recent
    observations, and it is **not** a zero return. Writing 0.0 for "we do not
    know" poisons every statistic computed downstream: it drags means toward
    zero, understates variance, and makes the most recent -- most relevant --
    observations look like the flattest ones. So the absence is a state, not a
    number, and the labelling job is re-runnable precisely so these mature.
    """

    COMPLETE = "COMPLETE"
    """Enough future data existed; the label is real."""
    PENDING = "PENDING"
    """The horizon has not elapsed yet. Will complete on a later run."""
    INSUFFICIENT_FUTURE_DATA = "INSUFFICIENT_FUTURE_DATA"
    """The horizon elapsed but the bars are missing -- a gap, not a wait."""

    @property
    def is_usable(self) -> bool:
        return self is LabelStatus.COMPLETE


class BarrierOutcome(StrEnum):
    """Which of a target/stop pair was reached first.

    ``AMBIGUOUS_SAME_BAR`` is the honest answer whenever one candle's range spans
    both levels: OHLC records the extremes but not their order, so the question
    is unanswerable from the data rather than merely difficult. Recording it as
    its own outcome -- instead of resolving it and moving on -- is what lets a
    later analysis measure how much of a result rests on the guess. See
    :class:`CandleAmbiguityPolicy` for how execution resolves it when it must.
    """

    TARGET_FIRST = "TARGET_FIRST"
    STOP_FIRST = "STOP_FIRST"
    NEITHER = "NEITHER"
    """Neither level was touched before the horizon elapsed."""
    AMBIGUOUS_SAME_BAR = "AMBIGUOUS_SAME_BAR"
    """One bar touched both. Unknowable without intrabar data."""

    @property
    def is_resolved(self) -> bool:
        return self in {BarrierOutcome.TARGET_FIRST, BarrierOutcome.STOP_FIRST}


class SpreadQuality(StrEnum):
    """How much trust a recorded spread deserves.

    Separate from :class:`~app.scanner.enums.DataQuality` on purpose: bar
    staleness and quote sanity are different questions, and the free IEX feed
    answers them differently. A 900 bps spread on a mega-cap is not stale data --
    the bars are fine -- it is a thin book after the close being reported
    faithfully. Classifying it needs the session, not the bar age.

    The raw observation is always preserved; this only tells research queries
    which rows they may believe.
    """

    REGULAR_SESSION = "REGULAR_SESSION"
    """Quoted during regular hours and within a plausible range."""
    EXTENDED_HOURS = "EXTENDED_HOURS"
    """Pre-market or after-hours: real, but not comparable to session spreads."""
    SUSPICIOUS_SPREAD = "SUSPICIOUS_SPREAD"
    """Implausibly wide for the instrument. Preserved, excluded from benchmarks."""
    STALE = "STALE"
    """The quote was too old to describe the market at that instant."""
    MISSING = "MISSING"
    """No quote at all -- the normal case for historical observations."""

    @property
    def is_reliable(self) -> bool:
        """Whether a benchmark may use this spread as an executable cost."""
        return self is SpreadQuality.REGULAR_SESSION


class CostBasis(StrEnum):
    """Where a transaction-cost figure came from.

    Required on every trade outcome because the distinction is invisible in the
    number itself. tradabot stores no historical quotes, so every backtested cost
    is ``MODELLED``; presenting one as if it were measured would be the same
    error as quoting a simulated fill as a real one.
    """

    OBSERVED = "OBSERVED"
    """Derived from a quote recorded at that instant."""
    MODELLED = "MODELLED"
    """Estimated by a versioned cost model. Never call this 'actual'."""
    UNAVAILABLE = "UNAVAILABLE"
    """No basis for an estimate; the cost is unknown rather than zero."""
