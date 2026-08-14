"""The chronological portfolio replay must behave like a portfolio.

Phase 11.3's replay walked positions independently and passed
``current_exposure=0``. Everything here exists because that made whole classes of
question unanswerable: what was already open, what cash was actually free, and
whether an exit released capital in time for the next entry.

These tests drive the **real** engine against a real database. Nothing is
asserted about profitability -- signal-v1 is directionally unvalidated and no
test here would be evidence either way.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import OrderRejectionReason
from app.paper.engine import PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.portfolio import value_portfolio
from app.paper.repository import PaperTradingRepository
from app.paper.sizing import ExecutionFractionality
from app.research import phase11_4
from app.research.phase11_3 import Candidate
from app.research.phase11_4 import (
    CostBurden,
    Viability,
    classify_cost_burden,
    classify_viability,
    controlled_profile,
    order_simultaneous,
)
from app.simulation.models import SimulationProfileConfig
from app.simulation.repository import SimulationProfileRepository
from tests.integration.test_paper_lifecycle import make_instrument

T0 = datetime(2025, 3, 3, 15, 0, tzinfo=UTC)


def bar(offset: int, price: str, *, low: str | None = None) -> BarPrices:
    p = Decimal(price)
    return BarPrices(
        timestamp=T0 + timedelta(hours=offset),
        open=p,
        high=p + Decimal("0.5"),
        low=Decimal(low) if low is not None else p - Decimal("0.5"),
        close=p,
    )


async def setup(session, profile: SimulationProfileConfig, **engine_kwargs):
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many([profile])
    await session.flush()
    stored = await profiles.get_profile(profile.name)
    repository = PaperTradingRepository(session)
    portfolio = await repository.ensure_portfolio(stored)
    await session.flush()
    engine = PaperTradingEngine(repository, stored, portfolio, **engine_kwargs)
    return engine, repository, portfolio, stored


async def enter(engine, instrument, *, decision_id, at, price="100", atr="2.0"):
    return await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=decision_id,
        signal_id=None,
        signal_bar_timestamp=at - timedelta(hours=1),
        execution_timestamp=at,
        execution_price=Decimal(price),
        quote=None,
        atr=Decimal(atr),
    )


# ---------------------------------------------------------------------------
# A: one ledger, not two
# ---------------------------------------------------------------------------
def test_the_replay_defines_no_ledger_of_its_own() -> None:
    """**The gate.** Cash, equity and exposure have exactly one owner."""
    names = {
        name
        for name, obj in vars(phase11_4).items()
        if inspect.isfunction(obj) and obj.__module__ == phase11_4.__name__
    }
    for forbidden in ("apply_entry", "apply_exit", "value_portfolio", "size_position"):
        assert forbidden not in names
    source = inspect.getsource(phase11_4)
    assert "portfolio.cash =" not in source
    assert "portfolio.realized_pnl =" not in source


def test_the_replay_places_no_live_orders() -> None:
    source = inspect.getsource(phase11_4).lower()
    for forbidden in ("submit_order", "tradingclient", "alpaca", "place_order"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# B: deterministic ordering at the same timestamp
# ---------------------------------------------------------------------------
def test_simultaneous_candidates_are_ordered_by_score_then_symbol() -> None:
    """Whoever is offered first takes the last slot in a full portfolio."""
    at = T0
    group = [
        Candidate(instrument_id=1, symbol="ZZZ", signal_bar=at, score=80.0),
        Candidate(instrument_id=2, symbol="AAA", signal_bar=at, score=90.0),
        Candidate(instrument_id=3, symbol="BBB", signal_bar=at, score=80.0),
    ]
    assert [c.symbol for c in order_simultaneous(group)] == ["AAA", "BBB", "ZZZ"]


def test_the_ordering_is_stable_under_input_permutation() -> None:
    """Otherwise the experiment silently depends on SQLite's row order."""
    at = T0
    group = [
        Candidate(instrument_id=i, symbol=s, signal_bar=at, score=sc)
        for i, (s, sc) in enumerate([("AAA", 90.0), ("BBB", 80.0), ("CCC", 80.0)])
    ]
    expected = [c.symbol for c in order_simultaneous(group)]
    assert [c.symbol for c in order_simultaneous(list(reversed(group)))] == expected


# ---------------------------------------------------------------------------
# C/D: real exposure and real cash
# ---------------------------------------------------------------------------
class TestPortfolioAccounting:
    async def test_exposure_is_non_zero_once_a_position_is_open(self, session):
        """The defect phase 11.3 carried: exposure was always passed as zero."""
        profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
        engine, repository, portfolio, stored = await setup(session, profile)
        instrument = await make_instrument(session, "AAA")

        result = await enter(engine, instrument, decision_id=1, at=T0)
        assert result.accepted

        valuation = value_portfolio(
            portfolio=portfolio,
            positions=await repository.open_positions(stored.id),
            quotes={},
            marks={},
            timestamp=T0,
        )
        assert valuation.gross_exposure > 0
        assert valuation.equity == valuation.cash + valuation.positions_value

    async def test_cash_falls_by_notional_plus_fee_on_entry(self, session):
        profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
        engine, repository, portfolio, _stored = await setup(session, profile)
        instrument = await make_instrument(session, "AAA")
        before = portfolio.cash

        result = await enter(engine, instrument, decision_id=1, at=T0)
        position = await repository.get_position(result.position_id)
        spent = before - portfolio.cash
        assert spent == pytest.approx(
            position.average_entry_price * position.quantity + position.entry_fee,
            abs=Decimal("0.01"),
        )

    async def test_cash_never_goes_negative_without_leverage(self, session):
        """No profile field permits leverage, so an entry needing it is refused."""
        profile = controlled_profile(Decimal("10000"), Decimal("0.02"))
        engine, _repository, portfolio, _stored = await setup(session, profile)

        for index in range(5):
            instrument = await make_instrument(session, f"S{index}")
            await enter(engine, instrument, decision_id=index + 1, at=T0, price="100")
            assert portfolio.cash >= 0

    async def test_an_entry_requiring_more_cash_than_exists_is_refused(self, session):
        profile = controlled_profile(Decimal("10000"), Decimal("0.02"))
        engine, _repository, portfolio, _stored = await setup(session, profile)
        portfolio.cash = Decimal("0.50")  # below even the order fee

        instrument = await make_instrument(session, "AAA")
        result = await enter(engine, instrument, decision_id=1, at=T0)
        assert not result.accepted
        assert result.rejection is OrderRejectionReason.INSUFFICIENT_CASH
        assert portfolio.cash >= 0

    async def test_an_exit_releases_cash_for_a_later_entry(self, session):
        """The whole reason exits are processed before entries at an instant."""
        profile = controlled_profile(Decimal("10000"), Decimal("0.02"))
        engine, _repository, portfolio, _stored = await setup(session, profile)
        first = await make_instrument(session, "AAA")

        await enter(engine, first, decision_id=1, at=T0, price="100")
        starved = portfolio.cash
        portfolio.cash = Decimal("5")  # not enough for a second entry

        second = await make_instrument(session, "BBB")
        blocked = await enter(engine, second, decision_id=2, at=T0 + timedelta(hours=1))
        assert not blocked.accepted

        # Close the first position; its proceeds return to cash.
        await engine.process_bar(instrument_id=first.id, bar=bar(2, "80", low="80"))
        assert portfolio.cash > Decimal("5")

        allowed = await enter(engine, second, decision_id=3, at=T0 + timedelta(hours=3))
        assert allowed.accepted
        assert starved >= 0


class TestSelfFundingExits:
    """A position must be able to pay for its own exit.

    Phase 11.4 found the engine reaching a state its own schema forbids: a EUR
    100 portfolio ran 52 trades on positions worth a few euros each, paid EUR 104
    in flat fees, and drove cash to -0.24 -- violating ``cash >= 0`` and raising
    an IntegrityError instead of refusing a trade.
    """

    async def test_a_position_worth_less_than_its_exit_fee_is_refused(self, session):
        """**The gate.** Never a trade worth making, at any account size."""
        profile = controlled_profile(Decimal("10000"), Decimal("0.0001"))
        engine, _repository, _portfolio, _stored = await setup(session, profile)
        instrument = await make_instrument(session, "AAA")

        # EUR 1 risk budget over a 60-wide stop sizes to about EUR 1.67 of stock,
        # which cannot cover the EUR 2.00 of flat fees needed to get in and out.
        result = await enter(engine, instrument, decision_id=1, at=T0, atr="30")
        assert not result.accepted
        assert result.rejection is OrderRejectionReason.BELOW_MIN_NOTIONAL
        assert "round-trip cost" in result.detail

    async def test_cash_survives_a_long_run_of_losing_micro_trades(self, session):
        """The exact shape that tripped the constraint: tiny positions, flat fees."""
        profile = controlled_profile(Decimal("100"), Decimal("0.0025"))
        engine, _repository, portfolio, _stored = await setup(session, profile)

        for index in range(25):
            instrument = await make_instrument(session, f"M{index}")
            result = await enter(engine, instrument, decision_id=index + 1, at=T0)
            if result.accepted:
                # Close it at a heavy loss, which is where the exit fee bites.
                await engine.process_bar(
                    instrument_id=instrument.id, bar=bar(index + 1, "20", low="20")
                )
            assert portfolio.cash >= 0, f"cash went negative at trade {index}"

    def test_the_guard_uses_the_canonical_cost_model(self) -> None:
        """Not a second cost estimate living quietly inside the sizer."""
        from app.paper import sizing

        source = inspect.getsource(sizing)
        assert "estimate_round_trip_cost" in source
        assert "order_fee * 2" not in source


# ---------------------------------------------------------------------------
# E: concurrency and exposure caps
# ---------------------------------------------------------------------------
class TestConcurrencyLimits:
    async def test_max_open_positions_is_enforced(self, session):
        profile = controlled_profile(Decimal("100000"), Decimal("0.005"))
        engine, repository, _portfolio, stored = await setup(session, profile)
        limit = stored.risk.max_open_positions

        accepted = 0
        for index in range(limit + 3):
            instrument = await make_instrument(session, f"S{index}")
            result = await enter(engine, instrument, decision_id=index + 1, at=T0)
            if result.accepted:
                accepted += 1
            else:
                assert result.rejection in (
                    OrderRejectionReason.MAX_OPEN_POSITIONS,
                    OrderRejectionReason.MAX_EXPOSURE,
                    OrderRejectionReason.INSUFFICIENT_CASH,
                )
        assert accepted <= limit
        assert len(await repository.open_positions(stored.id)) <= limit

    async def test_total_exposure_never_exceeds_the_profile_limit(self, session):
        profile = controlled_profile(Decimal("10000"), Decimal("0.02"))
        engine, repository, portfolio, stored = await setup(session, profile)

        for index in range(5):
            instrument = await make_instrument(session, f"S{index}")
            await enter(engine, instrument, decision_id=index + 1, at=T0)

        valuation = value_portfolio(
            portfolio=portfolio,
            positions=await repository.open_positions(stored.id),
            quotes={},
            marks={},
            timestamp=T0,
        )
        limit = valuation.equity * stored.risk.max_total_exposure
        assert valuation.gross_exposure <= limit

    async def test_no_position_exceeds_the_per_position_cap(self, session):
        profile = controlled_profile(Decimal("10000"), Decimal("0.02"))
        engine, repository, _portfolio, stored = await setup(session, profile)
        instrument = await make_instrument(session, "AAA")

        result = await enter(engine, instrument, decision_id=1, at=T0)
        position = await repository.get_position(result.position_id)
        notional = position.average_entry_price * position.quantity
        assert notional <= Decimal("10000") * stored.risk.max_position_percent * Decimal("1.01")


# ---------------------------------------------------------------------------
# F/I: profiles and fractionality
# ---------------------------------------------------------------------------
class TestProfilesAndFractionality:
    async def test_no_stored_profile_is_named_for_the_briefed_capitals(self, session):
        """paper-100/1000/10000 do not exist. Recording it so it is not invented."""
        from app.simulation.defaults import build_default_profiles

        names = {p.name for p in build_default_profiles()}
        for absent in ("paper-100", "paper-1000", "paper-10000"):
            assert absent not in names

    async def test_no_profile_carries_a_fractionality_field(self, session):
        """It is an engine argument, not stored configuration. Stated, not assumed."""
        from app.simulation.models import RiskConfig

        fields = set(RiskConfig.model_fields) | set(SimulationProfileConfig.model_fields)
        for absent in ("fractionality", "allow_fractional", "fractional_shares"):
            assert absent not in fields

    async def test_whole_shares_never_exceed_the_fractional_quantity(self, session):
        profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
        frac_engine, frac_repo, _, _frac_stored = await setup(
            session, profile, fractionality=ExecutionFractionality.FRACTIONAL_ALLOWED
        )
        one = await make_instrument(session, "AAA")
        a = await frac_repo.get_position(
            (await enter(frac_engine, one, decision_id=1, at=T0)).position_id
        )

        whole_profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
        whole_profile = whole_profile.model_copy(update={"name": "whole-test"})
        whole_engine, whole_repo, _unused, _also = await setup(
            session, whole_profile, fractionality=ExecutionFractionality.WHOLE_SHARES_ONLY
        )
        two = await make_instrument(session, "BBB")
        b = await whole_repo.get_position(
            (await enter(whole_engine, two, decision_id=2, at=T0)).position_id
        )
        assert b.quantity <= a.quantity
        assert b.quantity == b.quantity.to_integral_value()


# ---------------------------------------------------------------------------
# H: A/B determinism
# ---------------------------------------------------------------------------
class TestDeterminism:
    async def test_the_same_configuration_replays_identically(self, session):
        """An A/B whose arms are not individually reproducible measures nothing."""
        results = []
        for run in range(2):
            profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
            profile = profile.model_copy(update={"name": f"determinism-{run}"})
            engine, repository, portfolio, stored = await setup(session, profile)
            instrument = await make_instrument(session, f"D{run}")
            await enter(engine, instrument, decision_id=run + 1, at=T0)
            await engine.process_bar(instrument_id=instrument.id, bar=bar(1, "101"))
            position = (await repository.open_positions(stored.id))[0]
            results.append((position.quantity, portfolio.cash, position.stop_loss))
        assert results[0] == results[1]

    async def test_disabling_the_risk_layer_changes_the_stop_and_the_size(self, session):
        """If A and B were identical the comparison would be vacuous."""
        outcomes = {}
        for enabled in (False, True):
            profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
            profile = profile.model_copy(update={"name": f"ab-{enabled}"})
            engine, repository, _portfolio, _stored = await setup(
                session, profile, risk_layer_enabled=enabled
            )
            instrument = await make_instrument(session, f"AB{int(enabled)}")
            from app.market_data.risk import assess
            from app.market_data.volatility import ExpectedMovement, VolatilityRegime

            risk = assess(
                ExpectedMovement(
                    symbol="AB",
                    calculated_at=T0,
                    bar_timestamp=T0,
                    regime=VolatilityRegime.NORMAL,
                    percentile=0.5,
                    atr_pct=2.0,
                    recent_range_pct=4.0,
                ),
                now=T0,
            )
            result = await engine.open_from_decision(
                instrument=instrument,
                # Distinct per arm: ``idempotency_key`` is globally unique, not
                # scoped per profile, so reusing decision id 1 would hand the
                # second arm the first arm's order and compare a run to itself.
                trade_decision_id=100 + int(enabled),
                signal_id=None,
                signal_bar_timestamp=T0 - timedelta(hours=1),
                execution_timestamp=T0,
                execution_price=Decimal("100"),
                quote=None,
                atr=Decimal("2.0"),
                risk=risk,
            )
            position = await repository.get_position(result.position_id)
            outcomes[enabled] = (position.quantity, position.stop_loss)

        assert outcomes[True][1] < outcomes[False][1]  # risk-v1 stop is wider
        assert outcomes[True][0] < outcomes[False][0]  # therefore smaller


# ---------------------------------------------------------------------------
# P: the equity curve is chronological, not a sum of trade returns
# ---------------------------------------------------------------------------
class TestEquityCurve:
    async def test_equity_moves_while_a_position_is_merely_open(self, session):
        """A sum of closed-trade returns cannot see this, and would understate
        drawdown by exactly the part that hurts most."""
        profile = controlled_profile(Decimal("10000"), Decimal("0.01"))
        engine, repository, portfolio, stored = await setup(session, profile)
        instrument = await make_instrument(session, "AAA")
        await enter(engine, instrument, decision_id=1, at=T0)

        def equity_now(mark: str) -> Decimal:
            return value_portfolio(
                portfolio=portfolio,
                positions=positions,
                quotes={},
                marks={instrument.id: Decimal(mark)},
                timestamp=T0,
            ).equity

        positions = await repository.open_positions(stored.id)
        assert equity_now("95") < equity_now("100") < equity_now("105")
        assert portfolio.realized_pnl == 0  # nothing closed


# ---------------------------------------------------------------------------
# Q: the reserve stays inactive
# ---------------------------------------------------------------------------
def test_no_reserve_concept_is_active_anywhere() -> None:
    """**The gate.** Phase 11.3 documented the shape; 11.4 must not build it."""
    source = inspect.getsource(phase11_4)
    for forbidden in ("LOCKED_RESERVE", "RESERVE_PAYABLE", "ACTIVE_CAPITAL"):
        assert forbidden not in source


async def test_the_portfolio_row_has_no_reserve_column(session) -> None:
    """A reserve would have to be added later; nothing may quietly hold one now."""
    from app.db.models import VirtualPortfolio

    columns = set(VirtualPortfolio.__table__.columns.keys())
    for forbidden in ("locked_reserve", "reserve_payable", "active_capital"):
        assert forbidden not in columns


# ---------------------------------------------------------------------------
# J: the classification was fixed in advance
# ---------------------------------------------------------------------------
class TestPreRegisteredClassification:
    def test_the_cost_thresholds_are_the_registered_ones(self) -> None:
        assert Decimal("0.05") == phase11_4.COST_BURDEN_LOW
        assert Decimal("0.15") == phase11_4.COST_BURDEN_ACCEPTABLE
        assert Decimal("0.40") == phase11_4.COST_BURDEN_HIGH
        assert phase11_4.MIN_ENTRIES_FOR_PRACTICAL == 30

    def test_each_burden_band_is_reachable(self) -> None:
        cap = Decimal("1000")
        big = Decimal("100000")
        assert (
            classify_cost_burden(
                execution_cost=Decimal("10"), starting_capital=cap, gross_result=big
            )
            is CostBurden.LOW
        )
        assert (
            classify_cost_burden(
                execution_cost=Decimal("100"), starting_capital=cap, gross_result=big
            )
            is CostBurden.ACCEPTABLE
        )
        assert (
            classify_cost_burden(
                execution_cost=Decimal("300"), starting_capital=cap, gross_result=big
            )
            is CostBurden.HIGH
        )
        assert (
            classify_cost_burden(
                execution_cost=Decimal("500"), starting_capital=cap, gross_result=big
            )
            is CostBurden.UNVIABLE
        )

    def test_cost_exceeding_the_gross_result_is_unviable_whatever_the_ratio(self) -> None:
        """A cheap-looking burden that still eats the whole trading result."""
        assert (
            classify_cost_burden(
                execution_cost=Decimal("10"),
                starting_capital=Decimal("10000"),
                gross_result=Decimal("5"),
            )
            is CostBurden.UNVIABLE
        )

    def test_an_account_that_never_trades_is_not_low_cost(self) -> None:
        """**The gate.** Zero cost through zero participation is not a success."""
        assert (
            classify_viability(burden=CostBurden.LOW, entries=0, entries_any_budget=0)
            is Viability.UNVIABLE
        )

    def test_few_entries_cap_the_verdict_at_limited(self) -> None:
        assert (
            classify_viability(burden=CostBurden.LOW, entries=5, entries_any_budget=5)
            is Viability.LIMITED
        )

    def test_a_healthy_account_can_reach_practical(self) -> None:
        assert (
            classify_viability(burden=CostBurden.ACCEPTABLE, entries=120, entries_any_budget=120)
            is Viability.PRACTICAL
        )


# ---------------------------------------------------------------------------
# S: production safety
# ---------------------------------------------------------------------------
def test_the_replay_never_writes_to_the_production_database() -> None:
    """It opens production read-only and writes only to a temporary file."""
    source = inspect.getsource(phase11_4)
    assert "tradabot.db" not in source
