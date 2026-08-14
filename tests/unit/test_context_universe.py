"""The gates phase 9A must not be allowed to breach.

Context instruments are new, and the whole risk of adding them is that one leaks
into a path that treats it as a trade candidate. These tests are that boundary,
stated as assertions rather than as care: the trade universe, the paper
portfolios, the routing and the Discord language each get a test that fails
loudly if an ETF turns up where it should not.
"""

from __future__ import annotations

import pytest

from app.domain.enums import AssetType
from app.market_data.benchmarks import (
    ALTERNATE_BENCHMARKS,
    BENCHMARK_SYMBOLS,
    BENCHMARKS,
    MARKET_BENCHMARKS,
    SECTOR_BENCHMARKS,
    ContextRole,
    benchmark_infos,
    is_benchmark,
    market_benchmark,
    parent_benchmark,
    sector_benchmark,
)
from app.notifications.market_context import (
    CONTEXT_DISCLAIMER,
    MAX_CONTEXT_SYMBOLS,
    ReferenceMove,
    StockContext,
    build_context_block,
)
from app.notifications.trends import assert_no_recommendation_language
from app.scanner.universe import universe_symbols

WATCHLIST_SECTORS = {
    "technology",
    "semiconductors",
    "communication",
    "consumer-discretionary",
    "consumer-staples",
    "financials",
    "healthcare",
    "industrials",
    "energy",
}


# ---------------------------------------------------------------------------
# TRADE_UNIVERSE and CONTEXT_UNIVERSE are disjoint
# ---------------------------------------------------------------------------
def test_no_context_instrument_is_in_the_trade_universe() -> None:
    """**The gate.** The seeded universe and the context set must not overlap."""
    overlap = set(universe_symbols()) & set(BENCHMARK_SYMBOLS)
    assert overlap == set(), f"context instruments in the trade universe: {sorted(overlap)}"


def test_context_symbols_are_unique() -> None:
    assert len(BENCHMARK_SYMBOLS) == len(set(BENCHMARK_SYMBOLS))


def test_every_context_instrument_is_registered_as_an_etf() -> None:
    """Asset type is what a future cost or borrow model would branch on."""
    assert all(info.asset_type is AssetType.ETF for info in benchmark_infos())


def test_is_benchmark_recognises_every_context_symbol() -> None:
    for symbol in BENCHMARK_SYMBOLS:
        assert is_benchmark(symbol)
        assert is_benchmark(symbol.lower())


def test_is_benchmark_rejects_watchlist_names() -> None:
    for symbol in universe_symbols():
        assert not is_benchmark(symbol)


# ---------------------------------------------------------------------------
# Sector mapping (part D)
# ---------------------------------------------------------------------------
def test_every_watchlist_sector_has_exactly_one_benchmark() -> None:
    mapped = [b.sector for b in SECTOR_BENCHMARKS]
    assert set(mapped) == WATCHLIST_SECTORS
    assert len(mapped) == len(set(mapped)), "a sector tag is mapped twice"


def test_no_fund_serves_two_sectors() -> None:
    """XLK for both technology and semiconductors would collapse the distinction."""
    funds = [b.symbol for b in SECTOR_BENCHMARKS]
    assert len(funds) == len(set(funds))


def test_semiconductors_declares_technology_as_its_parent() -> None:
    """Part D asks for hierarchy, not a choice between SMH and XLK."""
    semis = sector_benchmark("semiconductors")
    assert semis.symbol == "SMH"
    parent = parent_benchmark("semiconductors")
    assert parent is not None
    assert parent.symbol == "XLK"


def test_sectors_without_a_parent_return_none_not_the_market() -> None:
    """'No parent' and 'parent is the whole market' are different statements."""
    for sector in WATCHLIST_SECTORS - {"semiconductors"}:
        assert parent_benchmark(sector) is None


def test_an_unmapped_sector_raises_rather_than_returning_nothing() -> None:
    with pytest.raises(KeyError, match="no sector benchmark"):
        sector_benchmark("utilities")


def test_roles_are_consistent_with_the_catalogue_shape() -> None:
    assert all(b.role is ContextRole.MARKET for b in MARKET_BENCHMARKS)
    assert all(b.role is ContextRole.SECTOR for b in SECTOR_BENCHMARKS)
    assert all(b.role is ContextRole.ALTERNATE for b in ALTERNATE_BENCHMARKS)
    assert market_benchmark().symbol == "SPY"


def test_alternates_are_not_mapped_to_any_sector() -> None:
    """SOXX is stored for comparison, and must never be joined as a sector."""
    mapped = {b.symbol for b in SECTOR_BENCHMARKS}
    for alternate in ALTERNATE_BENCHMARKS:
        assert alternate.symbol not in mapped
        assert alternate.sector is None


def test_the_catalogue_is_the_union_of_its_parts() -> None:
    """One source of truth: no symbol may exist in a sub-tuple and not in BENCHMARKS."""
    parts = {b.symbol for b in (*MARKET_BENCHMARKS, *SECTOR_BENCHMARKS, *ALTERNATE_BENCHMARKS)}
    assert parts == set(BENCHMARK_SYMBOLS)
    assert len(BENCHMARKS) == len(parts)


# ---------------------------------------------------------------------------
# Discord: descriptive language only (part L)
# ---------------------------------------------------------------------------
def test_the_context_block_survives_the_recommendation_guard() -> None:
    """**The gate.** Every rendered line must pass the language check."""
    block = build_context_block(
        [
            ReferenceMove("SPY", 0.8, "UP"),
            ReferenceMove("SMH", 2.4, "UP"),
        ],
        [
            StockContext(
                symbol="NVDA",
                return_pct=4.2,
                market_symbol="SPY",
                versus_market_pp=3.4,
                sector_symbol="SMH",
                versus_sector_pp=1.8,
            )
        ],
    )
    assert block is not None
    assert_no_recommendation_language("\n".join([block["title"], *block["lines"]]))


def test_a_negative_session_also_passes_the_guard() -> None:
    """Falling numbers must not tempt the renderer into 'exit' or 'sell'."""
    block = build_context_block(
        [ReferenceMove("SPY", -2.1, "DOWN"), ReferenceMove("XLE", -3.4, "DOWN")],
        [StockContext("XOM", -4.8, "SPY", -2.7, "XLE", -1.4)],
    )
    assert block is not None
    assert_no_recommendation_language("\n".join([block["title"], *block["lines"]]))


def test_the_disclaimer_says_what_this_is_not() -> None:
    """Exempt from the guard, and the only string allowed to name the negation."""
    assert "not a trade recommendation" in CONTEXT_DISCLAIMER.lower()


def test_the_block_is_none_when_there_are_no_references() -> None:
    """A header with no content reads as a failure, not as an empty market."""
    assert build_context_block([], []) is None


def test_references_render_without_stocks() -> None:
    block = build_context_block([ReferenceMove("SPY", -1.2, "DOWN")], [])
    assert block is not None
    assert any("SPY" in line for line in block["lines"])


def test_the_block_is_capped() -> None:
    stocks = [StockContext(f"S{i}", 1.0, "SPY", 0.5) for i in range(MAX_CONTEXT_SYMBOLS + 6)]
    block = build_context_block([ReferenceMove("SPY", 0.4)], stocks)
    assert block is not None
    rendered = "\n".join(block["lines"])
    assert sum(1 for s in stocks if f"{s.symbol:<6}" in rendered) == MAX_CONTEXT_SYMBOLS


def test_a_stock_without_a_sector_still_renders_its_market_comparison() -> None:
    lines = StockContext("AAPL", 1.0, "SPY", 0.3).render()
    assert any("vs SPY" in line for line in lines)
    assert not any("vs None" in line for line in lines)
