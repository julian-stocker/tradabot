"""Exit evaluation against a candle.

Pure functions. Given an open position and one candle, decide whether the
position closed during that bar, why, and at what price.

Two hazards dominate this module, and both make results *better* than reality
when handled naively:

**Same-bar ambiguity (Part J).** A candle with ``high=110, low=90`` that contains
both a stop at 95 and a target at 108 does not say which was touched first. OHLC
is four numbers; the path between them is lost. A simulator that resolves this in
its own favour inflates every result by an amount no summary statistic reveals.
The default policy is therefore CONSERVATIVE: assume the stop.

**Gaps (Part K).** A stop at 100 does not fill at 100 when the market opens at 95.
There was no trade at 100 for it to fill against. Filling at the stop price would
manufacture 5 points of liquidity that did not exist -- and gaps cluster precisely
where losses are largest, so this error is worst exactly when it matters most.

Both hazards are one-directional: getting them wrong always flatters the result.
That asymmetry is why the defaults here are pessimistic rather than "neutral".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.errors import TradabotError
from app.domain.enums import CandleAmbiguityPolicy, ExitReason, Side


class IntrabarDataRequiredError(TradabotError):
    """A bar touched both stop and target and the policy refuses to guess.

    Raised only under :attr:`CandleAmbiguityPolicy.INTRABAR_DATA_REQUIRED`, whose
    entire purpose is to surface how often the ambiguity actually arises rather
    than silently resolving it.
    """

    def __init__(self, timestamp: datetime, stop: Decimal, target: Decimal) -> None:
        self.timestamp = timestamp
        super().__init__(
            f"candle at {timestamp.isoformat()} touched both stop ({stop}) and "
            f"target ({target}); INTRABAR_DATA_REQUIRED refuses to guess the order"
        )


@dataclass(frozen=True, slots=True)
class BarPrices:
    """The OHLC of one candle, plus its timestamp.

    A local value type rather than the ORM row, so exit logic stays a pure
    function of numbers and can be tested without a database.
    """

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class ExitEvaluation:
    """Whether and how a position closed during a bar."""

    triggered: bool
    reason: ExitReason | None = None
    exit_price: Decimal | None = None
    gapped: bool = False
    """True when the exit filled at the open because the market gapped through
    the level -- the fill is worse (stop) or better (target) than the level."""
    ambiguous: bool = False
    """True when both stop and target were touched in the same bar and the policy
    resolved it. Persisted so results resting on ambiguity are identifiable."""

    @staticmethod
    def none() -> ExitEvaluation:
        return ExitEvaluation(triggered=False)


def evaluate_exit(  # noqa: PLR0911 -- one return per exit case; merging them would hide the cases
    *,
    side: Side,
    bar: BarPrices,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    policy: CandleAmbiguityPolicy = CandleAmbiguityPolicy.CONSERVATIVE,
) -> ExitEvaluation:
    """Decide whether ``bar`` closed a position via its stop or target.

    Evaluation order matters and is deliberate:

    1. **Gaps at the open.** If the bar *opened* beyond a level, that level was
       already breached before any intrabar movement. The fill is the open.
    2. **Intrabar touches.** Otherwise, check whether the bar's range reached
       either level.
    3. **Ambiguity.** If both were reached intrabar, apply ``policy``.

    Only LONG is implemented; ``SHORT`` raises rather than returning a plausible
    but unverified answer.

    Returns:
        An :class:`ExitEvaluation`. ``triggered=False`` means the position
        survived the bar.
    """
    if side is not Side.LONG:
        msg = (
            "exit evaluation is implemented for LONG positions only; short support "
            "requires its own tested rules and must not be inferred by symmetry"
        )
        raise NotImplementedError(msg)

    if stop_loss is None and take_profit is None:
        return ExitEvaluation.none()

    # --- 1. Gaps: the level was breached before the bar even traded ---------
    if stop_loss is not None and bar.open <= stop_loss:
        # Opened at or below the stop. The first available price is the open, and
        # it is worse than the stop. Filling at `stop_loss` would invent liquidity.
        return ExitEvaluation(
            triggered=True,
            reason=ExitReason.STOP_LOSS,
            exit_price=bar.open,
            gapped=bar.open < stop_loss,
        )

    if take_profit is not None and bar.open >= take_profit:
        # Opened at or above the target: filled at the open, which is *better*
        # than the target. Favourable gaps are real and must not be clipped back
        # to the target price -- that would be pessimism for its own sake.
        return ExitEvaluation(
            triggered=True,
            reason=ExitReason.TAKE_PROFIT,
            exit_price=bar.open,
            gapped=bar.open > take_profit,
        )

    # --- 2. Intrabar touches -----------------------------------------------
    hit_stop = stop_loss is not None and bar.low <= stop_loss
    hit_target = take_profit is not None and bar.high >= take_profit

    if hit_stop and hit_target:
        return _resolve_ambiguity(bar, stop_loss, take_profit, policy)  # type: ignore[arg-type]

    if hit_stop:
        return ExitEvaluation(triggered=True, reason=ExitReason.STOP_LOSS, exit_price=stop_loss)

    if hit_target:
        return ExitEvaluation(triggered=True, reason=ExitReason.TAKE_PROFIT, exit_price=take_profit)

    return ExitEvaluation.none()


def _resolve_ambiguity(
    bar: BarPrices,
    stop_loss: Decimal,
    take_profit: Decimal,
    policy: CandleAmbiguityPolicy,
) -> ExitEvaluation:
    """Both levels were touched in one bar. Decide, without cheating."""
    if policy is CandleAmbiguityPolicy.INTRABAR_DATA_REQUIRED:
        raise IntrabarDataRequiredError(bar.timestamp, stop_loss, take_profit)

    if policy is CandleAmbiguityPolicy.OPTIMISTIC:
        # Provided only to measure how much of a result depends on the guess.
        return ExitEvaluation(
            triggered=True,
            reason=ExitReason.TAKE_PROFIT,
            exit_price=take_profit,
            ambiguous=True,
        )

    # CONSERVATIVE: assume the worse outcome happened first.
    return ExitEvaluation(
        triggered=True, reason=ExitReason.STOP_LOSS, exit_price=stop_loss, ambiguous=True
    )


def holding_period_expired(*, bars_held: int, max_holding_bars: int | None) -> bool:
    """Whether a position has been open longer than its configured limit.

    **Counted in bars processed, not calendar days.** tradabot has no exchange
    calendar yet, so "5 trading days" cannot be computed from timestamps -- a
    weekend or holiday would silently shorten the holding period. Counting bars is
    exact for the data actually seen and degrades gracefully: with daily bars,
    ``max_holding_bars=5`` is five *trading* days, which is what was meant.

    This is the single place the approximation lives. A real calendar replaces
    this function and nothing else.
    """
    if max_holding_bars is None:
        return False
    return bars_held >= max_holding_bars


def derive_stop_and_target(
    *,
    entry_price: Decimal,
    atr: Decimal | None,
    stop_loss_atr_multiple: Decimal | None,
    take_profit_r_multiple: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Derive stop and target levels for a new long position.

    The stop is placed ``stop_loss_atr_multiple`` ATRs below entry, so it scales
    with the instrument's own volatility instead of using a fixed percentage that
    would be far too tight on a volatile name and far too wide on a quiet one.

    The target is expressed in **R multiples** -- multiples of the risk distance --
    rather than as an independent price. That makes the reward:risk ratio an
    explicit configured number rather than an accident of two unrelated settings.

    Returns ``(None, None)`` when no ATR is available. It deliberately does **not**
    fall back to an invented distance: a stop is the sizing denominator, and
    guessing it would silently produce arbitrary position sizes. The caller
    decides what to do about that (see ``app.paper.sizing``).
    """
    if atr is None or atr <= 0 or stop_loss_atr_multiple is None:
        return None, None

    risk_distance = atr * stop_loss_atr_multiple
    stop = entry_price - risk_distance
    if stop <= 0:
        # An ATR wider than the price itself. Refuse rather than produce a
        # negative or zero stop.
        return None, None

    target = (
        entry_price + risk_distance * take_profit_r_multiple
        if take_profit_r_multiple is not None and take_profit_r_multiple > 0
        else None
    )
    return stop, target
