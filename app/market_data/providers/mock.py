"""Deterministic synthetic market-data provider.

Purpose: let the whole vertical slice run and be tested end-to-end without a paid
data subscription, and give tests a fixture that is byte-for-byte reproducible.

Determinism guarantees
----------------------
1. The RNG seed for a series is derived from ``(configured seed, symbol,
   timeframe)`` via CRC32 -- **not** Python's ``hash()``, which is salted per
   process and would make runs irreproducible.
2. Every series is generated forward from a fixed :data:`ORIGIN`. The requested
   window is a *slice* of that path, so asking for 2023 alone and asking for
   2015-2024 return identical bars for 2023.

What this is not
----------------
Synthetic geometric-Brownian-motion noise with a mild volatility cluster. It has
no earnings, no news, no real microstructure, and no exchange holidays. It is
adequate for exercising plumbing and for deterministic unit tests. It is **not**
adequate for evaluating whether a strategy works -- a strategy tuned on this data
has been tuned on a random number generator.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Final

import numpy as np
import numpy.typing as npt

from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.core.time import ensure_utc
from app.corporate_actions.models import CorporateAction
from app.domain.enums import AssetType, CorporateActionType, Timeframe
from app.domain.quotes import Quote
from app.market_data.provider import CandleData, InstrumentInfo

logger = get_logger(__name__)

ORIGIN: Final = datetime(2015, 1, 1, tzinfo=UTC)
"""Fixed start of every synthetic series. Never change this: it would silently
alter every previously generated bar."""

SESSION_OPEN: Final = time(13, 30)
SESSION_CLOSE: Final = time(20, 0)
"""Intraday bars are confined to 13:30-20:00 UTC, roughly a US cash session in
winter. DST is not modelled -- see the module docstring."""

MAX_BARS: Final = 500_000
PRICE_QUANTUM: Final = Decimal("0.0001")
VOLUME_QUANTUM: Final = Decimal("1")

# Arbitrary constant that decorrelates quote-size randomness from the price path
# while keeping it reproducible.
_QUOTE_SEED_MIX: Final = 0x5EED_0417

LISTING_EPOCH: Final = ORIGIN
"""Instruments without a more specific listing date are treated as listed from
the start of synthetic history."""


_UNIVERSE: Final[tuple[InstrumentInfo, ...]] = (
    InstrumentInfo(
        symbol="NVDA",
        name="NVIDIA Corporation",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        isin="US67066G1040",
        listed_at=LISTING_EPOCH,
    ),
    InstrumentInfo(
        symbol="AAPL",
        name="Apple Inc.",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        isin="US0378331005",
        listed_at=LISTING_EPOCH,
    ),
    InstrumentInfo(
        symbol="MSFT",
        name="Microsoft Corporation",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        isin="US5949181045",
        listed_at=LISTING_EPOCH,
    ),
    InstrumentInfo(
        symbol="SAP",
        name="SAP SE",
        exchange="XETR",
        currency="EUR",
        asset_type=AssetType.STOCK,
        isin="DE0007164600",
        listed_at=LISTING_EPOCH,
    ),
    InstrumentInfo(
        symbol="ASML",
        name="ASML Holding N.V.",
        exchange="XAMS",
        currency="EUR",
        asset_type=AssetType.STOCK,
        isin="NL0010273215",
        listed_at=LISTING_EPOCH,
    ),
    InstrumentInfo(
        symbol="IWDA",
        name="iShares Core MSCI World UCITS ETF",
        exchange="XAMS",
        currency="EUR",
        asset_type=AssetType.ETF,
        isin="IE00B4L5Y983",
        listed_at=LISTING_EPOCH,
    ),
    # A late lister and an instrument that has since been delisted. Both exist so
    # that point-in-time universe queries have something to get wrong: a naive
    # "select all instruments" returns them for every date, which is precisely
    # the survivorship/look-ahead error the universe layer must prevent.
    InstrumentInfo(
        symbol="LATE",
        name="Latecomer Industries",
        exchange="XNAS",
        currency="USD",
        asset_type=AssetType.STOCK,
        listed_at=datetime(2021, 6, 1, tzinfo=UTC),
    ),
    InstrumentInfo(
        symbol="OLDCO",
        name="Oldco Holdings (delisted)",
        exchange="XETR",
        currency="EUR",
        asset_type=AssetType.STOCK,
        listed_at=LISTING_EPOCH,
        delisted_at=datetime(2022, 9, 30, tzinfo=UTC),
    ),
)

# Per-symbol synthetic character: starting price, annual drift, annual vol,
# typical spread in bps. Chosen to span a realistic range (a wide-spread mid cap
# behaves very differently from a tight mega cap once costs are applied).
_PROFILES: Final[dict[str, tuple[float, float, float, float]]] = {
    "NVDA": (18.0, 0.35, 0.48, 3.0),
    "AAPL": (27.0, 0.18, 0.28, 2.0),
    "MSFT": (46.0, 0.20, 0.26, 2.0),
    "SAP": (58.0, 0.10, 0.25, 6.0),
    "ASML": (86.0, 0.22, 0.34, 7.0),
    "IWDA": (38.0, 0.08, 0.15, 4.0),
    "LATE": (12.0, 0.15, 0.40, 12.0),
    "OLDCO": (64.0, -0.12, 0.32, 18.0),
}

# Deterministic corporate actions. Declared here and *actually applied* to the
# generated raw prices below, so the mock's RAW series contains the same
# discontinuity a real provider would deliver. A mock that declared a split
# without producing the price jump would let a broken adjustment layer pass.
_CORPORATE_ACTIONS: Final[dict[str, tuple[CorporateAction, ...]]] = {
    "NVDA": (
        CorporateAction(
            symbol="NVDA",
            action_type=CorporateActionType.SPLIT,
            effective_at=datetime(2021, 7, 20, tzinfo=UTC),
            from_shares=Decimal(1),
            to_shares=Decimal(4),
            source="mock",
            external_id="mock-nvda-split-2021",
        ),
        CorporateAction(
            symbol="NVDA",
            action_type=CorporateActionType.SPLIT,
            effective_at=datetime(2024, 6, 10, tzinfo=UTC),
            from_shares=Decimal(1),
            to_shares=Decimal(10),
            source="mock",
            external_id="mock-nvda-split-2024",
        ),
    ),
    "AAPL": (
        CorporateAction(
            symbol="AAPL",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=datetime(2023, 2, 10, tzinfo=UTC),
            payment_at=datetime(2023, 2, 16, tzinfo=UTC),
            cash_amount=Decimal("0.23"),
            currency="USD",
            source="mock",
        ),
        CorporateAction(
            symbol="AAPL",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=datetime(2023, 5, 12, tzinfo=UTC),
            payment_at=datetime(2023, 5, 18, tzinfo=UTC),
            cash_amount=Decimal("0.24"),
            currency="USD",
            source="mock",
        ),
    ),
    "SAP": (
        CorporateAction(
            symbol="SAP",
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=datetime(2023, 5, 15, tzinfo=UTC),
            payment_at=datetime(2023, 5, 19, tzinfo=UTC),
            cash_amount=Decimal("2.05"),
            currency="EUR",
            source="mock",
        ),
    ),
    # A reverse split: the adjustment arithmetic must handle ratio < 1 with no
    # special case.
    "OLDCO": (
        CorporateAction(
            symbol="OLDCO",
            action_type=CorporateActionType.SPLIT,
            effective_at=datetime(2021, 3, 15, tzinfo=UTC),
            from_shares=Decimal(10),
            to_shares=Decimal(1),
            source="mock",
            external_id="mock-oldco-reverse-2021",
        ),
    ),
}

_DEFAULT_PROFILE: Final = (50.0, 0.08, 0.30, 8.0)
_BARS_PER_YEAR: Final[dict[Timeframe, float]] = {
    Timeframe.M1: 252 * 390,
    Timeframe.M5: 252 * 78,
    Timeframe.M15: 252 * 26,
    Timeframe.M30: 252 * 13,
    Timeframe.H1: 252 * 6.5,
    Timeframe.H4: 252 * 1.625,
    Timeframe.D1: 252,
    Timeframe.W1: 52,
}


class _Series:
    """A generated OHLCV path, cached so repeated requests are cheap."""

    __slots__ = ("close", "high", "low", "open", "timestamps", "volume")

    def __init__(
        self,
        timestamps: list[datetime],
        open_: npt.NDArray[np.float64],
        high: npt.NDArray[np.float64],
        low: npt.NDArray[np.float64],
        close: npt.NDArray[np.float64],
        volume: npt.NDArray[np.float64],
    ) -> None:
        self.timestamps = timestamps
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

    def __len__(self) -> int:
        return len(self.timestamps)


def _seed_for(base_seed: int, symbol: str, timeframe: Timeframe) -> int:
    """Stable seed for a series.

    CRC32 of the key, mixed with the configured base seed. Stable across
    processes and Python versions, unlike ``hash()``.
    """
    key = f"{symbol}:{timeframe.value}".encode()
    return (base_seed * 2_654_435_761 + zlib.crc32(key)) % (2**32)


def _bar_timestamps(timeframe: Timeframe, end: datetime) -> list[datetime]:
    """Grid of bar open times in ``[ORIGIN, end)``.

    Weekends are skipped for every timeframe; intraday bars are additionally
    restricted to the synthetic session window. The resulting gaps are on purpose:
    downstream code must cope with non-contiguous bars, and mock data that is
    perfectly contiguous hides those bugs until real data arrives.
    """
    stamps: list[datetime] = []
    step = timeframe.duration

    if timeframe is Timeframe.W1:
        cursor = ORIGIN + timedelta(days=(7 - ORIGIN.weekday()) % 7)  # first Monday
        while cursor < end:
            stamps.append(cursor)
            cursor += timedelta(days=7)
        return stamps

    if timeframe is Timeframe.D1:
        cursor = ORIGIN
        while cursor < end:
            if cursor.weekday() < 5:  # noqa: PLR2004 -- Mon-Fri
                stamps.append(cursor)
            cursor += timedelta(days=1)
        return stamps

    day = ORIGIN
    while day < end:
        if day.weekday() < 5:  # noqa: PLR2004 -- Mon-Fri
            cursor = day.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute)
            session_end = day.replace(hour=SESSION_CLOSE.hour, minute=SESSION_CLOSE.minute)
            while cursor + step <= session_end:
                if cursor >= end:
                    return stamps
                stamps.append(cursor)
                cursor += step
        day += timedelta(days=1)
        if len(stamps) > MAX_BARS:
            msg = (
                f"mock provider would generate more than {MAX_BARS} bars for "
                f"{timeframe.value}; narrow the requested window"
            )
            raise ProviderError(msg)
    return stamps


def _generate(symbol: str, timeframe: Timeframe, end: datetime, seed: int) -> _Series:
    """Generate the full synthetic path from :data:`ORIGIN` to ``end``."""
    stamps = _bar_timestamps(timeframe, end)
    n = len(stamps)
    if n == 0:
        empty = np.empty(0, dtype=np.float64)
        return _Series([], empty, empty, empty, empty, empty)

    start_price, annual_drift, annual_vol, _ = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    bars_per_year = _BARS_PER_YEAR[timeframe]
    dt = 1.0 / bars_per_year

    rng = np.random.default_rng(seed)

    # Volatility clustering: an AR(1) process on log-vol, so quiet and stormy
    # regimes alternate instead of vol being constant.
    # The shock scale is chosen against the AR(1) stationary std,
    # sigma / sqrt(1 - persistence^2) ~= 0.17 in log terms, so realised vol
    # wanders roughly within [0.6x, 1.7x] of the symbol's baseline instead of
    # drifting to implausible levels.
    log_vol = np.empty(n, dtype=np.float64)
    log_vol[0] = np.log(annual_vol)
    shocks = rng.normal(0.0, 0.03, size=n)
    persistence = 0.985
    mean_log_vol = np.log(annual_vol)
    for i in range(1, n):
        log_vol[i] = mean_log_vol + persistence * (log_vol[i - 1] - mean_log_vol) + shocks[i]
    vol = np.exp(log_vol)

    # Geometric Brownian motion with time-varying volatility.
    z = rng.normal(0.0, 1.0, size=n)
    log_returns = (annual_drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * z
    close = start_price * np.exp(np.cumsum(log_returns))

    # Open is the previous close plus a small overnight/inter-bar gap.
    gap = rng.normal(0.0, 0.15, size=n) * vol * np.sqrt(dt)
    open_ = np.empty(n, dtype=np.float64)
    open_[0] = start_price
    open_[1:] = close[:-1] * np.exp(gap[1:])

    # Intrabar extremes: extend beyond the open/close range by a positive amount.
    bar_range = np.abs(rng.normal(0.0, 1.0, size=n)) * vol * np.sqrt(dt)
    body_high = np.maximum(open_, close)
    body_low = np.minimum(open_, close)
    high = body_high * (1.0 + bar_range * rng.uniform(0.2, 1.0, size=n))
    low = body_low * (1.0 - bar_range * rng.uniform(0.2, 1.0, size=n))

    # Volume: lognormal, positively correlated with absolute return so that
    # relative-volume features have something non-trivial to detect.
    base_volume = 1_000_000.0 / max(bars_per_year / 252.0, 1.0)
    activity = 1.0 + 3.0 * np.abs(log_returns) / (vol * np.sqrt(dt) + 1e-12) * 0.25
    volume = base_volume * activity * np.exp(rng.normal(0.0, 0.35, size=n))

    # The path above is the *economic* value of the holding, which a split does
    # not change. Convert it into quoted prices by dividing out the splits that
    # have already happened by each bar.
    price_scale, volume_scale = _raw_split_scales(symbol, stamps)
    return _Series(
        stamps,
        open_ * price_scale,
        high * price_scale,
        low * price_scale,
        close * price_scale,
        np.round(volume * volume_scale),
    )


def _raw_split_scales(
    symbol: str, stamps: list[datetime]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Per-bar scales converting a continuous value path into quoted prices.

    A split does not change what a holding is worth -- it changes how that value
    is divided into shares. So the raw quoted price at bar *t* is the continuous
    path divided by the cumulative split ratio in force at *t*, and the quoted
    volume is multiplied by it.

    Applying this here is what makes the mock a genuine test of the adjustment
    layer: ``adjust_candles`` run over these raw bars must reproduce the original
    continuous path, up to a constant scale.
    """
    n = len(stamps)
    price_scale = np.ones(n, dtype=np.float64)
    volume_scale = np.ones(n, dtype=np.float64)

    splits = [
        action
        for action in _CORPORATE_ACTIONS.get(symbol, ())
        if action.action_type is CorporateActionType.SPLIT
    ]
    if not splits:
        return price_scale, volume_scale

    for split in splits:
        ratio = float(split.split_ratio)
        # Bars at or after the effective instant quote the post-split share.
        after = np.fromiter((stamp >= split.effective_at for stamp in stamps), dtype=bool, count=n)
        price_scale[after] /= ratio
        volume_scale[after] *= ratio

    return price_scale, volume_scale


class MockMarketDataProvider:
    """Deterministic in-process :class:`~app.market_data.provider.MarketDataProvider`.

    Args:
        seed: base RNG seed. Two providers with the same seed emit identical data.
        universe: override the instrument list (useful in tests).
    """

    def __init__(
        self,
        seed: int = 1337,
        universe: tuple[InstrumentInfo, ...] = _UNIVERSE,
    ) -> None:
        self._seed = seed
        self._universe = universe
        self._by_symbol = {info.symbol: info for info in universe}
        self._cache: dict[tuple[str, Timeframe], _Series] = {}
        self._cache_end: dict[tuple[str, Timeframe], datetime] = {}

    @property
    def name(self) -> str:
        return "mock"

    async def get_instruments(self) -> list[InstrumentInfo]:
        return list(self._universe)

    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[CandleData]:
        """Closed bars in ``[start, end)``, ascending.

        The window is clamped to the instrument's listing lifecycle: a delisted
        instrument stops producing bars at its delisting, and a late lister
        produces none before it existed. Real providers behave this way, and a
        mock that kept emitting prices for a dead company would hide exactly the
        survivorship-bias bugs the universe layer exists to prevent.
        """
        symbol = symbol.upper()
        self._require_known(symbol)
        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            msg = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            raise ProviderError(msg)

        info = self._by_symbol[symbol]
        if info.listed_at is not None:
            start = max(start, info.listed_at)
        if info.delisted_at is not None:
            end = min(end, info.delisted_at)
        if start >= end:
            return []

        series = self._series(symbol, timeframe, end)
        stamps = series.timestamps
        lo = _bisect_left(stamps, start)
        hi = _bisect_left(stamps, end)

        return [
            CandleData(
                timestamp=stamps[i],
                open=_price(series.open[i]),
                high=_price(series.high[i]),
                low=_price(series.low[i]),
                close=_price(series.close[i]),
                volume=Decimal(int(series.volume[i])).quantize(VOLUME_QUANTUM),
                trade_count=int(series.volume[i] // 100) or None,
                vwap=_price((series.high[i] + series.low[i] + series.close[i]) / 3.0),
            )
            for i in range(lo, hi)
        ]

    async def get_corporate_actions(self, symbol: str) -> list[CorporateAction]:
        """Deterministic corporate actions, ascending by effective time."""
        symbol = symbol.upper()
        self._require_known(symbol)
        return sorted(_CORPORATE_ACTIONS.get(symbol, ()), key=lambda a: a.effective_at)

    async def get_historical_candles_batch(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[CandleData]]:
        """Bars for many symbols.

        Implemented by looping, because the mock has no request to batch -- it
        generates deterministically. The point is that the *batch code path* is
        exercisable offline, so a test of the sync logic does not need Alpaca.
        """
        return {
            symbol.upper(): await self.get_historical_candles(symbol, timeframe, start, end)
            for symbol in symbols
        }

    async def get_latest_quote(self, symbol: str) -> Quote:
        """Synthetic top-of-book derived from the most recent close.

        Raises:
            ProviderError: the instrument is delisted -- there is no live market.
        """
        symbol = symbol.upper()
        self._require_known(symbol)

        now = datetime.now(UTC)
        info = self._by_symbol[symbol]
        if info.delisted_at is not None and now >= info.delisted_at:
            msg = (
                f"{symbol} was delisted at {info.delisted_at.isoformat()}; "
                f"there is no current quote"
            )
            raise ProviderError(msg)
        series = self._series(symbol, Timeframe.D1, now)
        if len(series) == 0:
            msg = f"no synthetic history available for {symbol}"
            raise ProviderError(msg)

        mid = float(series.close[-1])
        spread_bps = _PROFILES.get(symbol, _DEFAULT_PROFILE)[3]
        half_spread = mid * spread_bps / 20_000.0

        # Sizes are deterministic per symbol so quote-dependent tests stay stable.
        rng = np.random.default_rng(_seed_for(self._seed, symbol, Timeframe.D1) ^ _QUOTE_SEED_MIX)
        return Quote(
            symbol=symbol,
            timestamp=now,
            bid=_price(mid - half_spread),
            ask=_price(mid + half_spread),
            bid_size=Decimal(int(rng.integers(100, 5000))),
            ask_size=Decimal(int(rng.integers(100, 5000))),
        )

    # -- internals ---------------------------------------------------------

    def _require_known(self, symbol: str) -> None:
        if symbol not in self._by_symbol:
            msg = (
                f"unknown symbol {symbol!r} for the mock provider; "
                f"available: {', '.join(sorted(self._by_symbol))}"
            )
            raise ProviderError(msg)

    def _series(self, symbol: str, timeframe: Timeframe, end: datetime) -> _Series:
        """Return the cached path, regenerating when a later ``end`` is needed.

        Regeneration is safe because generation always restarts from
        :data:`ORIGIN` with the same seed: the overlapping prefix is identical.
        """
        key = (symbol, timeframe)
        cached_end = self._cache_end.get(key)
        if cached_end is None or end > cached_end:
            logger.debug("generating synthetic series", symbol=symbol, timeframe=timeframe.value)
            seed = _seed_for(self._seed, symbol, timeframe)
            self._cache[key] = _generate(symbol, timeframe, end, seed)
            self._cache_end[key] = end
        return self._cache[key]


def _price(value: float) -> Decimal:
    """Convert a synthetic float price into a quantised Decimal.

    This is the mock provider's float -> Decimal boundary. Real providers deliver
    strings or decimals and must not go through a float at all.
    """
    return Decimal(str(round(float(value), 4))).quantize(PRICE_QUANTUM)


def _bisect_left(stamps: list[datetime], target: datetime) -> int:
    """Index of the first timestamp >= ``target``."""
    lo, hi = 0, len(stamps)
    while lo < hi:
        mid = (lo + hi) // 2
        if stamps[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo
