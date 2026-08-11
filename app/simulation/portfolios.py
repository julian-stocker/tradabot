"""The personal paper portfolios.

Three simulation portfolios with their own Discord destinations, seeded from
**data** rather than from behaviour branching on capital size. Nothing anywhere
reads a portfolio's capital to decide where a message goes; routing comes from
:attr:`PersonalPortfolio.notification_channel`, which is stored on the profile.

Adding a fourth is one entry in this tuple plus one environment variable. No
business logic changes, which is the point of Part B.

> **These are experimental simulation configurations, not financial
> recommendations.** The capital sizes are round numbers chosen to show how the
> same signal produces different decisions at different account sizes -- a fixed
> per-order fee is 1% of a 100 EUR round trip and 0.01% of a 10,000 EUR one, and
> making that visible is the entire reason there are three.

The nine default profiles from phase 3 are **not** replaced. They remain the
generic architecture; these three are personal instances of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.simulation.defaults import (
    BALANCED,
    DEFAULT_CURRENCY,
    FLAT_FEE_BROKER,
)
from app.simulation.models import SimulationProfileConfig


@dataclass(frozen=True, slots=True)
class PersonalPortfolio:
    """One personal portfolio and where its notifications go."""

    key: str
    """Routing key and profile name, e.g. ``paper-100``. Stable identity: it is
    stored on the profile row and is what notification routing looks up."""
    capital: Decimal
    description: str

    @property
    def notification_channel(self) -> str:
        return self.key


PERSONAL_PORTFOLIOS: Final[tuple[PersonalPortfolio, ...]] = (
    PersonalPortfolio(
        key="paper-100",
        capital=Decimal("100"),
        description="100 EUR personal paper portfolio, balanced risk.",
    ),
    PersonalPortfolio(
        key="paper-1000",
        capital=Decimal("1000"),
        description="1000 EUR personal paper portfolio, balanced risk.",
    ),
    PersonalPortfolio(
        key="paper-10000",
        capital=Decimal("10000"),
        description="10000 EUR personal paper portfolio, balanced risk.",
    ),
)

PORTFOLIO_KEYS: Final[tuple[str, ...]] = tuple(p.key for p in PERSONAL_PORTFOLIOS)


def build_personal_profiles(
    portfolios: tuple[PersonalPortfolio, ...] = PERSONAL_PORTFOLIOS,
) -> tuple[SimulationProfileConfig, ...]:
    """Simulation profiles for the personal portfolios.

    All three share the **balanced** risk profile from
    :mod:`app.simulation.defaults`, so the only difference between them is
    capital. That isolates the variable actually being studied: with three risk
    profiles as well, a difference in outcome could not be attributed to account
    size.
    """
    return tuple(
        SimulationProfileConfig(
            name=portfolio.key,
            description=portfolio.description,
            initial_capital=portfolio.capital,
            currency=DEFAULT_CURRENCY,
            risk=BALANCED,
            costs=FLAT_FEE_BROKER,
            notification_channel=portfolio.notification_channel,
        )
        for portfolio in portfolios
    )
