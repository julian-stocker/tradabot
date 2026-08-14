"""Alpaca market-data provider.

The only module in tradabot that knows Alpaca exists. It implements the
:class:`~app.market_data.provider.MarketDataProvider` protocol, so everything
downstream -- features, signals, simulation, paper broker -- is unchanged by
switching providers (coding rule 12).

SDK
---
`alpaca-py <https://github.com/alpacahq/alpaca-py>`_, the official SDK.
``alpaca-trade-api`` is deprecated and its repository archived read-only; it is
deliberately not used.

**alpaca-py's REST clients are synchronous.** tradabot's provider protocol is
async, so every call is dispatched through :func:`asyncio.to_thread`. Wrapping a
blocking client is honest about what it is; pretending it is async by calling it
directly on the event loop would stall every other request in the process.

Raw bars, always
----------------
Bars are requested with ``Adjustment.RAW``. tradabot stores what the provider
actually reported and applies split adjustment on read (phase 2), so asking
Alpaca to pre-adjust would produce a series that silently disagrees with the
adjustment layer -- and there would be no way to tell which had been applied.

Market data is not broker execution data
----------------------------------------
An Alpaca quote is *reference* market data. It is not the price a retail broker
would fill you at: venue, routing, payment for order flow and retail spread
markup all differ. See docs/market-data.md. The distinction is configuration
(``treat_provider_quote_as_executable``), not an assumption buried in code.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, Final, TypeVar

from app.core.config import AlpacaSettings, MarketDataSettings
from app.core.errors import ConfigurationError, ProviderError
from app.core.logging import get_logger
from app.core.redaction import first_line as _first_line
from app.core.redaction import safe_message as _safe_message
from app.core.time import ensure_utc, utc_now
from app.corporate_actions.models import CorporateAction
from app.domain.enums import AssetType, CorporateActionType, Timeframe
from app.domain.quotes import Quote
from app.market_data.provider import AssetMetadata, CandleData, InstrumentInfo
from app.market_data.quality import NormalisationReport

logger = get_logger(__name__)

_MIC_BY_ALPACA_EXCHANGE: Final[dict[str, str]] = {
    "NYSE": "XNYS",
    "NASDAQ": "XNAS",
    "ARCA": "ARCX",
    "AMEX": "XASE",
    "BATS": "BATS",
    "OTC": "OTCM",
}
"""Alpaca's exchange names to ISO 10383 MICs.

The MIC is the provider-neutral key: `exchange-calendars` takes one, and a
future Xetra or Tokyo provider reports XETR/XTKS rather than a vendor string.
Storing Alpaca's own spelling would make the column mean "what Alpaca called it",
which is not a fact about the instrument.
"""


def _mic_for(exchange: object) -> str | None:
    """Map a provider exchange to a MIC, or None when unrecognised.

    None rather than a default: defaulting is exactly how every instrument came
    to claim XNAS.
    """
    if exchange is None:
        return None
    raw = getattr(exchange, "value", exchange)
    return _MIC_BY_ALPACA_EXCHANGE.get(str(raw).upper())


PROVIDER_NAME: Final = "alpaca"

OPTIONS_FEED: Final = "indicative"
"""The only option feed this account can read; OPRA requires a signed agreement.

Recorded on every stored snapshot. Alpaca documents the indicative feed as a
derived approximation of quotes rather than the consolidated OPRA best
bid/offer, so a series that silently mixed the two would carry an unexplained
step change.
"""

T = TypeVar("T")

# Alpaca timeframe units are constructed lazily so importing this module does not
# require the SDK to be installed until a client is actually built.
_TIMEFRAME_SPEC: Final[dict[Timeframe, tuple[int, str]]] = {
    Timeframe.M1: (1, "Min"),
    Timeframe.M5: (5, "Min"),
    Timeframe.M15: (15, "Min"),
    Timeframe.M30: (30, "Min"),
    Timeframe.H1: (1, "Hour"),
    Timeframe.H4: (4, "Hour"),
    Timeframe.D1: (1, "Day"),
    Timeframe.W1: (1, "Week"),
}

HTTP_UNAUTHORIZED: Final = 401
HTTP_FORBIDDEN: Final = 403
HTTP_NOT_FOUND: Final = 404
HTTP_UNPROCESSABLE: Final = 422
HTTP_CLIENT_ERROR_FLOOR: Final = 400
HTTP_SERVER_ERROR_FLOOR: Final = 500

_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
"""Rate limiting and transient server faults. Everything else -- 401, 403, 404,
422 -- is a request that will fail identically no matter how often it is retried,
and retrying it just delays the error report."""


class AlpacaAuthenticationError(ProviderError):
    """Credentials were rejected. Never retried, and never echoes the key."""


class AlpacaRateLimitError(ProviderError):
    """Rate limited after exhausting the retry budget."""


class MarketDataProviderUnavailableError(ProviderError):
    """The provider could not be reached, or is not configured."""


class AlpacaMarketDataProvider:
    """Alpaca implementation of the market-data provider protocol.

    Args:
        settings: credentials, feed and retry policy.
        market_data: provider-independent behaviour (default exchange).
        clients: injected SDK clients, for tests. Real clients are built lazily
            so importing this module never requires credentials.
    """

    def __init__(
        self,
        settings: AlpacaSettings,
        market_data: MarketDataSettings | None = None,
        *,
        stock_client: Any = None,
        corporate_actions_client: Any = None,
        option_client: Any = None,
    ) -> None:
        self._settings = settings
        self._market_data = market_data or MarketDataSettings()
        self._stock_client = stock_client
        self._corporate_actions_client = corporate_actions_client
        self._option_client = option_client
        self._last_success: datetime | None = None
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def last_successful_request(self) -> datetime | None:
        return self._last_success

    @property
    def last_error(self) -> str | None:
        """Last failure message. Never contains a credential."""
        return self._last_error

    # -- Client construction ----------------------------------------------

    def _require_credentials(self) -> None:
        if not self._settings.is_configured:
            msg = (
                "Alpaca credentials are not configured. Set ALPACA_API_KEY and "
                "ALPACA_API_SECRET, or run with TRADABOT_MARKET_DATA_PROVIDER=mock."
            )
            raise ConfigurationError(msg)

    def _stock(self) -> Any:
        if self._stock_client is None:
            self._require_credentials()
            from alpaca.data.historical import StockHistoricalDataClient

            self._stock_client = StockHistoricalDataClient(
                api_key=self._settings.api_key.get_secret_value(),
                secret_key=self._settings.api_secret.get_secret_value(),
            )
        return self._stock_client

    def _corporate_actions(self) -> Any:
        if self._corporate_actions_client is None:
            self._require_credentials()
            from alpaca.data.historical.corporate_actions import CorporateActionsClient

            self._corporate_actions_client = CorporateActionsClient(
                api_key=self._settings.api_key.get_secret_value(),
                secret_key=self._settings.api_secret.get_secret_value(),
            )
        return self._corporate_actions_client

    def _options(self) -> Any:
        if self._option_client is None:
            self._require_credentials()
            from alpaca.data.historical.option import OptionHistoricalDataClient

            self._option_client = OptionHistoricalDataClient(
                api_key=self._settings.api_key.get_secret_value(),
                secret_key=self._settings.api_secret.get_secret_value(),
            )
        return self._option_client

    # -- Options -----------------------------------------------------------

    @property
    def options_feed(self) -> str:
        """Which option feed this account actually reads.

        ``indicative`` unless an OPRA agreement is signed. Exposed so every
        stored snapshot records its provenance: the indicative feed is Alpaca's
        derived approximation of quotes rather than the consolidated OPRA best
        bid/offer, and mixing the two in one series without a marker would put
        an unexplained step change in the middle of it.
        """
        return OPTIONS_FEED

    async def get_option_chain(self, symbol: str) -> dict[str, Any]:
        """Every listed contract for ``symbol``, as provider snapshots.

        Read-only, and deliberately the *data* client: this class never imports
        an Alpaca trading client, so no order can be placed from here.
        """
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionChainRequest

        client = self._options()
        request = OptionChainRequest(
            underlying_symbol=symbol.upper(), feed=OptionsFeed(OPTIONS_FEED)
        )
        result = await self._call(lambda: client.get_option_chain(request), "get_option_chain")
        return dict(result)

    async def get_underlying_price(self, symbol: str) -> tuple[float, datetime | None]:
        """Latest traded price for the underlying, with its timestamp.

        The timestamp is returned rather than discarded so the caller can refuse
        a stale spot. Every moneyness and delta in a surface is quoted relative
        to this number, so a stale one shifts the whole capture silently.
        """
        from alpaca.data.requests import StockLatestTradeRequest

        client = self._stock()
        request = StockLatestTradeRequest(symbol_or_symbols=symbol.upper())
        result = await self._call(
            lambda: client.get_stock_latest_trade(request), "get_stock_latest_trade"
        )
        trade = result[symbol.upper()]
        return float(trade.price), getattr(trade, "timestamp", None)

    # -- Protocol ----------------------------------------------------------

    async def get_instruments(self) -> list[InstrumentInfo]:
        """The configured watchlist as instrument metadata.

        Metadata here is a **placeholder**, and knowing that matters: ``name`` is
        the ticker and ``exchange`` is the configured default, so every instrument
        seeded through this path claims to be on XNAS whether or not it is. JPM,
        KO and XOM are NYSE-listed and were all recorded as XNAS.

        The real catalogue lives behind Alpaca's *trading* API. See
        :meth:`get_asset_metadata`, which reads it deliberately and separately --
        this method stays credential-light so seeding works with market-data
        access alone.
        """
        return [
            InstrumentInfo(
                symbol=symbol,
                name=symbol,
                exchange=self._market_data.default_exchange,
                currency="USD",
                asset_type=AssetType.STOCK,
            )
            for symbol in self._market_data.watchlist
        ]

    async def get_asset_metadata(self, symbols: Sequence[str]) -> dict[str, AssetMetadata]:
        """Authoritative name, exchange and tradability, from Alpaca's asset catalogue.

        **Read-only, and separate from everything else on purpose.** This is the
        only method in the codebase that touches the trading API, and it calls
        exactly one endpoint: ``GET /v2/assets/{symbol}``. It cannot place,
        modify or cancel an order -- no order type is imported here and none is
        reachable from this client. tradabot still submits no orders anywhere.

        It is separate because seeding must keep working with market-data
        credentials alone; identity enrichment is an explicit, opt-in step
        (``tradabot market-data refresh-identity``).

        A symbol the catalogue does not know is omitted rather than guessed --
        the whole point is to stop inventing metadata.
        """
        from alpaca.trading.client import TradingClient

        client = TradingClient(
            api_key=self._settings.api_key.get_secret_value(),
            secret_key=self._settings.api_secret.get_secret_value(),
            paper=True,
        )

        found: dict[str, AssetMetadata] = {}
        for symbol in symbols:
            try:
                asset = await self._call(partial(client.get_asset, symbol.upper()), "get_asset")
            except ProviderError as exc:
                logger.warning(
                    "asset lookup failed", symbol=symbol.upper(), error=_safe_message(exc)
                )
                continue
            found[symbol.upper()] = AssetMetadata(
                symbol=symbol.upper(),
                name=str(getattr(asset, "name", "") or "") or None,
                exchange=_mic_for(getattr(asset, "exchange", None)),
                tradable=bool(getattr(asset, "tradable", False)),
            )
        return found

    async def get_historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[CandleData]:
        """Closed bars for ``symbol`` in ``[start, end)``, ascending.

        Raises:
            ProviderError: on an unsupported timeframe, an invalid window, or a
                provider failure that survived the retry budget.
        """
        symbol = symbol.upper()
        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            msg = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            raise ProviderError(msg)

        request = self._bars_request(symbol, timeframe, start, end)
        client = self._stock()
        response = await self._call(lambda: client.get_stock_bars(request), "get_stock_bars")

        raw_bars = _extract_bars(response, symbol)
        candles, report = normalise_bars(raw_bars, symbol=symbol, end=end)
        if report.rejected:
            logger.warning(
                "rejected malformed bars",
                provider=PROVIDER_NAME,
                symbol=symbol,
                rejected=len(report.rejected),
                first_reason=report.rejected[0].reason,
            )
        return candles

    async def get_historical_candles_batch(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[CandleData]]:
        """Bars for many symbols in **one** request.

        Alpaca's bars endpoint accepts a list, and using it is not an
        optimisation so much as a correctness requirement at this scale: a
        52-symbol universe across four timeframes is 208 sequential requests,
        which both approaches the Basic plan's 200-per-minute ceiling and takes
        longer than the five-minute interval the sync is scheduled at. Batched,
        the same work is four requests.

        A symbol the provider returns nothing for maps to an empty list rather
        than being absent, so a caller can tell "asked and got nothing" from
        "never asked".

        Raises:
            ProviderError: unsupported timeframe, invalid window, or a failure
                that survived the retry budget. One bad *symbol* does not raise --
                it simply has no bars.
        """
        wanted = [s.upper() for s in symbols]
        if not wanted:
            return {}

        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            msg = f"start ({start.isoformat()}) must be before end ({end.isoformat()})"
            raise ProviderError(msg)

        results: dict[str, list[CandleData]] = {symbol: [] for symbol in wanted}

        client = self._stock()
        for chunk in _chunked(wanted, self._settings.max_symbols_per_request):
            request = self._bars_request(chunk, timeframe, start, end)
            response = await self._call(partial(client.get_stock_bars, request), "get_stock_bars")
            for symbol in chunk:
                raw_bars = _extract_bars(response, symbol)
                candles, report = normalise_bars(raw_bars, symbol=symbol, end=end)
                results[symbol] = candles
                if report.rejected:
                    logger.warning(
                        "rejected malformed bars",
                        provider=PROVIDER_NAME,
                        symbol=symbol,
                        rejected=len(report.rejected),
                        first_reason=report.rejected[0].reason,
                    )

        return results

    async def get_latest_quote(self, symbol: str) -> Quote:
        """Most recent top-of-book quote.

        The provider's own timestamp is preserved, never replaced with "now" --
        that timestamp is what the stale-quote check depends on, and overwriting
        it would make every quote look fresh.
        """
        symbol = symbol.upper()
        request = self._quote_request(symbol)
        client = self._stock()
        response = await self._call(
            lambda: client.get_stock_latest_quote(request), "get_stock_latest_quote"
        )

        raw = _extract_quote(response, symbol)
        if raw is None:
            msg = f"Alpaca returned no quote for {symbol}"
            raise ProviderError(msg)
        return normalise_quote(raw, symbol=symbol)

    async def get_corporate_actions(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[CorporateAction]:
        """Splits and cash dividends for ``symbol`` in ``[start, end]``.

        Alpaca exposes typed corporate actions -- ``ForwardSplit``,
        ``ReverseSplit``, ``CashDividend`` -- which map cleanly onto tradabot's
        existing domain. Types it reports that tradabot does not yet adjust for
        (mergers, spin-offs, name changes) are **skipped rather than stored**:
        recording an action the adjustment layer ignores would produce a series
        that looks handled and is not.

        **The window matters more than it looks.** Alpaca's corporate-actions
        endpoint defaults to roughly the current month, so calling this without
        dates returned one action across 62 instruments -- indistinguishable from
        a universe in which nobody ever split. Every historical split this
        database needs sits years before that default.
        """
        symbol = symbol.upper()
        request = self._corporate_actions_request(symbol, start=start, end=end)
        client = self._corporate_actions()
        response = await self._call(
            lambda: client.get_corporate_actions(request), "get_corporate_actions"
        )
        return normalise_corporate_actions(response, symbol=symbol)

    # -- Request construction ---------------------------------------------

    def _bars_request(
        self,
        symbol: str | Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Any:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        spec = _TIMEFRAME_SPEC.get(timeframe)
        if spec is None:
            msg = f"Alpaca does not support timeframe {timeframe.value}"
            raise ProviderError(msg)
        amount, unit = spec

        return StockBarsRequest(
            symbol_or_symbols=list(symbol) if not isinstance(symbol, str) else symbol,
            timeframe=TimeFrame(amount, TimeFrameUnit(unit)),
            start=start,
            end=end,
            # **No limit.** Alpaca applies `limit` to the whole response, not per
            # symbol, and truncates by dropping symbols entirely rather than by
            # shortening each series. Measured: one 52-symbol 5-minute request for
            # June 2026 returned 10,000 bars covering **6 symbols**; the other 46
            # came back with nothing at all, and no error. A backfill built on
            # that would silently store a dataset missing 88% of the universe.
            #
            # The response is bounded by the *date window* instead -- see
            # `app.market_data.backfill.CHUNK_DAYS` -- which bounds it honestly.
            limit=None,
            # RAW: tradabot stores what traded and adjusts on read.
            adjustment=Adjustment.RAW,
            feed=DataFeed(self._settings.feed),
        )

    def _quote_request(self, symbol: str) -> Any:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest

        return StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed(self._settings.feed))

    def _corporate_actions_request(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Any:
        from alpaca.data.enums import CorporateActionsType
        from alpaca.data.requests import CorporateActionsRequest

        # The SDK wants plain dates here, and rejects `None` for one bound while
        # the other is set -- so both are passed or neither is.
        window: dict[str, Any] = {}
        if start is not None and end is not None:
            window = {"start": start.date(), "end": end.date()}

        return CorporateActionsRequest(
            symbols=[symbol],
            types=[
                CorporateActionsType.FORWARD_SPLIT,
                CorporateActionsType.REVERSE_SPLIT,
                CorporateActionsType.CASH_DIVIDEND,
            ],
            **window,
        )

    # -- Transport ---------------------------------------------------------

    async def _call(self, operation: Callable[[], T], label: str) -> T:
        """Run a blocking SDK call off the event loop, with bounded retries.

        Retries only what can plausibly succeed on a second attempt: rate limits
        and 5xx. Authentication and malformed-request failures are raised
        immediately -- retrying a 401 four times just delays telling the operator
        their key is wrong.

        Backoff is exponential with full jitter. Without jitter, several symbols
        rate-limited at once retry in lockstep and rate-limit each other again.
        """
        attempts = self._settings.max_retries + 1
        delay = self._settings.backoff_base_seconds
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(operation),
                    timeout=self._settings.request_timeout_seconds,
                )
            except TimeoutError as exc:
                last_error = exc
                self._last_error = f"{label}: request timed out"
                if attempt == attempts:
                    msg = (
                        f"Alpaca {label} timed out after "
                        f"{self._settings.request_timeout_seconds}s "
                        f"({attempts} attempts)"
                    )
                    raise MarketDataProviderUnavailableError(msg) from exc
            except Exception as exc:
                classified = _classify(exc, label)
                if classified is not None:
                    self._last_error = str(classified)
                    raise classified from exc
                last_error = exc
                self._last_error = f"{label}: {_safe_message(exc)}"
                if attempt == attempts:
                    msg = f"Alpaca {label} failed after {attempts} attempts: {_safe_message(exc)}"
                    raise ProviderError(msg) from exc
            else:
                self._last_success = utc_now()
                self._last_error = None
                return result

            sleep_for = min(delay, self._settings.backoff_max_seconds)
            jittered = random.uniform(0, sleep_for)
            retry_after = _retry_after(last_error)
            wait = retry_after if retry_after is not None else jittered
            logger.warning(
                "retrying provider call",
                provider=PROVIDER_NAME,
                operation=label,
                attempt=attempt,
                of=attempts,
                wait_seconds=round(wait, 2),
                error=_safe_message(last_error) if last_error else "timeout",
            )
            await asyncio.sleep(wait)
            delay *= 2

        # Unreachable: the final attempt either returns or raises above.
        msg = f"Alpaca {label} exhausted its retry budget"
        raise ProviderError(msg)


# ---------------------------------------------------------------------------
# Normalisation -- Alpaca DTOs in, tradabot domain out.
# ---------------------------------------------------------------------------
def normalise_bars(
    raw_bars: list[Any], *, symbol: str, end: datetime | None = None
) -> tuple[list[CandleData], NormalisationReport]:
    """Convert Alpaca bars into validated :class:`CandleData`.

    Malformed bars are **rejected and reported**, not silently repaired.
    ``CandleData`` already enforces OHLC consistency and non-negative volume, so
    this function's job is to translate, catch the rejections, and record why.

    Args:
        raw_bars: Alpaca ``Bar`` objects (or anything with the same attributes).
        symbol: for error messages.
        end: exclusive window end. Alpaca's ``end`` is inclusive, so a bar landing
            exactly on it is dropped to preserve tradabot's half-open ``[start, end)``
            convention -- otherwise consecutive requests would duplicate a bar.

    Returns:
        Accepted candles (ascending, deduplicated) and a report of what was not.
    """
    report = NormalisationReport(symbol=symbol)
    accepted: dict[datetime, CandleData] = {}

    for raw in raw_bars:
        try:
            timestamp = ensure_utc(_require(raw, "timestamp"))
        except (ValueError, AttributeError, TypeError) as exc:
            report.reject(None, f"unusable timestamp: {exc}")
            continue

        if end is not None and timestamp >= end:
            report.skip_out_of_window()
            continue

        try:
            candle = CandleData(
                timestamp=timestamp,
                open=_decimal(raw, "open"),
                high=_decimal(raw, "high"),
                low=_decimal(raw, "low"),
                close=_decimal(raw, "close"),
                volume=_decimal(raw, "volume", default=Decimal(0)),
                trade_count=_optional_int(raw, "trade_count"),
                vwap=_optional_decimal(raw, "vwap"),
            )
        except (ValueError, TypeError, InvalidOperation) as exc:
            report.reject(timestamp, _first_line(str(exc)))
            continue

        if timestamp in accepted:
            report.duplicate(timestamp)
            continue
        accepted[timestamp] = candle

    return [accepted[key] for key in sorted(accepted)], report


def normalise_quote(raw: Any, *, symbol: str) -> Quote:
    """Convert an Alpaca quote into the domain :class:`Quote`.

    ``Quote`` rejects crossed books and non-positive prices, so a nonsensical
    quote raises here rather than reaching the cost model. The provider's
    timestamp is preserved exactly.
    """
    try:
        return Quote(
            symbol=symbol,
            timestamp=ensure_utc(_require(raw, "timestamp")),
            bid=_decimal(raw, "bid_price"),
            ask=_decimal(raw, "ask_price"),
            bid_size=_optional_decimal(raw, "bid_size"),
            ask_size=_optional_decimal(raw, "ask_size"),
        )
    except (ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        msg = f"Alpaca returned an unusable quote for {symbol}: {_first_line(str(exc))}"
        raise ProviderError(msg) from exc


def normalise_corporate_actions(response: Any, *, symbol: str) -> list[CorporateAction]:
    """Map Alpaca corporate actions onto the tradabot domain.

    Alpaca expresses a split as ``old_rate`` -> ``new_rate`` (a 4-for-1 forward
    split is ``old_rate=1, new_rate=4``), which is exactly tradabot's
    ``from_shares``/``to_shares`` pair -- no lossy float ratio in between.

    Unsupported action types are skipped, deliberately. Storing a spin-off the
    adjustment layer ignores would produce a price series that looks corrected
    and is not.
    """
    actions: list[CorporateAction] = []
    for kind, entries in _iter_action_groups(response):
        for entry in entries:
            action = _map_action(kind, entry, symbol)
            if action is not None:
                actions.append(action)
    return sorted(actions, key=lambda a: a.effective_at)


def _map_action(kind: str, entry: Any, symbol: str) -> CorporateAction | None:
    """One Alpaca action to one domain action, or None if unsupported."""
    ex_date = _optional_date(entry, "ex_date")
    if ex_date is None:
        return None

    entry_symbol = str(getattr(entry, "symbol", symbol) or symbol).upper()
    external_id = _optional_str(entry, "id")

    if kind in {"forward_splits", "reverse_splits"}:
        old_rate = _optional_decimal(entry, "old_rate")
        new_rate = _optional_decimal(entry, "new_rate")
        if old_rate is None or new_rate is None or old_rate <= 0 or new_rate <= 0:
            return None
        return CorporateAction(
            symbol=entry_symbol,
            action_type=CorporateActionType.SPLIT,
            effective_at=ex_date,
            from_shares=old_rate,
            to_shares=new_rate,
            source=PROVIDER_NAME,
            external_id=external_id,
        )

    if kind == "cash_dividends":
        rate = _optional_decimal(entry, "rate")
        if rate is None or rate <= 0:
            return None
        return CorporateAction(
            symbol=entry_symbol,
            action_type=CorporateActionType.CASH_DIVIDEND,
            effective_at=ex_date,
            payment_at=_optional_date(entry, "payable_date"),
            cash_amount=rate,
            currency="USD",
            source=PROVIDER_NAME,
            external_id=external_id,
        )

    return None


# ---------------------------------------------------------------------------
# Response shape helpers
# ---------------------------------------------------------------------------
def _extract_bars(response: Any, symbol: str) -> list[Any]:
    """Pull one symbol's bars out of whatever shape the SDK returned.

    alpaca-py returns a ``BarSet`` that indexes by symbol, but tests and raw-data
    modes hand back plain dicts or lists. Handling all three here keeps the
    variation out of the provider's main path.
    """
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        value = response.get(symbol, [])
        return list(value) if value else []
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return list(data.get(symbol, []))
    try:
        return list(response[symbol])
    except (KeyError, TypeError):
        return []


def _extract_quote(response: Any, symbol: str) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get(symbol)
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data.get(symbol)
    try:
        return response[symbol]
    except (KeyError, TypeError):
        return None


def _iter_action_groups(response: Any) -> list[tuple[str, list[Any]]]:
    """Yield ``(group name, entries)`` from a corporate-actions response."""
    if response is None:
        return []
    if isinstance(response, dict):
        source: dict[str, Any] = response
    else:
        data = getattr(response, "data", None)
        source = data if isinstance(data, dict) else {}

    groups: list[tuple[str, list[Any]]] = []
    for key, value in source.items():
        if isinstance(value, list):
            groups.append((str(key), value))
    return groups


def _require(raw: Any, field: str) -> Any:
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    if value is None:
        msg = f"missing required field {field!r}"
        raise ValueError(msg)
    return value


def _decimal(raw: Any, field: str, *, default: Decimal | None = None) -> Decimal:
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    if value is None:
        if default is not None:
            return default
        msg = f"missing required field {field!r}"
        raise ValueError(msg)
    # str() first: a float would carry binary rounding into a Decimal price.
    return Decimal(str(value))


def _optional_decimal(raw: Any, field: str) -> Decimal | None:
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _optional_int(raw: Any, field: str) -> int | None:
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(raw: Any, field: str) -> str | None:
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    return str(value)[:64] if value is not None else None


def _optional_date(raw: Any, field: str) -> datetime | None:
    """A date-ish provider field as an aware UTC datetime at midnight."""
    value = getattr(raw, field, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(field)
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value) if value.tzinfo else value.replace(tzinfo=UTC)
    if hasattr(value, "year") and hasattr(value, "month"):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
def _classify(exc: Exception, label: str) -> ProviderError | None:
    """Return a terminal error, or None if the exception is worth retrying."""
    status = _status_code(exc)

    if status in {HTTP_UNAUTHORIZED, HTTP_FORBIDDEN}:
        return AlpacaAuthenticationError(
            f"Alpaca rejected the credentials on {label} (HTTP {status}). "
            f"Check ALPACA_API_KEY and ALPACA_API_SECRET."
        )
    if status == HTTP_UNPROCESSABLE:
        return ProviderError(f"Alpaca rejected the {label} request: {_safe_message(exc)}")
    if status == HTTP_NOT_FOUND:
        return ProviderError(f"Alpaca has no data for this {label} request")
    if status is not None and status in _RETRYABLE_STATUS:
        return None
    if status is not None and HTTP_CLIENT_ERROR_FLOOR <= status < HTTP_SERVER_ERROR_FLOOR:
        return ProviderError(f"Alpaca {label} failed: HTTP {status}")
    return None


def _status_code(exc: Exception) -> int | None:
    for attribute in ("status_code", "code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after(exc: Exception | None) -> float | None:
    """Honour a ``Retry-After`` header when the provider supplies one.

    The provider knows better than our backoff curve when it will accept traffic
    again; ignoring the header is how a client gets itself banned.
    """
    if exc is None:
        return None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _chunked(items: Sequence[str], size: int) -> list[list[str]]:
    """Split into request-sized groups.

    Alpaca accepts many symbols per request but not unboundedly many, and a very
    long URL is its own failure mode. The cap is configuration rather than a
    constant so it can be lowered without a release if the provider tightens it.
    """
    step = max(1, size)
    return [list(items[index : index + step]) for index in range(0, len(items), step)]


def build_alpaca_provider(
    settings: AlpacaSettings, market_data: MarketDataSettings
) -> AlpacaMarketDataProvider:
    """Factory for the provider registry."""
    return AlpacaMarketDataProvider(settings, market_data)


__all__ = [
    "PROVIDER_NAME",
    "AlpacaAuthenticationError",
    "AlpacaMarketDataProvider",
    "AlpacaRateLimitError",
    "MarketDataProviderUnavailableError",
    "build_alpaca_provider",
    "normalise_bars",
    "normalise_corporate_actions",
    "normalise_quote",
]
