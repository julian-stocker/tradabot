"""Exit rules: stops, targets, same-bar ambiguity, gaps and holding periods.

The ambiguity and gap tests are the most important in this file. Both hazards are
one-directional -- getting either wrong always makes results look better than
reality -- so each has an explicit test pinning the pessimistic behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import CandleAmbiguityPolicy, ExitReason, Side
from app.paper.exits import (
    BarPrices,
    IntrabarDataRequiredError,
    derive_stop_and_target,
    evaluate_exit,
    holding_period_expired,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)
STOP = Decimal("95")
TARGET = Decimal("108")


def bar(open_: str, high: str, low: str, close: str, day: int = 0) -> BarPrices:
    return BarPrices(
        timestamp=T0 + timedelta(days=day),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def evaluate(
    b: BarPrices,
    *,
    stop: Decimal | None = STOP,
    target: Decimal | None = TARGET,
    policy: CandleAmbiguityPolicy = CandleAmbiguityPolicy.CONSERVATIVE,
):
    return evaluate_exit(side=Side.LONG, bar=b, stop_loss=stop, take_profit=target, policy=policy)


class TestStopAndTarget:
    def test_stop_triggers_when_the_low_reaches_it(self):
        result = evaluate(bar("100", "102", "94", "96"))
        assert result.triggered
        assert result.reason is ExitReason.STOP_LOSS
        assert result.exit_price == STOP

    def test_target_triggers_when_the_high_reaches_it(self):
        result = evaluate(bar("100", "109", "99", "107"))
        assert result.triggered
        assert result.reason is ExitReason.TAKE_PROFIT
        assert result.exit_price == TARGET

    def test_no_exit_when_the_bar_stays_inside_the_band(self):
        assert not evaluate(bar("100", "104", "97", "103")).triggered

    def test_touching_the_stop_exactly_triggers_it(self):
        """`low == stop` means it traded there."""
        assert evaluate(bar("100", "102", "95", "99")).triggered

    def test_no_levels_means_no_exit(self):
        assert not evaluate(bar("100", "200", "1", "150"), stop=None, target=None).triggered

    def test_stop_only(self):
        result = evaluate(bar("100", "120", "94", "119"), target=None)
        assert result.reason is ExitReason.STOP_LOSS

    def test_target_only(self):
        result = evaluate(bar("100", "109", "80", "107"), stop=None)
        assert result.reason is ExitReason.TAKE_PROFIT


class TestSameBarAmbiguity:
    """Part J. The candle from the specification, exactly."""

    SPEC_BAR = ("100", "110", "90", "105")

    def test_conservative_assumes_the_stop_happened_first(self):
        """The default, and the only safe choice.

        OHLC cannot say which came first. Choosing the profitable one inflates
        every result by an amount no summary statistic reveals.
        """
        result = evaluate(bar(*self.SPEC_BAR))
        assert result.reason is ExitReason.STOP_LOSS
        assert result.exit_price == STOP
        assert result.ambiguous

    def test_conservative_is_the_default_policy(self):
        explicit = evaluate(bar(*self.SPEC_BAR), policy=CandleAmbiguityPolicy.CONSERVATIVE)
        default = evaluate_exit(
            side=Side.LONG, bar=bar(*self.SPEC_BAR), stop_loss=STOP, take_profit=TARGET
        )
        assert default.reason is explicit.reason

    def test_optimistic_assumes_the_target(self):
        """Provided only to quantify how much a result rests on the guess."""
        result = evaluate(bar(*self.SPEC_BAR), policy=CandleAmbiguityPolicy.OPTIMISTIC)
        assert result.reason is ExitReason.TAKE_PROFIT
        assert result.ambiguous

    def test_the_two_policies_disagree_by_the_full_band(self):
        conservative = evaluate(bar(*self.SPEC_BAR))
        optimistic = evaluate(bar(*self.SPEC_BAR), policy=CandleAmbiguityPolicy.OPTIMISTIC)
        assert optimistic.exit_price - conservative.exit_price == TARGET - STOP

    def test_intrabar_policy_refuses_to_guess(self):
        with pytest.raises(IntrabarDataRequiredError, match="both stop"):
            evaluate(bar(*self.SPEC_BAR), policy=CandleAmbiguityPolicy.INTRABAR_DATA_REQUIRED)

    def test_unambiguous_bars_are_not_flagged(self):
        assert not evaluate(bar("100", "102", "94", "96")).ambiguous

    def test_intrabar_policy_allows_unambiguous_bars(self):
        """Refusing to guess must not mean refusing to work."""
        result = evaluate(
            bar("100", "102", "94", "96"), policy=CandleAmbiguityPolicy.INTRABAR_DATA_REQUIRED
        )
        assert result.reason is ExitReason.STOP_LOSS


class TestGaps:
    """Part K. A stop at 100 does not fill at 100 when the market opens at 95."""

    def test_stop_gap_fills_at_the_open_not_the_stop(self):
        result = evaluate(bar("90", "93", "88", "92"))
        assert result.reason is ExitReason.STOP_LOSS
        assert result.exit_price == Decimal("90"), "must fill at the open, worse than the stop"
        assert result.gapped

    def test_stop_gap_is_worse_than_the_stop_price(self):
        result = evaluate(bar("90", "93", "88", "92"))
        assert result.exit_price < STOP

    def test_target_gap_fills_at_the_open_and_is_better(self):
        """Favourable gaps are real and must not be clipped back to the target."""
        result = evaluate(bar("112", "115", "111", "114"))
        assert result.reason is ExitReason.TAKE_PROFIT
        assert result.exit_price == Decimal("112")
        assert result.exit_price > TARGET
        assert result.gapped

    def test_opening_exactly_at_the_stop_is_not_a_gap(self):
        result = evaluate(bar("95", "97", "94", "96"))
        assert result.reason is ExitReason.STOP_LOSS
        assert result.exit_price == STOP
        assert not result.gapped

    def test_gap_through_the_stop_wins_over_an_intrabar_target(self):
        """A bar that opened below the stop was already stopped out.

        Even if it later rallied through the target, the position was gone. This
        is the case where a naive implementation books a winner.
        """
        result = evaluate(bar("90", "115", "89", "114"))
        assert result.reason is ExitReason.STOP_LOSS
        assert result.exit_price == Decimal("90")


class TestHoldingPeriod:
    def test_not_expired_before_the_limit(self):
        assert not holding_period_expired(bars_held=3, max_holding_bars=5)

    def test_expired_at_the_limit(self):
        assert holding_period_expired(bars_held=5, max_holding_bars=5)

    def test_expired_beyond_the_limit(self):
        assert holding_period_expired(bars_held=9, max_holding_bars=5)

    def test_no_limit_never_expires(self):
        assert not holding_period_expired(bars_held=10_000, max_holding_bars=None)


class TestStopDerivation:
    def test_stop_is_placed_atr_multiples_below_entry(self):
        stop, target = derive_stop_and_target(
            entry_price=Decimal(100),
            atr=Decimal(2),
            stop_loss_atr_multiple=Decimal(2),
            take_profit_r_multiple=Decimal(3),
        )
        assert stop == Decimal(96)
        assert target == Decimal(112), "3R on a 4-point risk distance"

    def test_target_is_expressed_in_r_multiples(self):
        stop, target = derive_stop_and_target(
            entry_price=Decimal(100),
            atr=Decimal(1),
            stop_loss_atr_multiple=Decimal(2),
            take_profit_r_multiple=Decimal(2),
        )
        assert stop is not None
        assert target is not None
        risk = Decimal(100) - stop
        assert (target - Decimal(100)) / risk == Decimal(2)

    def test_no_atr_means_no_stop_and_no_invented_distance(self):
        """The load-bearing refusal: a fabricated stop produces an arbitrary size."""
        assert derive_stop_and_target(
            entry_price=Decimal(100),
            atr=None,
            stop_loss_atr_multiple=Decimal(2),
            take_profit_r_multiple=Decimal(2),
        ) == (None, None)

    def test_no_multiple_configured_means_no_stop(self):
        assert derive_stop_and_target(
            entry_price=Decimal(100),
            atr=Decimal(2),
            stop_loss_atr_multiple=None,
            take_profit_r_multiple=Decimal(2),
        ) == (None, None)

    def test_an_atr_wider_than_the_price_is_refused(self):
        """Rather than producing a zero or negative stop."""
        assert derive_stop_and_target(
            entry_price=Decimal(10),
            atr=Decimal(20),
            stop_loss_atr_multiple=Decimal(2),
            take_profit_r_multiple=Decimal(2),
        ) == (None, None)

    def test_target_is_optional(self):
        stop, target = derive_stop_and_target(
            entry_price=Decimal(100),
            atr=Decimal(2),
            stop_loss_atr_multiple=Decimal(2),
            take_profit_r_multiple=None,
        )
        assert stop == Decimal(96)
        assert target is None


class TestShortIsRefusedNotFaked:
    def test_short_exit_raises(self):
        """Long-only is a stated limitation, not a silent approximation."""
        with pytest.raises(NotImplementedError, match="LONG positions only"):
            evaluate_exit(
                side=Side.SHORT,
                bar=bar("100", "110", "90", "105"),
                stop_loss=STOP,
                take_profit=TARGET,
            )
