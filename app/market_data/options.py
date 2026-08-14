"""Capturing the option surface, because it cannot be backfilled later.

The asymmetry that justifies this module
----------------------------------------
Every other dataset in this project can be reconstructed on demand: candles,
corporate actions and fundamentals all have historical endpoints. Option chains
do not. Alpaca returns snapshots of *now*, the request object has no historical
"as of" parameter, and even the paid OPRA feed only carries option bars back to
February 2024.

So the cost of not running this today is not "we will do it later" -- it is a
permanent hole. That is the entire argument for writing it now, before there is
any evidence that implied volatility predicts anything.

What the free feed actually gives
---------------------------------
Measured on this account, not assumed: the ``indicative`` feed returns implied
volatility and greeks on **78.5%** of contracts (10,996 of 14,001 for SPY), full
bid/ask on 100%, and **no open interest at all**. The OPRA feed is refused
without a signed agreement.

Alpaca documents the indicative feed as "indicative derivatives" of quotes
rather than the consolidated OPRA best bid/offer. Everything captured through it
is therefore approximate, and ``feed`` is recorded on every row so a future
reader can separate the two rather than discovering a step change in the middle
of a series.

Nothing here is fabricated
--------------------------
Every derived field returns ``None`` when its inputs are absent. A 30-day IV is
interpolated only when expiries bracket 30 days -- never extrapolated from one
side -- and a skew is computed only when both wings exist. The brief's rule was
"never fabricate IV from missing inputs without explicitly labelling it
derived"; the stricter rule taken here is not to fabricate it at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, Protocol, runtime_checkable

from app.core.logging import get_logger

logger = get_logger(__name__)

OCC_SYMBOL: Final = re.compile(
    r"^(?P<root>[A-Z]+)(?P<expiry>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$"
)
"""OCC 21-character option symbol: root, YYMMDD, C/P, strike x 1000."""

MONEYNESS_WINDOW: Final = 0.10
"""How far from spot a contract may be and still enter the canonical slice.

Ten percent either side. Measured on this universe that keeps 283 contracts per
symbol per day instead of 2,598 -- a 9.2x reduction -- while retaining every
strike any ATM, skew or expected-move calculation reads. Wider adds deep wings
whose indicative quotes are least reliable.
"""

MAX_DTE: Final = 60
"""Longest expiry kept. The stated trading style is swing over 1-20 sessions, so
a LEAP contributes storage and nothing else."""

ATM_MONEYNESS: Final = 0.02
"""Distance from spot that counts as at-the-money for the derived summary."""

SKEW_DELTA: Final = 0.25
SKEW_TOLERANCE: Final = 0.10
"""Target delta for the skew wings, and how far a contract may sit from it.

Delta rather than a fixed strike offset: a 25-delta put is the same *risk*
distance on a 12%-vol utility and a 40%-vol semiconductor, and a fixed
percentage would not be.
"""

INTERPOLATION_TARGET_DAYS: Final = 30


@runtime_checkable
class OptionSurfaceProvider(Protocol):
    """A provider that can serve option chains.

    Deliberately **separate** from ``MarketDataProvider`` rather than bolted on
    to it. Option data is an optional capability -- the mock provider has none,
    and a narrow test double implementing candles should not have to grow an
    unrelated property to stay a valid provider. This mirrors how
    ``AssetCatalogue`` and ``BatchMarketDataProvider`` are already handled, and
    the first attempt at the alternative silently changed which code path
    ``import_service`` took for five test doubles.
    """

    @property
    def options_feed(self) -> str:
        """``indicative`` or ``opra`` -- recorded on every stored snapshot."""
        ...

    async def get_option_chain(self, symbol: str) -> dict[str, Any]: ...

    async def get_underlying_price(self, symbol: str) -> tuple[float, datetime | None]: ...


@dataclass(frozen=True, slots=True)
class ContractQuote:
    """One contract as captured, already parsed out of its OCC symbol."""

    occ_symbol: str
    expiration: date
    strike: float
    option_type: str
    bid: float | None
    ask: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    open_interest: int | None

    @property
    def mid(self) -> float | None:
        """Midpoint, or ``None`` if either side is missing.

        A one-sided market has no mid. Substituting the live side would create a
        price that never existed, which is the kind of number that survives into
        a backtest unchallenged.
        """
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0

    def days_to_expiry(self, as_of: date) -> int:
        return (self.expiration - as_of).days

    def moneyness(self, spot: float) -> float:
        return abs(self.strike / spot - 1.0) if spot else float("inf")


@dataclass(frozen=True, slots=True)
class SurfaceSummary:
    """The derived surface. Every field is optional and none is invented."""

    atm_iv: float | None = None
    iv_30d: float | None = None
    skew_25d: float | None = None
    term_slope: float | None = None
    expected_move_pct: float | None = None
    contracts_seen: int = 0
    contracts_with_iv: int = 0


def parse_contract(occ_symbol: str, snapshot: Any) -> ContractQuote | None:
    """Turn one chain entry into a :class:`ContractQuote`, or ``None``.

    Returns ``None`` for a symbol that does not parse rather than raising: an
    unfamiliar root should cost one contract, not the whole capture.
    """
    match = OCC_SYMBOL.match(occ_symbol)
    if match is None:
        return None

    quote = getattr(snapshot, "latest_quote", None)
    greeks = getattr(snapshot, "greeks", None)

    def greek(name: str) -> float | None:
        value = getattr(greeks, name, None) if greeks is not None else None
        return float(value) if value is not None else None

    try:
        expiration = datetime.strptime(match["expiry"], "%y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None

    return ContractQuote(
        occ_symbol=occ_symbol,
        expiration=expiration,
        strike=int(match["strike"]) / 1000.0,
        option_type=match["kind"],
        bid=float(quote.bid_price) if quote and quote.bid_price is not None else None,
        ask=float(quote.ask_price) if quote and quote.ask_price is not None else None,
        implied_volatility=(
            float(snapshot.implied_volatility)
            if getattr(snapshot, "implied_volatility", None) is not None
            else None
        ),
        delta=greek("delta"),
        gamma=greek("gamma"),
        vega=greek("vega"),
        theta=greek("theta"),
        open_interest=getattr(snapshot, "open_interest", None),
    )


def canonical_slice(
    contracts: list[ContractQuote], *, spot: float, as_of: date
) -> list[ContractQuote]:
    """The near-the-money, near-dated contracts worth storing.

    Requires an implied volatility: a contract with no IV contributes nothing
    the candle table does not already hold, and keeping it would triple the
    table to preserve a bid/ask nobody will study.
    """
    return [
        c
        for c in contracts
        if c.implied_volatility is not None
        and 0 < c.days_to_expiry(as_of) <= MAX_DTE
        and c.moneyness(spot) <= MONEYNESS_WINDOW
    ]


def _atm_iv_for_expiry(
    contracts: list[ContractQuote], *, spot: float, expiration: date
) -> float | None:
    """Mean call/put IV nearest the money for one expiry.

    Averaging the two sides rather than picking one: put-call parity means they
    should agree, and where they do not the average is the honest midpoint
    rather than a choice that silently embeds skew into an ATM number.
    """
    at_expiry = [
        c
        for c in contracts
        if c.expiration == expiration
        and c.implied_volatility is not None
        and c.moneyness(spot) <= ATM_MONEYNESS
    ]
    if not at_expiry:
        return None
    return sum(c.implied_volatility or 0.0 for c in at_expiry) / len(at_expiry)


def summarise(contracts: list[ContractQuote], *, spot: float, as_of: date) -> SurfaceSummary:
    """Derive the surface summary, leaving anything unsupported as ``None``."""
    with_iv = [c for c in contracts if c.implied_volatility is not None]
    dated = sorted({c.expiration for c in with_iv if c.days_to_expiry(as_of) > 0})
    if not dated:
        return SurfaceSummary(contracts_seen=len(contracts), contracts_with_iv=len(with_iv))

    front = dated[0]
    atm_iv = _atm_iv_for_expiry(with_iv, spot=spot, expiration=front)

    # Term slope needs two expiries. One expiry is not a term structure.
    term_slope = None
    if len(dated) >= 2:  # noqa: PLR2004
        far_iv = _atm_iv_for_expiry(with_iv, spot=spot, expiration=dated[-1])
        if atm_iv is not None and far_iv is not None:
            term_slope = far_iv - atm_iv

    # 30-day IV by linear interpolation in days, and only when 30 is bracketed.
    iv_30d = None
    before = [d for d in dated if (d - as_of).days <= INTERPOLATION_TARGET_DAYS]
    after = [d for d in dated if (d - as_of).days >= INTERPOLATION_TARGET_DAYS]
    if before and after:
        near, far = before[-1], after[0]
        near_iv = _atm_iv_for_expiry(with_iv, spot=spot, expiration=near)
        far_iv = _atm_iv_for_expiry(with_iv, spot=spot, expiration=far)
        if near_iv is not None and far_iv is not None:
            near_days, far_days = (near - as_of).days, (far - as_of).days
            if far_days == near_days:
                iv_30d = near_iv
            else:
                weight = (INTERPOLATION_TARGET_DAYS - near_days) / (far_days - near_days)
                iv_30d = near_iv + weight * (far_iv - near_iv)

    # 25-delta skew, front expiry, both wings required.
    def wing(kind: str, target: float) -> float | None:
        candidates = [
            c
            for c in with_iv
            if c.option_type == kind
            and c.expiration == front
            and c.delta is not None
            and abs(abs(c.delta) - target) <= SKEW_TOLERANCE
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda c: abs(abs(c.delta or 0.0) - target))
        return best.implied_volatility

    put_iv, call_iv = wing("P", SKEW_DELTA), wing("C", SKEW_DELTA)
    skew = put_iv - call_iv if put_iv is not None and call_iv is not None else None

    # Expected move: the front-expiry straddle mid as a percent of spot. A
    # magnitude, carrying no direction -- the same discipline volatility-v1 keeps.
    expected_move = None
    straddle = [
        c
        for c in with_iv
        if c.expiration == front and c.moneyness(spot) <= ATM_MONEYNESS and c.mid is not None
    ]
    calls = [c for c in straddle if c.option_type == "C"]
    puts = [c for c in straddle if c.option_type == "P"]
    if calls and puts and spot:
        call_mid = min(calls, key=lambda c: c.moneyness(spot)).mid or 0.0
        put_mid = min(puts, key=lambda c: c.moneyness(spot)).mid or 0.0
        expected_move = (call_mid + put_mid) / spot * 100.0

    return SurfaceSummary(
        atm_iv=atm_iv,
        iv_30d=iv_30d,
        skew_25d=skew,
        term_slope=term_slope,
        expected_move_pct=expected_move,
        contracts_seen=len(contracts),
        contracts_with_iv=len(with_iv),
    )


def as_decimal(value: float | None) -> Decimal | None:
    """Float to exact decimal for storage, preserving ``None``."""
    return None if value is None else Decimal(str(round(value, 6)))
