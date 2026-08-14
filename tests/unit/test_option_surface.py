"""The option collector must never invent a number it could not compute.

Everything derived here — ATM IV, a 30-day interpolation, a skew, an expected
move — is a number a future phase would treat as measured. So the tests that
matter are the ones asserting a `None` where the inputs are absent, rather than
the ones asserting arithmetic on a full surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.market_data.options import (
    ATM_MONEYNESS,
    MAX_DTE,
    MONEYNESS_WINDOW,
    ContractQuote,
    canonical_slice,
    parse_contract,
    summarise,
)

TODAY = date(2026, 8, 14)
SPOT = 100.0


def contract(
    *,
    days: int = 30,
    strike: float = 100.0,
    kind: str = "C",
    iv: float | None = 0.25,
    delta: float | None = 0.5,
    bid: float | None = 2.0,
    ask: float | None = 2.2,
) -> ContractQuote:
    expiry = TODAY + timedelta(days=days)
    return ContractQuote(
        occ_symbol=f"TEST{expiry:%y%m%d}{kind}{int(strike * 1000):08d}",
        expiration=expiry,
        strike=strike,
        option_type=kind,
        bid=bid,
        ask=ask,
        implied_volatility=iv,
        delta=delta,
        gamma=0.02,
        vega=0.1,
        theta=-0.05,
        open_interest=None,
    )


def straddle(days: int, iv: float, *, strike: float = 100.0) -> list[ContractQuote]:
    return [
        contract(days=days, strike=strike, kind="C", iv=iv, delta=0.5),
        contract(days=days, strike=strike, kind="P", iv=iv, delta=-0.5),
    ]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class FakeQuote:
    bid_price: float | None = 1.0
    ask_price: float | None = 1.2


@dataclass
class FakeGreeks:
    delta: float = 0.42
    gamma: float = 0.01
    vega: float = 0.2
    theta: float = -0.03


@dataclass
class FakeSnapshot:
    latest_quote: FakeQuote | None = None
    greeks: FakeGreeks | None = None
    implied_volatility: float | None = 0.33


def test_a_valid_occ_symbol_parses_completely() -> None:
    parsed = parse_contract("NVDA260918C00225000", FakeSnapshot(FakeQuote(), FakeGreeks()))
    assert parsed is not None
    assert parsed.expiration == date(2026, 9, 18)
    assert parsed.strike == pytest.approx(225.0)
    assert parsed.option_type == "C"
    assert parsed.delta == pytest.approx(0.42)
    assert parsed.implied_volatility == pytest.approx(0.33)


def test_an_unparseable_symbol_costs_one_contract_not_the_capture() -> None:
    assert parse_contract("NOT-AN-OCC-SYMBOL", FakeSnapshot()) is None


def test_missing_greeks_and_iv_become_none_not_zero() -> None:
    """21.5% of contracts on the indicative feed have no IV. Zero would be a lie."""
    parsed = parse_contract(
        "NVDA260918C00225000", FakeSnapshot(FakeQuote(), greeks=None, implied_volatility=None)
    )
    assert parsed is not None
    assert parsed.implied_volatility is None
    assert parsed.delta is None


def test_a_one_sided_market_has_no_mid() -> None:
    """Substituting the live side would create a price that never existed."""
    assert contract(bid=None, ask=2.0).mid is None
    assert contract(bid=2.0, ask=None).mid is None
    assert contract(bid=2.0, ask=2.4).mid == pytest.approx(2.2)


# ---------------------------------------------------------------------------
# The canonical slice
# ---------------------------------------------------------------------------
def test_the_slice_keeps_only_near_money_near_dated_contracts_with_iv() -> None:
    contracts = [
        contract(days=30, strike=100.0),
        contract(days=30, strike=150.0),  # far out of the money
        contract(days=MAX_DTE + 30, strike=100.0),  # too far out in time
        contract(days=30, strike=100.0, iv=None),  # no IV
        contract(days=-5, strike=100.0),  # expired
    ]
    kept = canonical_slice(contracts, spot=SPOT, as_of=TODAY)

    assert len(kept) == 1
    assert kept[0].strike == pytest.approx(100.0)


def test_the_slice_boundary_matches_the_documented_window() -> None:
    inside = contract(days=30, strike=SPOT * (1 + MONEYNESS_WINDOW - 0.001))
    outside = contract(days=30, strike=SPOT * (1 + MONEYNESS_WINDOW + 0.01))
    kept = canonical_slice([inside, outside], spot=SPOT, as_of=TODAY)
    assert [c.strike for c in kept] == [pytest.approx(inside.strike)]


# ---------------------------------------------------------------------------
# Derivation: the None cases are the point
# ---------------------------------------------------------------------------
def test_an_empty_chain_derives_nothing_and_says_so() -> None:
    summary = summarise([], spot=SPOT, as_of=TODAY)
    assert summary.atm_iv is None
    assert summary.iv_30d is None
    assert summary.skew_25d is None
    assert summary.term_slope is None
    assert summary.expected_move_pct is None
    assert summary.contracts_seen == 0


def test_atm_iv_averages_the_two_sides() -> None:
    summary = summarise(
        [
            contract(days=25, kind="C", iv=0.20, delta=0.5),
            contract(days=25, kind="P", iv=0.30, delta=-0.5),
        ],
        spot=SPOT,
        as_of=TODAY,
    )
    assert summary.atm_iv == pytest.approx(0.25)


def test_a_single_expiry_has_no_term_structure() -> None:
    """One point is not a slope, and reporting zero would imply a flat curve."""
    summary = summarise(straddle(25, 0.22), spot=SPOT, as_of=TODAY)
    assert summary.atm_iv is not None
    assert summary.term_slope is None


def test_term_slope_is_far_minus_near() -> None:
    summary = summarise([*straddle(20, 0.20), *straddle(50, 0.26)], spot=SPOT, as_of=TODAY)
    assert summary.term_slope == pytest.approx(0.06)


def test_thirty_day_iv_interpolates_only_when_thirty_is_bracketed() -> None:
    bracketed = summarise([*straddle(20, 0.20), *straddle(40, 0.30)], spot=SPOT, as_of=TODAY)
    assert bracketed.iv_30d == pytest.approx(0.25)


def test_thirty_day_iv_is_never_extrapolated() -> None:
    """**The rule.** Both expiries on one side must not produce a 30-day number."""
    only_near = summarise([*straddle(5, 0.20), *straddle(12, 0.22)], spot=SPOT, as_of=TODAY)
    only_far = summarise([*straddle(45, 0.20), *straddle(55, 0.22)], spot=SPOT, as_of=TODAY)

    assert only_near.iv_30d is None
    assert only_far.iv_30d is None
    assert only_near.atm_iv is not None  # the rest still derives


def test_skew_requires_both_wings() -> None:
    one_wing = summarise(
        [contract(days=25, kind="P", strike=95.0, iv=0.30, delta=-0.25)],
        spot=SPOT,
        as_of=TODAY,
    )
    assert one_wing.skew_25d is None

    both = summarise(
        [
            contract(days=25, kind="P", strike=95.0, iv=0.30, delta=-0.25),
            contract(days=25, kind="C", strike=105.0, iv=0.22, delta=0.25),
        ],
        spot=SPOT,
        as_of=TODAY,
    )
    assert both.skew_25d == pytest.approx(0.08)


def test_skew_sign_is_not_assumed_positive() -> None:
    """JPM measured a negative 25-delta skew. A model that clamps it is wrong."""
    summary = summarise(
        [
            contract(days=25, kind="P", strike=95.0, iv=0.19, delta=-0.25),
            contract(days=25, kind="C", strike=105.0, iv=0.22, delta=0.25),
        ],
        spot=SPOT,
        as_of=TODAY,
    )
    assert summary.skew_25d is not None
    assert summary.skew_25d < 0


def test_expected_move_needs_a_two_sided_straddle() -> None:
    one_sided = summarise([contract(days=25, kind="C", bid=None, ask=None)], spot=SPOT, as_of=TODAY)
    assert one_sided.expected_move_pct is None

    priced = summarise(
        [
            contract(days=25, kind="C", bid=2.0, ask=2.0, delta=0.5),
            contract(days=25, kind="P", bid=1.8, ask=1.8, delta=-0.5),
        ],
        spot=SPOT,
        as_of=TODAY,
    )
    assert priced.expected_move_pct == pytest.approx(3.8)


def test_counts_make_a_thin_capture_visible() -> None:
    """A drop in IV coverage means the feed changed, not that the market did."""
    summary = summarise([*straddle(25, 0.2), contract(days=25, iv=None)], spot=SPOT, as_of=TODAY)
    assert summary.contracts_seen == 3
    assert summary.contracts_with_iv == 2


def test_atm_window_excludes_distant_strikes() -> None:
    far = contract(days=25, strike=SPOT * (1 + ATM_MONEYNESS + 0.05), iv=0.9, delta=0.2)
    summary = summarise([*straddle(25, 0.2), far], spot=SPOT, as_of=TODAY)
    assert summary.atm_iv == pytest.approx(0.2)
