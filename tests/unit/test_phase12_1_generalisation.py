"""Phase 12.1 tests what phase 12 found, on data phase 12 never saw.

That only means anything if two things hold: the rule cannot have changed, and
the new universe cannot have been chosen with the rule's performance in mind.
Both are pinned here, because both are invisible in a results table — a tuned
threshold and a cherry-picked universe produce the same confident numbers as a
real generalisation.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import polars as pl
import pytest

from app.research import phase12_1
from app.research.phase12_1 import (
    BREAKOUT_RETEST,
    ELIGIBLE_EXCHANGES,
    ETF_NAME_PATTERN,
    MATCH_B_DEFINITION,
    MATCH_B_HORIZON,
    MATCH_B_MOVEMENT_FLOOR,
    MATCH_B_RANK_FLOOR,
    MIN_HISTORY_BARS,
    MIN_IEX_DOLLAR_VOLUME,
    MIN_PRICE,
    MONTHLY_CONTRIBUTION,
    SECTOR_FIT_WINDOW_END,
    SELECTION_RULES,
    SYMBOL_PATTERN,
    WealthLedger,
    assign_sectors_by_correlation,
    match_b_mask,
)


# ---------------------------------------------------------------------------
# H: the rule is frozen
# ---------------------------------------------------------------------------
class TestMatchBIsFrozen:
    def test_the_definition_is_exactly_what_phase_12_found(self) -> None:
        """**The gate.** Editing any value here changes the experiment, and the
        entire claim of this phase is that the experiment did not change."""
        assert MATCH_B_DEFINITION["version"] == "match-b-v1"
        assert MATCH_B_DEFINITION["horizon_sessions"] == 3
        assert MATCH_B_DEFINITION["relative_strength"] == "xs_rank_ret_20d >= 0.90"
        assert MATCH_B_DEFINITION["sector_positive"] == "sector_etf_ret_20d > 0"
        assert MATCH_B_DEFINITION["movement_sufficiency"] == "movement_to_cost >= 8.0"
        assert MATCH_B_RANK_FLOOR == 0.90
        assert MATCH_B_MOVEMENT_FLOOR == 8.0
        assert MATCH_B_HORIZON == 3

    def test_the_mask_matches_the_written_definition(self) -> None:
        """A definition dict that drifts from the executed predicate is worse
        than no definition dict at all."""
        rendered = str(match_b_mask())
        assert "0.9" in rendered
        assert "8" in rendered
        assert "sector_etf_ret_20d" in rendered
        assert "xs_rank_ret_20d" in rendered

    def test_the_mask_has_exactly_three_conditions(self) -> None:
        """No fourth filter may be appended -- not RSI, not EMA, not volume."""
        frame = pl.DataFrame(
            {
                "xs_rank_ret_20d": [0.95, 0.95, 0.95, 0.85],
                "sector_etf_ret_20d": [1.0, -1.0, 1.0, 1.0],
                "movement_to_cost": [9.0, 9.0, 2.0, 9.0],
            }
        )
        assert frame.filter(match_b_mask()).height == 1

    def test_the_module_defines_no_weights_or_score(self) -> None:
        source = inspect.getsource(phase12_1).lower()
        for forbidden in ("weight", "def score", "0-100", "coefficient"):
            assert forbidden not in source


# ---------------------------------------------------------------------------
# B: the universe was chosen blind to outcomes
# ---------------------------------------------------------------------------
class TestOutcomeBlindSelection:
    def test_no_selection_rule_mentions_returns_or_performance(self) -> None:
        """**The gate.** A universe picked using the result is not a test of it."""
        forbidden = ("return", "momentum", "match_b", "performance", "profit", "advantage")
        for rule in SELECTION_RULES:
            text = rule.description.lower()
            for word in forbidden:
                assert word not in text, f"{rule.number} references {word!r}"

    def test_every_rule_is_a_property_of_the_instrument_not_its_outcome(self) -> None:
        allowed = (
            "status",
            "tradable",
            "fractionable",
            "exchange",
            "name",
            "symbol",
            "close",
            "volume",
            "bar",
            "benchmark",
            "liquid",
        )
        for rule in SELECTION_RULES:
            assert any(word in rule.description.lower() for word in allowed), rule.number

    def test_the_only_ranking_step_ranks_by_liquidity(self) -> None:
        assert "liquid" in SELECTION_RULES[-1].description.lower()

    def test_thresholds_are_the_registered_ones(self) -> None:
        assert Decimal("10") == MIN_PRICE
        assert Decimal("2000000") == MIN_IEX_DOLLAR_VOLUME
        assert MIN_HISTORY_BARS == 1400

    @pytest.mark.parametrize(
        ("name", "excluded"),
        [
            ("iShares Core S&P 500 ETF", True),
            ("SPDR Gold Trust", True),
            ("Vanguard Total Stock Market", True),
            ("Direxion Daily Semiconductor Bull", True),
            ("Apple Inc.", False),
            ("JPMorgan Chase & Co.", False),
        ],
    )
    def test_the_fund_filter_separates_funds_from_operating_companies(
        self, name: str, excluded: bool
    ) -> None:
        assert bool(ETF_NAME_PATTERN.search(name)) is excluded

    @pytest.mark.parametrize(
        ("symbol", "kept"),
        [("AAPL", True), ("F", True), ("BRK.B", False), ("ABCDEF", False), ("aapl", False)],
    )
    def test_only_plain_tickers_are_admitted(self, symbol: str, kept: bool) -> None:
        assert bool(SYMBOL_PATTERN.match(symbol)) is kept

    def test_only_the_registered_exchanges_are_eligible(self) -> None:
        assert frozenset({"NASDAQ", "NYSE"}) == ELIGIBLE_EXCHANGES


# ---------------------------------------------------------------------------
# Sector assignment: causal, and refuses rather than guesses
# ---------------------------------------------------------------------------
class TestSectorAssignment:
    def frames(self) -> tuple[pl.DataFrame, pl.DataFrame]:
        days = list(range(300))
        tech = [((i * 7) % 13 - 6) / 10 for i in days]
        energy = [((i * 11) % 17 - 8) / 10 for i in days]
        sector_returns = pl.DataFrame(
            {
                "timestamp": days * 2,
                "sector": ["technology"] * 300 + ["energy"] * 300,
                "ret_1d": tech + energy,
            }
        )
        stocks = pl.DataFrame(
            {
                "symbol": ["TECHY"] * 300 + ["OILY"] * 300,
                "timestamp": days * 2,
                "ret_1d": [v * 1.5 for v in tech] + [v * 1.2 for v in energy],
            }
        )
        return stocks, sector_returns

    def test_a_symbol_is_labelled_by_its_closest_sector(self) -> None:
        stocks, sectors = self.frames()
        assigned = assign_sectors_by_correlation(stocks, sectors)
        assert assigned["TECHY"] == "technology"
        assert assigned["OILY"] == "energy"

    def test_a_symbol_with_too_little_overlap_is_omitted_not_defaulted(self) -> None:
        """**The gate.** MATCH_B gates on the sector benchmark, so a wrong label
        is worse than a missing one."""
        stocks, sectors = self.frames()
        thin = stocks.filter(pl.col("symbol") == "TECHY").head(20)
        assert assign_sectors_by_correlation(thin, sectors) == {}

    def test_the_fit_window_closes_before_the_evaluation_panel_opens(self) -> None:
        """2021-07-27 is the first evaluation row in phase 12's panel."""
        assert SECTOR_FIT_WINDOW_END.year == 2021
        assert (SECTOR_FIT_WINDOW_END.month, SECTOR_FIT_WINDOW_END.day) == (7, 27)


# ---------------------------------------------------------------------------
# N: exactly one corrected breakout test
# ---------------------------------------------------------------------------
class TestBreakoutRetest:
    def test_the_corrected_definition_excludes_the_current_bar(self) -> None:
        assert "PRIOR" in str(BREAKOUT_RETEST["definition"])
        assert "excluding today" in str(BREAKOUT_RETEST["definition"])

    def test_only_one_variant_is_registered(self) -> None:
        """Several variants would turn a bug fix into a search."""
        assert BREAKOUT_RETEST["variants"] == 1

    def test_the_prior_high_never_includes_the_current_bar(self) -> None:
        highs = pl.DataFrame({"high": [10.0, 11.0, 12.0, 9.0], "close": [9.0, 9.0, 9.0, 11.5]})
        prior = highs.with_columns(
            pl.col("high").shift(1).rolling_max(window_size=2, min_samples=1).alias("prior_high")
        )
        # At row 3 the prior 2-session high is max(11, 12) = 12, not today's 9.
        assert prior["prior_high"][3] == 12.0
        assert prior["close"][3] < prior["prior_high"][3]


# ---------------------------------------------------------------------------
# V: contributions are never trading profit
# ---------------------------------------------------------------------------
class TestContributionAccounting:
    def test_a_contribution_raises_equity_without_touching_trading_pnl(self) -> None:
        """**The gate.** The most flattering error a savings plan can make."""
        ledger = WealthLedger(initial_capital=Decimal("1000"))
        ledger.contribute(Decimal("200"))
        assert ledger.total_equity == Decimal("1200")
        assert ledger.trading_pnl == Decimal("0")
        assert ledger.invested == Decimal("1200")

    def test_total_equity_always_decomposes_into_its_three_parts(self) -> None:
        ledger = WealthLedger(initial_capital=Decimal("1000"))
        for _ in range(12):
            ledger.contribute(MONTHLY_CONTRIBUTION)
        ledger.record_trading(Decimal("-150"))
        assert ledger.invested == Decimal("3400")
        assert ledger.trading_pnl == Decimal("-150")
        assert ledger.total_equity == Decimal("3250")
        assert ledger.total_equity == ledger.invested + ledger.trading_pnl

    def test_a_losing_strategy_cannot_be_hidden_by_deposits(self) -> None:
        """Equity above the start while trading lost money must remain visible."""
        ledger = WealthLedger(initial_capital=Decimal("1000"))
        for _ in range(10):
            ledger.contribute(MONTHLY_CONTRIBUTION)
        ledger.record_trading(Decimal("-400"))
        assert ledger.total_equity > ledger.initial_capital
        assert ledger.trading_pnl < 0

    def test_a_negative_contribution_is_refused(self) -> None:
        ledger = WealthLedger(initial_capital=Decimal("1000"))
        with pytest.raises(ValueError, match="withdrawal"):
            ledger.contribute(Decimal("-50"))

    def test_the_registered_contribution_is_two_hundred(self) -> None:
        assert Decimal("200") == MONTHLY_CONTRIBUTION


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def test_options_data_is_excluded_from_this_phase() -> None:
    source = inspect.getsource(phase12_1).lower()
    for forbidden in ("option_surface", "option_quote", "implied_volatility", "iv_30d"):
        assert forbidden not in source


def test_the_phase_places_no_orders_and_enables_nothing() -> None:
    source = inspect.getsource(phase12_1).lower()
    for forbidden in ("submit_order", "place_order", "tradingclient", "webhook", "locked_reserve"):
        assert forbidden not in source
