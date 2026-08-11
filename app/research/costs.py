"""Estimating what a historical trade would have cost to execute.

tradabot stores no historical quotes -- only OHLCV bars -- so there is no
recorded bid/ask for any past instant. Two responses are available and only one
is honest.

The dishonest one is to reach for the *current* quote. It is right there, it has
a real spread on it, and using it would be look-ahead of the purest kind: a 2026
spread applied to a February fill, plus a survivorship flavour, since an
instrument only has a current quote if it still trades. This module exists so
that shortcut is never taken.

The honest one is to model the cost from information that was available at the
time -- price, volatility, participation -- label the result ``MODELLED``, and
version it. That is what this does. The output is an *assumption with a name*,
not a measurement, and :class:`~app.domain.enums.CostBasis` travels with every
figure so no report can present it as observed.

Deliberately crude
------------------
The model is a small number of legible terms with configurable coefficients. It
would be easy to add microstructure sophistication here and it would be false
precision: there is no historical spread data to fit against, so a more elaborate
formula would only be more confidently wrong. A conservative estimate that is
visibly an estimate is worth more than a calibrated-looking one that is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.core.config import CostSettings
from app.costs.calculator import estimate_round_trip_cost
from app.costs.models import RoundTripCost
from app.domain.enums import CostBasis, Side
from app.scanner.enums import SessionPhase

COST_MODEL_VERSION: Final = "hist-cost-v1"
"""Identifies these coefficients and this formula.

Every trade outcome records it. Pooling results computed under different cost
models produces a number that describes neither.
"""

BASE_SPREAD_BPS: Final = 3.0
"""Floor for a liquid large-cap during regular hours.

Chosen to sit at the pessimistic end of what megacap US equities actually quote
(typically 1-2 bps) because every unmeasured friction in this project -- queue
position, odd-lot handling, the fact that IEX is not the consolidated tape --
pushes the true figure up rather than down.
"""

VOLATILITY_COEFFICIENT: Final = 0.15
"""Extra bps of spread per **percentage point of annualised** volatility.

The unit matters and getting it wrong is not subtle. ``volatility_20`` is
annualised and returned as a fraction -- 0.22 means 22% a year, which is ordinary
for a large-cap. An earlier version of this model read it as a per-bar fraction
and scaled it by 10,000, producing ~1,100 bps of spread on every observation,
pinning every estimate to :data:`MAX_MODELLED_SPREAD_BPS` and making the entire
net-P&L column a measurement of the cap rather than of the market.

At 0.15, a 22% name adds ~3.3 bps to the ~3 bp base: a mid-single-digit total,
which is pessimistic against the 1-2 bps such names really quote and therefore
errs the right way.
"""

LOW_PRICE_THRESHOLD: Final = Decimal(20)
"""Below this, the tick becomes a material fraction of the spread."""

LOW_PRICE_PENALTY_BPS: Final = 4.0
"""A one-cent tick is 5 bps on a 20 EUR share and 0.2 bps on a 500 EUR one."""

EXTENDED_HOURS_MULTIPLIER: Final = 4.0
"""How much wider to assume a spread is outside the regular session.

Anchored to the phase 4 observation that IEX after-hours spreads ran one to two
orders of magnitude above session levels. 4x is far below what was actually
seen, and that is intentional: extended-hours trading is not part of the primary
benchmark, so this multiplier exists to keep such rows from looking *free*, not
to model them accurately.
"""

MAX_MODELLED_SPREAD_BPS: Final = 250.0
"""Ceiling, so a volatility spike cannot produce an absurd cost."""


@dataclass(frozen=True, slots=True)
class ModelledSpread:
    """An estimated spread, with its provenance attached."""

    spread_bps: float
    basis: CostBasis
    model_version: str
    components: dict[str, float]
    """Each term's contribution, so a surprising total can be explained."""


def model_spread_bps(
    *,
    price: Decimal | float | None,
    volatility: float | None = None,
    relative_volume: float | None = None,
    session: SessionPhase = SessionPhase.REGULAR,
) -> ModelledSpread:
    """Estimate the round-trip-relevant spread for a historical instant.

    Every input is something that was knowable at that instant. ``None`` inputs
    degrade to the base term rather than raising: a missing volatility reading
    should widen the error bars, not delete the observation.
    """
    components: dict[str, float] = {"base": BASE_SPREAD_BPS}
    total = BASE_SPREAD_BPS

    if volatility is not None and volatility > 0:
        # ``volatility_20`` is **annualised** and a fraction: 0.22 == 22% a year.
        # Convert to percentage points, not to bps -- see VOLATILITY_COEFFICIENT.
        contribution = min(volatility * 100.0 * VOLATILITY_COEFFICIENT, MAX_MODELLED_SPREAD_BPS)
        components["volatility"] = contribution
        total += contribution

    if price is not None and Decimal(str(price)) > 0 and Decimal(str(price)) < LOW_PRICE_THRESHOLD:
        components["low_price"] = LOW_PRICE_PENALTY_BPS
        total += LOW_PRICE_PENALTY_BPS

    if relative_volume is not None and 0 < relative_volume < 1:
        # Thin participation relative to its own average: crossing costs more.
        contribution = BASE_SPREAD_BPS * (1.0 / relative_volume - 1.0)
        contribution = min(contribution, MAX_MODELLED_SPREAD_BPS)
        components["thin_volume"] = contribution
        total += contribution

    if session is not SessionPhase.REGULAR:
        before = total
        total *= EXTENDED_HOURS_MULTIPLIER
        components["extended_hours"] = total - before

    total = min(total, MAX_MODELLED_SPREAD_BPS)
    return ModelledSpread(
        spread_bps=total,
        basis=CostBasis.MODELLED,
        model_version=COST_MODEL_VERSION,
        components=components,
    )


def historical_round_trip(
    *,
    entry_mid: Decimal,
    exit_mid: Decimal,
    quantity: Decimal,
    settings: CostSettings,
    volatility: float | None = None,
    relative_volume: float | None = None,
    session: SessionPhase = SessionPhase.REGULAR,
    side: Side = Side.LONG,
) -> tuple[RoundTripCost, ModelledSpread]:
    """Full round-trip cost for a historical fill.

    Reuses the production :func:`~app.costs.calculator.estimate_round_trip_cost`
    rather than reimplementing fee and slippage arithmetic -- only the *spread
    input* is modelled here. Two cost calculators would eventually disagree, and
    the backtest disagreeing with live paper trading is the one divergence that
    would invalidate every comparison between them.
    """
    spread = model_spread_bps(
        price=entry_mid,
        volatility=volatility,
        relative_volume=relative_volume,
        session=session,
    )
    cost = estimate_round_trip_cost(
        entry_mid=entry_mid,
        exit_mid=exit_mid,
        quantity=quantity,
        spread_bps=Decimal(str(spread.spread_bps)),
        settings=settings,
        side=side,
    )
    return cost, spread
