"""match-b-v1 in production. **The rule itself is imported, never restated.**

Why the definition is not written out here
------------------------------------------
``app.research.phase12_1`` holds the frozen forward-validation record: the
thresholds, the horizon, and the ``MATCH_B_DEFINITION`` dict that a test asserts
field-by-field. Re-declaring ``0.90`` and ``8.0`` in a production module would
create a second place the rule lives, and the first time someone edited one and
not the other the forward experiment would silently stop testing the hypothesis
it was registered against.

So this module imports the constants and builds the same predicate. Production
depending on the frozen record is the point, not an accident of layering: the
record *is* the specification.

What this module adds
---------------------
Only the things research did not need: a session-scoped evaluation with an
explicit outcome (:class:`EvaluationOutcome`), a candidate object carrying enough
provenance to reconstruct the decision, and a deterministic candidate identity so
re-running a completed session cannot mint a second candidate.

It computes no features of its own. The cross-sectional panel comes from
:func:`app.research.phase12.build_dataset`, which is the same construction the
research result was measured on and which a test pins for equivalence.

Causality
---------
The panel's features come from bars at or before the session close; entry is the
next session's open. Nothing here reads a bar later than the session being
evaluated, and there is no provider call -- it answers from stored candles only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

import polars as pl

from app.research.phase12_1 import (
    MATCH_B_DEFINITION,
    MATCH_B_HORIZON,
    MATCH_B_MOVEMENT_FLOOR,
    MATCH_B_RANK_FLOOR,
    match_b_mask,
)

STRATEGY_VERSION: Final = str(MATCH_B_DEFINITION["version"])
HORIZON_SESSIONS: Final = MATCH_B_HORIZON

UNIVERSE_VERSION: Final = "original-52-v1"
"""The 52 watchlisted symbols the frozen hypothesis was measured on.

Not the expanded universe. Phase 12.1 tested match-b-v1 on 990 symbols and
generalisation **failed** -- net advantage fell from +0.29pp to +0.007pp, and
went negative once semiconductors were excluded. Running the forward experiment
on a universe the hypothesis demonstrably does not hold in would test something
nobody registered.
"""


class EvaluationOutcome(StrEnum):
    """What happened when a session was evaluated.

    Three distinct states, kept separate because collapsing them destroys the
    only signal that tells an operator whether the system is working:
    ``NO_OPPORTUNITY`` is the strategy behaving correctly, ``NOT_EVALUATED`` is
    work still to do, and ``FAILED`` is something broken. A dashboard that
    rendered all three as "0 candidates" would hide an outage behind a quiet day.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    NO_OPPORTUNITY = "NO_OPPORTUNITY"
    CANDIDATES = "CANDIDATES"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class MatchBCandidate:
    """One opportunity, with enough provenance to reconstruct the decision.

    Carries **no** direction, target or conviction. match-b-v1 selects a symbol
    cross-sectionally; it does not predict a price, and a field implying one
    would be the product contradicting eight phases of its own evidence.
    """

    candidate_id: str
    strategy_version: str
    universe_version: str
    session: datetime
    data_cutoff: datetime
    decided_at: datetime
    symbol: str
    rank: float
    sector: str
    sector_etf_return_20d: float
    movement_to_cost: float
    atr_pct: float
    reference_price: Decimal

    @property
    def horizon_sessions(self) -> int:
        return HORIZON_SESSIONS


@dataclass(frozen=True, slots=True)
class SessionEvaluation:
    """The result of evaluating one trading session. Zero candidates is valid."""

    session: datetime
    outcome: EvaluationOutcome
    universe_size: int
    eligible_symbols: int
    candidates: tuple[MatchBCandidate, ...] = ()
    error: str | None = None

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


def candidate_identity(
    *, session: datetime, symbol: str, strategy_version: str, universe_version: str
) -> str:
    """Deterministic candidate id.

    A hash of the four things that define the decision, so the same session
    evaluated twice yields the same id and the downstream account-scoped order
    key (``paper:{slot}:{candidate_id}``) stays stable across a restart. Random
    ids would make every replay a new trade.
    """
    material = f"{strategy_version}|{universe_version}|{session.date().isoformat()}|{symbol}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def universe_hash(symbols: tuple[str, ...]) -> str:
    """Stable fingerprint of the universe actually used.

    Persisted with the experiment record so a later reader can tell whether the
    universe drifted -- a symbol added or delisted changes every cross-sectional
    rank, and a result computed on a different 52 is a different result.
    """
    joined = "|".join(sorted(symbols))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def evaluate_session(
    panel: pl.DataFrame,
    *,
    session: datetime,
    decided_at: datetime,
) -> SessionEvaluation:
    """Apply frozen match-b-v1 to one session of an already-built panel.

    Args:
        panel: the cross-sectional frame from
            :func:`app.research.phase12.build_dataset`, containing every symbol's
            causal features and per-session ranks.
        session: the trading session whose close is being evaluated.
        decided_at: when the decision was taken, for provenance.

    Returns:
        A :class:`SessionEvaluation`. Zero candidates yields
        ``NO_OPPORTUNITY`` -- an outcome, never an error.
    """
    day = panel.filter(pl.col("timestamp") == session)
    if day.height == 0:
        return SessionEvaluation(
            session=session,
            outcome=EvaluationOutcome.FAILED,
            universe_size=0,
            eligible_symbols=0,
            error=f"no eligible rows for session {session.date().isoformat()}",
        )

    selected = day.filter(match_b_mask()).sort("xs_rank_ret_20d", descending=True)
    universe = tuple(str(s) for s in day["symbol"].to_list())

    candidates = tuple(
        MatchBCandidate(
            candidate_id=candidate_identity(
                session=session,
                symbol=str(row["symbol"]),
                strategy_version=STRATEGY_VERSION,
                universe_version=UNIVERSE_VERSION,
            ),
            strategy_version=STRATEGY_VERSION,
            universe_version=UNIVERSE_VERSION,
            session=session,
            data_cutoff=session,
            decided_at=decided_at,
            symbol=str(row["symbol"]),
            rank=float(row["xs_rank_ret_20d"]),
            sector=str(row["sector"]),
            sector_etf_return_20d=float(row["sector_etf_ret_20d"]),
            movement_to_cost=float(row["movement_to_cost"]),
            atr_pct=float(row["atr_pct"]),
            reference_price=Decimal(str(row["close"])),
        )
        for row in selected.iter_rows(named=True)
    )

    return SessionEvaluation(
        session=session,
        outcome=(EvaluationOutcome.CANDIDATES if candidates else EvaluationOutcome.NO_OPPORTUNITY),
        universe_size=len(universe),
        eligible_symbols=day.height,
        candidates=candidates,
    )


def describe_frozen_rule() -> dict[str, object]:
    """The rule as production sees it, for the experiment record.

    Reads the imported constants rather than restating them, so this cannot
    drift from what :func:`match_b_mask` actually evaluates.
    """
    return {
        "strategy_version": STRATEGY_VERSION,
        "rank_floor": MATCH_B_RANK_FLOOR,
        "movement_floor": MATCH_B_MOVEMENT_FLOOR,
        "horizon_sessions": HORIZON_SESSIONS,
        "universe_version": UNIVERSE_VERSION,
        "sector_condition": MATCH_B_DEFINITION["sector_positive"],
    }
