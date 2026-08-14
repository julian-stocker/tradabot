"""Phase 12.1 — does frozen MATCH_B generalise beyond the 52 it was found on?

MATCH_B is frozen
-----------------
Its definition lives in :data:`MATCH_B_DEFINITION` and is asserted by tests.
Nothing in this phase may change the relative-strength rule, the sector-positive
rule, the movement-sufficiency rule, the rank threshold, the 3-session horizon
or the cost model. This phase can only ask whether the rule already found holds
somewhere it has never been applied.

Why universe expansion is the right next test
---------------------------------------------
Phase 12 found the effect on 52 famous megacaps that were chosen, years ago, by
a human writing a watchlist. That is a selection no rule can defend: the same
cross-sectional momentum result could be a property of large-cap tech in
2021-2026 rather than of markets. Broadening the universe by objective rules is
the cheapest way to find out, and it can only ever *disconfirm* -- there is no
threshold here to tune in MATCH_B's favour.

Outcome blindness
-----------------
Every eligibility rule below is about tradability, price, liquidity and data
completeness. **None references forward returns, momentum, or MATCH_B's
performance.** The rules were written and committed before a single bar of the
expanded universe was downloaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import polars as pl

# ===========================================================================
# PART H — the frozen rule
# ===========================================================================
MATCH_B_DEFINITION: Final[dict[str, object]] = {
    "version": "match-b-v1",
    "horizon_sessions": 3,
    "relative_strength": "xs_rank_ret_20d >= 0.90",
    "sector_positive": "sector_etf_ret_20d > 0",
    "movement_sufficiency": "movement_to_cost >= 8.0",
    "movement_to_cost": "atr_pct / round_trip_cost_pct",
    "rank_basis": "cross-sectional percentile of trailing 20-session return, per session",
    "entry": "open of t+1",
    "exit_measurement": "close of t+3",
    "cost_model": "app.paper.execution.estimate_round_trip_cost at EUR 2,000 notional",
}
"""The exact rule carried forward from phase 12. **Do not edit.**

A test asserts every value. Changing one here is changing the experiment, and
the whole point of this phase is that the experiment cannot change while the
data does.
"""

MATCH_B_RANK_FLOOR: Final = 0.90
MATCH_B_MOVEMENT_FLOOR: Final = 8.0
MATCH_B_HORIZON: Final = 3


def match_b_mask() -> pl.Expr:
    """The frozen rule as a polars predicate. The only place it is expressed."""
    return (
        (pl.col("xs_rank_ret_20d") >= MATCH_B_RANK_FLOOR)
        & (pl.col("sector_etf_ret_20d") > 0)
        & (pl.col("movement_to_cost") >= MATCH_B_MOVEMENT_FLOOR)
    )


# ===========================================================================
# PART B — expanded universe eligibility, registered before any download
# ===========================================================================
PROVIDER_FLOOR: Final = datetime(2020, 7, 27, tzinfo=UTC)
"""Verified in phase 10.1. Nothing earlier exists on the IEX feed."""

PROBE_START: Final = datetime(2025, 1, 2, tzinfo=UTC)
PROBE_END: Final = datetime(2025, 4, 1, tzinfo=UTC)
"""Liquidity screening window.

A recent quarter, chosen because current tradability is what a future forward
test would face. It sits *inside* the phase 12 evaluation span, which is
acceptable only because the screen reads price and volume -- never returns, and
never anything about MATCH_B.
"""

MIN_PRICE: Final = Decimal("10")
"""Below this a name is a penny stock whose spread the cost model cannot
represent honestly."""

MIN_IEX_DOLLAR_VOLUME: Final = Decimal("2000000")
"""Median daily IEX dollar volume, in USD.

**On the IEX scale, not consolidated.** Volume on this feed is a single venue's
prints -- roughly 2-3% of national volume, measured directly: AAPL shows about
1.0M shares a day here against roughly 50M consolidated. EUR 2M of IEX notional
therefore corresponds to something on the order of EUR 100M consolidated, which
is the liquidity band the original 52 occupy.
"""

MIN_PROBE_COMPLETENESS: Final = 0.95
MIN_HISTORY_BARS: Final = 1400
"""Daily bars required from the provider floor. The original 52 carry 1,517+."""

MAX_UNIVERSE: Final = 300

ETF_NAME_PATTERN: Final = re.compile(
    r"\b(ETF|ETN|FUND|TRUST|INDEX|SHARES|PORTFOLIO|PROSHARES|ISHARES|SPDR|"
    r"INVESCO|VANGUARD|DIREXION|VANECK|GLOBAL X|WISDOMTREE|FIRST TRUST|"
    r"JANUS|SCHWAB|FIDELITY|AMPLIFY|GRANITESHARES|DEFIANCE|ROUNDHILL|"
    r"TIDAL|SIMPLIFY|PACER|ALPS|XTRACKERS|FRANKLIN|DIMENSIONAL)\b",
    re.IGNORECASE,
)
"""Name-based fund exclusion.

Alpaca's ``us_equity`` asset class contains ETFs and common stock with no field
separating them, so the filter has to be textual. It will keep a handful of
operating companies with "Trust" in the name (REITs) and that is the safer
error: including a REIT weakens nothing, whereas including a leveraged sector
ETF would put a derivative of the benchmark into the cross-section it is being
compared against.
"""

SYMBOL_PATTERN: Final = re.compile(r"^[A-Z]{1,5}$")
"""Plain tickers only. Class shares and unit/warrant suffixes are dropped rather
than deduplicated, because two share classes of one company are one bet."""

ELIGIBLE_EXCHANGES: Final[frozenset[str]] = frozenset({"NASDAQ", "NYSE"})
"""ARCA, BATS and AMEX are ETF-dominated; OTC fails every liquidity assumption
the cost model makes."""


@dataclass(frozen=True, slots=True)
class SelectionRule:
    number: str
    description: str


SELECTION_RULES: Final[tuple[SelectionRule, ...]] = (
    SelectionRule("R1", "Alpaca asset status ACTIVE, asset_class US_EQUITY"),
    SelectionRule("R2", "tradable is true"),
    SelectionRule("R3", "fractionable is true (the EUR 1,000 case needs it)"),
    SelectionRule("R4", f"listing exchange in {sorted(ELIGIBLE_EXCHANGES)}"),
    SelectionRule("R5", "company name does not match the fund/ETF pattern"),
    SelectionRule("R6", "symbol matches ^[A-Z]{1,5}$"),
    SelectionRule("R7", f"median close over the probe window >= {MIN_PRICE}"),
    SelectionRule("R8", f"median daily IEX dollar volume >= {MIN_IEX_DOLLAR_VOLUME}"),
    SelectionRule("R9", f"probe-window bar completeness >= {MIN_PROBE_COMPLETENESS:.0%}"),
    SelectionRule("R10", f">= {MIN_HISTORY_BARS} daily bars from the provider floor"),
    SelectionRule("R11", "not a registered benchmark instrument"),
    SelectionRule("R12", f"if more than {MAX_UNIVERSE} survive, keep the most liquid"),
)
"""The complete rule set. **No rule references returns, momentum or MATCH_B.**

R12 is the only ranking step and it ranks by liquidity, which is a property of
the instrument rather than of its outcome.
"""


# ===========================================================================
# Sector assignment for symbols with no watchlist tag
# ===========================================================================
SECTOR_FIT_WINDOW_END: Final = datetime(2021, 7, 27, tzinfo=UTC)
"""Sector assignment is fitted only on bars before this instant.

The phase 12 evaluation panel starts on 2021-07-27, so the correlation window
closes exactly where the evaluation opens. A sector label fitted on the same
bars it is later evaluated over would leak, even though correlation to a sector
ETF is not the target.
"""


def assign_sectors_by_correlation(
    returns: pl.DataFrame, sector_returns: pl.DataFrame
) -> dict[str, str]:
    """Label each symbol with the sector ETF its daily returns track most closely.

    Sector membership has to come from somewhere: Alpaca publishes no
    classification, and hand-writing 250 labels from memory would be fabricating
    metadata -- the exact failure mode phase 9 built ``AssetMetadata`` to stop.

    Correlation is how sector membership is empirically defined anyway, and it is
    outcome-blind here: it uses contemporaneous returns against a benchmark,
    never forward returns and never anything about MATCH_B. Its accuracy is
    measurable, because the original 52 carry human-assigned tags to check
    against.

    Args:
        returns: long frame with ``symbol``, ``timestamp``, ``ret_1d``, already
            restricted to bars before :data:`SECTOR_FIT_WINDOW_END`.
        sector_returns: the same shape for the nine sector ETFs, with the sector
            name in ``sector``.

    Returns:
        symbol -> sector name. Symbols with too little overlap are omitted
        rather than assigned a default, because a wrong sector is worse than a
        missing one: MATCH_B gates on the sector benchmark being positive.
    """
    wide = sector_returns.pivot(on="sector", index="timestamp", values="ret_1d")
    sectors = [c for c in wide.columns if c != "timestamp"]
    joined = returns.join(wide, on="timestamp", how="inner")

    out: dict[str, str] = {}
    for (symbol,), group in joined.group_by("symbol"):
        clean = group.drop_nulls(subset=["ret_1d"])
        if clean.height < 120:  # noqa: PLR2004 -- roughly half a year of overlap
            continue
        best_name, best_value = None, -2.0
        for sector in sectors:
            pair = clean.drop_nulls(subset=[sector])
            if pair.height < 120:  # noqa: PLR2004
                continue
            value = pair.select(pl.corr("ret_1d", sector)).item()
            if value is not None and value > best_value:
                best_name, best_value = sector, float(value)
        if best_name is not None:
            out[str(symbol)] = best_name
    return out


# ===========================================================================
# PART N — the corrected breakout retest, registered before inspection
# ===========================================================================
BREAKOUT_RETEST: Final[dict[str, object]] = {
    "definition": "close > max(high over the PRIOR 20 sessions, excluding today)",
    "volume_condition": "none -- earlier phases found standalone volume uninformative",
    "relative_strength_condition": "xs_rank_ret_20d >= 0.60",
    "horizon_sessions": 3,
    "sample_gate": 500,
    "variants": 1,
}
"""Exactly one corrected experiment.

Phase 12's version compared the close against a 20-day high that **included the
current bar**, which requires the close to exceed today's own high and left 156
observations out of 65,630. That was a defect in the definition, not a finding
about breakouts. This is the corrected single test; running several variants
would turn a fix into a search.
"""


# ===========================================================================
# PART V — contribution accounting
# ===========================================================================
MONTHLY_CONTRIBUTION: Final = Decimal("200")


@dataclass(slots=True)
class WealthLedger:
    """Keeps contributed capital and trading P&L structurally separate.

    The failure this prevents is the most flattering one available to a savings
    plan: paying EUR 200 a month into an account, watching the balance rise, and
    reporting the rise as strategy performance. Total equity is the sum of three
    things and this class refuses to let them merge.
    """

    initial_capital: Decimal
    contributed: Decimal = Decimal(0)
    trading_pnl: Decimal = Decimal(0)

    @property
    def invested(self) -> Decimal:
        """Money the user put in. Never a result."""
        return self.initial_capital + self.contributed

    @property
    def total_equity(self) -> Decimal:
        return self.invested + self.trading_pnl

    def contribute(self, amount: Decimal) -> None:
        if amount < 0:
            msg = "contributions are deposits; a negative one is a withdrawal"
            raise ValueError(msg)
        self.contributed += amount

    def record_trading(self, amount: Decimal) -> None:
        self.trading_pnl += amount
