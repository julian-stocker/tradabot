"""Production match-b-v1 must decide exactly what research decided.

If production drifts from the frozen record by even one threshold, the forward
experiment stops testing the hypothesis it was registered against — and it would
do so silently, producing a perfectly plausible stream of candidates. These tests
exist to make that drift impossible to introduce quietly.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import polars as pl
import pytest

from app.research.phase12_1 import (
    MATCH_B_DEFINITION,
    MATCH_B_MOVEMENT_FLOOR,
    MATCH_B_RANK_FLOOR,
    match_b_mask,
)
from app.strategy import match_b
from app.strategy.match_b import (
    HORIZON_SESSIONS,
    STRATEGY_VERSION,
    UNIVERSE_VERSION,
    EvaluationOutcome,
    candidate_identity,
    describe_frozen_rule,
    evaluate_session,
    universe_hash,
)

SESSION = datetime(2026, 8, 7, tzinfo=UTC)
DECIDED = datetime(2026, 8, 7, 21, tzinfo=UTC)


def panel(rows: list[dict[str, object]], session: datetime = SESSION) -> pl.DataFrame:
    return pl.DataFrame([{"timestamp": session, **row} for row in rows])


def row(symbol="AAA", rank=0.95, sector_ret=1.0, movement=9.0, sector="technology"):
    return {
        "symbol": symbol,
        "xs_rank_ret_20d": rank,
        "sector_etf_ret_20d": sector_ret,
        "movement_to_cost": movement,
        "atr_pct": 2.5,
        "close": 100.0,
        "sector": sector,
    }


# ---------------------------------------------------------------------------
# Equivalence with the frozen record
# ---------------------------------------------------------------------------
class TestEquivalence:
    def test_production_reuses_the_frozen_predicate_rather_than_restating_it(self) -> None:
        """**The gate.** A second copy of the rule is how the two drift apart."""
        # Scanned below the module docstring, which legitimately quotes the
        # thresholds while explaining why they are not declared here.
        source = inspect.getsource(match_b).split('"""', 2)[-1]
        assert "match_b_mask" in source
        assert "0.90" not in source
        assert "8.0" not in source
        assert ">= 0.9" not in source

    def test_the_version_and_horizon_come_from_the_frozen_record(self) -> None:
        assert STRATEGY_VERSION == MATCH_B_DEFINITION["version"] == "match-b-v1"
        assert HORIZON_SESSIONS == MATCH_B_DEFINITION["horizon_sessions"] == 3

    def test_the_described_rule_matches_the_executed_predicate(self) -> None:
        described = describe_frozen_rule()
        assert described["rank_floor"] == MATCH_B_RANK_FLOOR
        assert described["movement_floor"] == MATCH_B_MOVEMENT_FLOOR
        assert described["sector_condition"] == MATCH_B_DEFINITION["sector_positive"]

    def test_production_selects_exactly_what_the_research_mask_selects(self) -> None:
        """The equivalence proof, on a frame spanning every boundary."""
        frame = panel(
            [
                row("PASS", rank=0.95, sector_ret=1.0, movement=9.0),
                row("EDGE", rank=0.90, sector_ret=0.1, movement=8.0),
                row("LOWRANK", rank=0.89, sector_ret=1.0, movement=9.0),
                row("SECTORNEG", rank=0.95, sector_ret=-0.1, movement=9.0),
                row("SECTORFLAT", rank=0.95, sector_ret=0.0, movement=9.0),
                row("LOWMOVE", rank=0.95, sector_ret=1.0, movement=7.99),
            ]
        )
        research = set(frame.filter(match_b_mask())["symbol"].to_list())
        production = {
            c.symbol
            for c in evaluate_session(frame, session=SESSION, decided_at=DECIDED).candidates
        }
        assert production == research == {"PASS", "EDGE"}

    def test_every_boundary_is_inclusive_or_exclusive_as_registered(self) -> None:
        """Rank and movement are >=; sector is strictly >."""
        assert (
            evaluate_session(
                panel([row(rank=0.90)]), session=SESSION, decided_at=DECIDED
            ).candidate_count
            == 1
        )
        assert (
            evaluate_session(
                panel([row(rank=0.8999)]), session=SESSION, decided_at=DECIDED
            ).candidate_count
            == 0
        )
        assert (
            evaluate_session(
                panel([row(movement=8.0)]), session=SESSION, decided_at=DECIDED
            ).candidate_count
            == 1
        )
        assert (
            evaluate_session(
                panel([row(sector_ret=0.0)]), session=SESSION, decided_at=DECIDED
            ).candidate_count
            == 0
        )


# ---------------------------------------------------------------------------
# Outcomes are distinguishable
# ---------------------------------------------------------------------------
class TestOutcomes:
    def test_zero_candidates_is_no_opportunity_not_a_failure(self) -> None:
        """**The gate.** A quiet day and an outage must never look alike."""
        result = evaluate_session(panel([row(rank=0.1)]), session=SESSION, decided_at=DECIDED)
        assert result.outcome is EvaluationOutcome.NO_OPPORTUNITY
        assert result.error is None
        assert result.eligible_symbols == 1

    def test_an_absent_session_is_a_failure_not_no_opportunity(self) -> None:
        result = evaluate_session(
            panel([row()]), session=datetime(2020, 1, 1, tzinfo=UTC), decided_at=DECIDED
        )
        assert result.outcome is EvaluationOutcome.FAILED
        assert result.error is not None

    def test_candidates_found_reports_its_own_outcome(self) -> None:
        result = evaluate_session(panel([row()]), session=SESSION, decided_at=DECIDED)
        assert result.outcome is EvaluationOutcome.CANDIDATES

    def test_not_evaluated_exists_as_a_distinct_state(self) -> None:
        assert EvaluationOutcome.NOT_EVALUATED not in {
            EvaluationOutcome.NO_OPPORTUNITY,
            EvaluationOutcome.FAILED,
        }


# ---------------------------------------------------------------------------
# Identity and idempotency
# ---------------------------------------------------------------------------
class TestIdentity:
    def test_the_same_session_yields_the_same_candidate_id(self) -> None:
        """**The gate.** Random ids would make every restart a new trade."""
        first = evaluate_session(panel([row()]), session=SESSION, decided_at=DECIDED)
        later = evaluate_session(
            panel([row()]), session=SESSION, decided_at=datetime(2026, 8, 9, tzinfo=UTC)
        )
        assert first.candidates[0].candidate_id == later.candidates[0].candidate_id

    def test_different_sessions_or_symbols_never_collide(self) -> None:
        ids = {
            candidate_identity(
                session=datetime(2026, 8, day, tzinfo=UTC),
                symbol=symbol,
                strategy_version=STRATEGY_VERSION,
                universe_version=UNIVERSE_VERSION,
            )
            for day in (5, 6, 7)
            for symbol in ("AAA", "BBB")
        }
        assert len(ids) == 6

    def test_a_universe_change_changes_the_identity(self) -> None:
        base = candidate_identity(
            session=SESSION,
            symbol="AAA",
            strategy_version=STRATEGY_VERSION,
            universe_version=UNIVERSE_VERSION,
        )
        other = candidate_identity(
            session=SESSION,
            symbol="AAA",
            strategy_version=STRATEGY_VERSION,
            universe_version="expanded-990",
        )
        assert base != other

    def test_the_universe_hash_is_order_independent_and_change_sensitive(self) -> None:
        assert universe_hash(("A", "B", "C")) == universe_hash(("C", "A", "B"))
        assert universe_hash(("A", "B")) != universe_hash(("A", "B", "C"))


# ---------------------------------------------------------------------------
# The universe, and what the candidate may carry
# ---------------------------------------------------------------------------
class TestUniverseAndProvenance:
    def test_the_forward_experiment_uses_the_original_fifty_two(self) -> None:
        """Phase 12.1 measured generalisation FAILING on the broad universe."""
        assert UNIVERSE_VERSION == "original-52-v1"
        assert "expanded" not in UNIVERSE_VERSION

    def test_a_candidate_carries_no_direction_or_conviction(self) -> None:
        """**The gate.** match-b-v1 selects; it does not predict a price."""
        fields = set(match_b.MatchBCandidate.__dataclass_fields__)
        for forbidden in (
            "direction",
            "target",
            "confidence",
            "score",
            "expected_return",
            "signal",
        ):
            assert forbidden not in fields

    def test_a_candidate_reconstructs_its_own_decision(self) -> None:
        candidate = evaluate_session(
            panel([row()]), session=SESSION, decided_at=DECIDED
        ).candidates[0]
        assert candidate.strategy_version == "match-b-v1"
        assert candidate.universe_version == UNIVERSE_VERSION
        assert candidate.data_cutoff == SESSION
        assert candidate.horizon_sessions == 3
        assert candidate.reference_price == Decimal("100.0")

    def test_candidates_are_ranked_highest_first(self) -> None:
        frame = panel([row("LOW", rank=0.91), row("HIGH", rank=0.99), row("MID", rank=0.95)])
        result = evaluate_session(frame, session=SESSION, decided_at=DECIDED)
        assert [c.symbol for c in result.candidates] == ["HIGH", "MID", "LOW"]


# ---------------------------------------------------------------------------
# Isolation from everything it must not touch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    ["implied_volatility", "option_surface", "iv_30d", "submit_order", "webhook", "equity", "cash"],
)
def test_the_strategy_touches_neither_options_nor_capital_nor_orders(forbidden: str) -> None:
    """**The gate.** Capital may decide executability; it may never decide
    whether an opportunity exists."""
    assert forbidden not in inspect.getsource(match_b).split('"""', 2)[-1].lower()


def test_the_service_makes_no_provider_call() -> None:
    source = inspect.getsource(match_b).split('"""', 2)[-1].lower()
    for forbidden in ("alpaca", "httpx", "requests.get", "provider"):
        assert forbidden not in source
