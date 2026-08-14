"""The risk layer, wired into the engine, against a real database.

The unit tests pin what the gate decides. These pin the thing that can only go
wrong once it is connected: that enabling the layer changes *only* what it is
supposed to change, that the canonical sizer is still the only thing computing a
quantity, and that nothing in the layer can close a position.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.enums import ExitReason, OrderRejectionReason, PositionStatus
from app.market_data.risk import assess
from app.market_data.volatility import ExpectedMovement, VolatilityRegime
from app.paper.engine import PaperTradingEngine
from app.paper.exits import BarPrices
from app.paper.repository import PaperTradingRepository
from app.paper.risk_gate import RiskFlag
from app.paper.sizing import ExecutionFractionality
from app.simulation.defaults import build_default_profiles
from app.simulation.repository import SimulationProfileRepository
from tests.integration.test_paper_lifecycle import EXEC_AT, T0, make_instrument, quote

NOW = EXEC_AT + timedelta(hours=1)


def risk_at(*, atr_pct: float = 2.0, regime: VolatilityRegime = VolatilityRegime.NORMAL):
    return assess(
        ExpectedMovement(
            symbol="TEST",
            calculated_at=EXEC_AT,
            bar_timestamp=EXEC_AT,
            regime=regime,
            percentile=0.5,
            atr_pct=atr_pct,
            recent_range_pct=atr_pct * 2,
        ),
        now=EXEC_AT + timedelta(minutes=1),
    )


async def build_engine(
    session,
    *,
    name: str = "5000eur-balanced",
    risk_layer_enabled: bool = False,
    fractionality: ExecutionFractionality = ExecutionFractionality.FRACTIONAL_ALLOWED,
) -> PaperTradingEngine:
    profiles = SimulationProfileRepository(session)
    await profiles.upsert_many(build_default_profiles())
    await session.flush()
    repository = PaperTradingRepository(session)
    profile = await profiles.get_profile(name)
    portfolio = await repository.ensure_portfolio(profile)
    return PaperTradingEngine(
        repository,
        profile,
        portfolio,
        risk_layer_enabled=risk_layer_enabled,
        fractionality=fractionality,
    )


async def open_one(engine, instrument, *, decision_id: int = 1, risk=None, atr="2.00"):
    return await engine.open_from_decision(
        instrument=instrument,
        trade_decision_id=decision_id,
        signal_id=None,
        signal_bar_timestamp=T0,
        execution_timestamp=EXEC_AT,
        execution_price=Decimal("100.00"),
        quote=quote(),
        atr=Decimal(atr) if atr is not None else None,
        risk=risk,
    )


# ---------------------------------------------------------------------------
# A / S: the layer is additive, and off by default
# ---------------------------------------------------------------------------
class TestBaselineIsPreserved:
    async def test_the_layer_is_off_unless_asked_for(self, session):
        """**The gate.** A default of True would silently rewrite every result."""
        engine = await build_engine(session)
        assert engine._risk_layer_enabled is False

    async def test_a_disabled_layer_ignores_the_risk_estimate_entirely(self, session):
        """Passing a risk object must not change a baseline run."""
        instrument = await make_instrument(session)
        baseline = await build_engine(session)
        result = await open_one(baseline, instrument, risk=risk_at(atr_pct=50.0))
        assert result.accepted

        position = await baseline._repository.get_position(result.position_id)
        assert position.risk_distance is None
        assert position.risk_regime is None

    async def test_a_disabled_layer_still_records_the_fractionality_mode(self, session):
        """It changed the quantity either way; an unlabelled table cannot be read."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, fractionality=ExecutionFractionality.WHOLE_SHARES_ONLY)
        result = await open_one(engine, instrument)
        position = await engine._repository.get_position(result.position_id)
        assert position.execution_fractionality == "WHOLE_SHARES_ONLY"


# ---------------------------------------------------------------------------
# A / D: the gate runs at the seam, and what it saw is persisted
# ---------------------------------------------------------------------------
class TestTheGateIsWired:
    async def test_an_enabled_layer_persists_what_it_saw(self, session):
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        assert result.accepted

        position = await engine._repository.get_position(result.position_id)
        assert position.risk_regime == "NORMAL_VOL"
        assert position.risk_distance is not None
        assert position.risk_noise_floor is not None
        assert position.risk_structural_distance is not None
        assert position.risk_floor_bound is not None
        assert position.risk_estimated_cost is not None
        assert position.risk_model_version is not None

    async def test_a_missing_estimate_refuses_with_its_own_reason(self, session):
        """Distinct from RISK_LIMIT: a data outage is not a risk decision."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=None)
        assert not result.accepted
        assert result.rejection is OrderRejectionReason.RISK_DATA_UNAVAILABLE

    async def test_a_sub_noise_stop_is_widened_and_the_widening_is_recorded(self, session):
        """A tiny ATR produces a stop inside ordinary noise. It must not survive."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at(atr_pct=4.0), atr="0.01")
        assert result.accepted

        position = await engine._repository.get_position(result.position_id)
        assert position.risk_floor_bound is True
        assert position.risk_distance == position.risk_noise_floor
        assert position.risk_distance > position.risk_structural_distance
        # The widened stop is the one the position actually carries.
        assert position.stop_loss == pytest.approx(
            Decimal("100.05") - position.risk_distance, abs=Decimal("0.01")
        )

    async def test_widening_the_stop_reduces_the_quantity(self, session):
        """The floor must flow into sizing, not merely be recorded beside it."""
        plain = await make_instrument(session, "PLAIN")
        floored = await make_instrument(session, "FLOOR")
        loose = await build_engine(session, risk_layer_enabled=False)
        tight = await build_engine(session, risk_layer_enabled=True)

        without = await open_one(loose, plain, decision_id=1, atr="0.01")
        with_layer = await open_one(
            tight, floored, decision_id=2, atr="0.01", risk=risk_at(atr_pct=4.0)
        )
        a = await loose._repository.get_position(without.position_id)
        b = await tight._repository.get_position(with_layer.position_id)
        assert b.quantity < a.quantity

    async def test_the_gate_never_computes_the_quantity(self, session):
        """Sizing's caps must still bind. A €5000 profile cannot buy €50k of stock."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        position = await engine._repository.get_position(result.position_id)
        assert position.quantity * position.average_entry_price <= Decimal("5000")


# ---------------------------------------------------------------------------
# C: fractionality rounds down, never up
# ---------------------------------------------------------------------------
class TestFractionality:
    async def test_whole_share_mode_yields_an_integer_quantity(self, session):
        instrument = await make_instrument(session)
        engine = await build_engine(session, fractionality=ExecutionFractionality.WHOLE_SHARES_ONLY)
        result = await open_one(engine, instrument)
        position = await engine._repository.get_position(result.position_id)
        assert position.quantity == position.quantity.to_integral_value()

    async def test_whole_shares_never_exceed_the_fractional_size(self, session):
        """**The gate.** Rounding up breaches whichever cap was binding."""
        one = await make_instrument(session, "FRAC")
        two = await make_instrument(session, "WHOLE")
        fractional = await build_engine(session)
        whole = await build_engine(session, fractionality=ExecutionFractionality.WHOLE_SHARES_ONLY)
        a = await fractional._repository.get_position(
            (await open_one(fractional, one, decision_id=1)).position_id
        )
        b = await whole._repository.get_position(
            (await open_one(whole, two, decision_id=2)).position_id
        )
        assert b.quantity <= a.quantity

    async def test_an_unaffordable_whole_share_is_refused_not_rounded_up(self, session):
        """A €100 account facing an expensive instrument gets zero, not 0.4 shares."""
        instrument = await make_instrument(session)
        engine = await build_engine(
            session,
            name="50eur-conservative",
            fractionality=ExecutionFractionality.WHOLE_SHARES_ONLY,
        )
        result = await engine.open_from_decision(
            instrument=instrument,
            trade_decision_id=1,
            signal_id=None,
            signal_bar_timestamp=T0,
            execution_timestamp=EXEC_AT,
            execution_price=Decimal("100.00"),
            quote=quote(),
            atr=Decimal("2.00"),
        )
        assert not result.accepted
        assert result.rejection is OrderRejectionReason.QUANTITY_TOO_SMALL
        assert "rounds down to zero" in result.detail


# ---------------------------------------------------------------------------
# E: gap-through is measured, not assumed
# ---------------------------------------------------------------------------
class TestGapThrough:
    async def test_a_gap_below_the_stop_fills_at_the_open_and_records_the_excess(self, session):
        """The stop is a request, not a guarantee. The shortfall is the finding."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        position = await engine._repository.get_position(result.position_id)
        stop = position.stop_loss
        assert stop is not None

        # Opens far below the stop -- no path from the previous close to here.
        gapped_open = stop - Decimal("5")
        await engine.process_bar(
            instrument_id=instrument.id,
            bar=BarPrices(
                timestamp=EXEC_AT + timedelta(days=1),
                open=gapped_open,
                high=gapped_open + Decimal("0.5"),
                low=gapped_open - Decimal("1"),
                close=gapped_open,
            ),
        )
        await session.flush()
        await session.refresh(position)
        assert position.status is PositionStatus.CLOSED
        assert position.exit_reason is ExitReason.STOP_LOSS
        assert position.exit_was_gap
        assert position.exit_price < stop
        assert position.stop_excess_loss > 0

    async def test_a_stop_that_held_records_zero_excess_not_null(self, session):
        """A table containing only breaches cannot tell you the breach *rate*."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        position = await engine._repository.get_position(result.position_id)
        stop = position.stop_loss

        # Trades down through the stop intrabar, opening above it.
        await engine.process_bar(
            instrument_id=instrument.id,
            bar=BarPrices(
                timestamp=EXEC_AT + timedelta(days=1),
                open=stop + Decimal("1"),
                high=stop + Decimal("1.5"),
                low=stop - Decimal("0.5"),
                close=stop - Decimal("0.2"),
            ),
        )
        await session.flush()
        await session.refresh(position)
        assert position.exit_reason is ExitReason.STOP_LOSS
        assert not position.exit_was_gap
        assert position.stop_excess_loss == 0


# ---------------------------------------------------------------------------
# G: the rolling recompute flags, and cannot close
# ---------------------------------------------------------------------------
class TestRollingFlags:
    async def test_a_rising_band_flags_the_position_without_closing_it(self, session):
        """**The gate.** Risk doubling is information, not an instruction."""
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at(atr_pct=2.0))
        position = await engine._repository.get_position(result.position_id)

        flags = await engine.refresh_risk_flags(
            risks={instrument.id: risk_at(atr_pct=6.0)}, now=NOW
        )
        assert flags[position.id] is RiskFlag.INCREASED

        await session.flush()
        await session.refresh(position)
        assert position.risk_flag == "RISK_INCREASED"
        assert position.risk_flag_updated_at == NOW
        assert position.status is PositionStatus.OPEN

    async def test_an_extreme_regime_flags_and_still_does_not_close(self, session):
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        position = await engine._repository.get_position(result.position_id)

        await engine.refresh_risk_flags(
            risks={instrument.id: risk_at(atr_pct=9.0, regime=VolatilityRegime.EXTREME)},
            now=NOW,
        )
        await session.flush()
        await session.refresh(position)
        assert position.risk_flag == "RISK_EXTREME"
        assert position.status is PositionStatus.OPEN

    async def test_a_missing_recompute_is_flagged_stale_not_left_looking_fresh(self, session):
        instrument = await make_instrument(session)
        engine = await build_engine(session, risk_layer_enabled=True)
        result = await open_one(engine, instrument, risk=risk_at())
        position = await engine._repository.get_position(result.position_id)

        await engine.refresh_risk_flags(risks={}, now=NOW)
        await session.flush()
        await session.refresh(position)
        assert position.risk_flag == "RISK_DATA_STALE"
        assert position.status is PositionStatus.OPEN
