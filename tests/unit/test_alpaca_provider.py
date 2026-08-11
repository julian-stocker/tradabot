"""Alpaca provider: normalisation, transport policy and secret hygiene.

**Entirely offline.** Every SDK client is a stub; nothing here opens a socket, and
the suite must stay runnable on a machine with no credentials and no network.

Stubs mimic the SDK's *shape* (attribute access on bar objects, a ``data`` dict
keyed by symbol) rather than importing it, so these tests keep working when the
SDK is absent and fail loudly if our mapping drifts from what we claim it is.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from app.core.config import AlpacaSettings, MarketDataSettings
from app.core.errors import ConfigurationError, ProviderError
from app.core.redaction import redact
from app.domain.enums import Timeframe
from app.market_data.providers.alpaca import (
    AlpacaAuthenticationError,
    AlpacaMarketDataProvider,
    MarketDataProviderUnavailableError,
    normalise_bars,
    normalise_corporate_actions,
    normalise_quote,
)

SYMBOL = "NVDA"
BAR_TIME = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)

# A key-shaped string that is not a real credential. Deliberately fake, and
# checked into the repository only because the redaction tests need something
# key-shaped to prove they mask.
FAKE_KEY = "PKTESTFAKE1234567890"


# ---------------------------------------------------------------------------
# SDK stubs
# ---------------------------------------------------------------------------
@dataclass
class StubBar:
    """The attributes our mapping reads off an Alpaca ``Bar``."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 1_000.0
    trade_count: int | None = 10
    vwap: float | None = None


@dataclass
class StubQuote:
    timestamp: datetime
    bid_price: float
    ask_price: float
    bid_size: float | None = 100.0
    ask_size: float | None = 120.0


class StubBarSet:
    """Mimics ``BarSet``: a ``data`` mapping from symbol to bars."""

    def __init__(self, mapping: dict[str, list[StubBar]]) -> None:
        self.data = mapping


class StubQuoteSet:
    def __init__(self, mapping: dict[str, StubQuote]) -> None:
        self.data = mapping


class StubHttpError(Exception):
    """An SDK error carrying a status code, as the real ones do."""

    def __init__(self, status_code: int, message: str = "boom", retry_after: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = _StubResponse(status_code, retry_after)


class _StubResponse:
    def __init__(self, status_code: int, retry_after: str | None) -> None:
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after else {}


class StubStockClient:
    """Records calls and replays a scripted sequence of results."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls = 0

    def _next(self) -> Any:
        self.calls += 1
        outcome = self._results.pop(0) if self._results else self._results
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_stock_bars(self, request: Any) -> Any:
        return self._next()

    def get_stock_latest_quote(self, request: Any) -> Any:
        return self._next()


def make_bar(
    *,
    minutes: int = 0,
    open_: float = 100.0,
    high: float = 102.0,
    low: float = 99.0,
    close: float = 101.0,
    volume: float = 1_000.0,
) -> StubBar:
    return StubBar(
        timestamp=BAR_TIME + timedelta(minutes=minutes),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def make_settings(**overrides: Any) -> AlpacaSettings:
    """Configured credentials and a retry policy that does not slow the suite."""
    defaults: dict[str, Any] = {
        "api_key": SecretStr(FAKE_KEY),
        "api_secret": SecretStr("fake-secret-value"),
        "max_retries": 2,
        "backoff_base_seconds": 0.001,
        "backoff_max_seconds": 0.002,
        "request_timeout_seconds": 0.05,
    }
    return AlpacaSettings(**(defaults | overrides))


def make_provider(results: list[Any], **setting_overrides: Any) -> AlpacaMarketDataProvider:
    return AlpacaMarketDataProvider(
        make_settings(**setting_overrides),
        MarketDataSettings(),
        stock_client=StubStockClient(results),
    )


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_bars_normalise_to_domain_candles() -> None:
    candles, report = normalise_bars([make_bar()], symbol=SYMBOL)

    assert len(candles) == 1
    assert report.is_clean
    candle = candles[0]
    assert candle.timestamp == BAR_TIME
    assert candle.open == Decimal("100")
    assert candle.high == Decimal("102")
    assert candle.low == Decimal("99")
    assert candle.close == Decimal("101")
    assert isinstance(candle.open, Decimal), "prices must not arrive as floats"


def test_ohlc_violations_are_rejected_not_repaired() -> None:
    """A high below the open is a corrupt bar; guessing what it meant is worse."""
    bad = make_bar(open_=100.0, high=95.0, low=90.0, close=94.0)

    candles, report = normalise_bars([bad], symbol=SYMBOL)

    assert candles == []
    assert len(report.rejected) == 1
    assert report.rejected[0].timestamp == BAR_TIME


def test_negative_volume_is_rejected() -> None:
    candles, report = normalise_bars([make_bar(volume=-1.0)], symbol=SYMBOL)

    assert candles == []
    assert len(report.rejected) == 1


def test_one_bad_bar_does_not_discard_the_good_ones() -> None:
    good, bad = make_bar(), make_bar(minutes=1, open_=10.0, high=1.0, low=0.5, close=0.9)

    candles, report = normalise_bars([good, bad], symbol=SYMBOL)

    assert len(candles) == 1
    assert len(report.rejected) == 1


def test_a_non_utc_timestamp_is_converted_not_relabelled() -> None:
    """16:00 New York is 20:00 UTC -- the same instant, stored one way."""
    eastern = ZoneInfo("America/New_York")
    aware = StubBar(
        timestamp=datetime(2024, 6, 3, 16, 0, tzinfo=eastern),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
    )

    candles, report = normalise_bars([aware], symbol=SYMBOL)

    assert report.is_clean
    assert candles[0].timestamp == BAR_TIME
    assert candles[0].timestamp.utcoffset() == timedelta(0)


def test_a_naive_timestamp_is_rejected_rather_than_assumed_utc() -> None:
    """Guessing a zone silently shifts a bar by hours and looks like real data.

    Alpaca always sends an aware timestamp, so a naive one means something
    upstream is wrong -- exactly when a guess is most expensive.
    """
    naive = StubBar(
        timestamp=datetime(2024, 6, 3, 20, 0),  # noqa: DTZ001 -- naive is the point
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
    )

    candles, report = normalise_bars([naive], symbol=SYMBOL)

    assert candles == []
    assert len(report.rejected) == 1


def test_duplicate_timestamps_collapse_to_one_bar() -> None:
    candles, report = normalise_bars([make_bar(), make_bar()], symbol=SYMBOL)

    assert len(candles) == 1
    assert len(report.duplicates) == 1


def test_bars_are_returned_in_ascending_order() -> None:
    candles, _ = normalise_bars(
        [make_bar(minutes=10), make_bar(minutes=0), make_bar(minutes=5)], symbol=SYMBOL
    )

    assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)


def test_the_end_boundary_is_exclusive() -> None:
    """Alpaca's ``end`` is inclusive; ours is not, or consecutive pages duplicate."""
    candles, report = normalise_bars(
        [make_bar(), make_bar(minutes=1)], symbol=SYMBOL, end=BAR_TIME + timedelta(minutes=1)
    )

    assert [c.timestamp for c in candles] == [BAR_TIME]
    assert report.out_of_window == 1


def test_quotes_normalise_and_keep_the_provider_timestamp() -> None:
    quote = normalise_quote(
        StubQuote(timestamp=BAR_TIME, bid_price=99.95, ask_price=100.05), symbol=SYMBOL
    )

    assert quote.bid == Decimal("99.95")
    assert quote.ask == Decimal("100.05")
    assert quote.timestamp == BAR_TIME


def test_a_crossed_quote_is_refused() -> None:
    """Ask below bid is impossible; letting it through corrupts every cost figure."""
    with pytest.raises(ProviderError):
        normalise_quote(
            StubQuote(timestamp=BAR_TIME, bid_price=100.05, ask_price=99.95), symbol=SYMBOL
        )


def test_a_non_positive_price_quote_is_refused() -> None:
    with pytest.raises(ProviderError):
        normalise_quote(StubQuote(timestamp=BAR_TIME, bid_price=0.0, ask_price=1.0), symbol=SYMBOL)


def test_corporate_actions_map_onto_the_domain_split() -> None:
    """Alpaca's old_rate/new_rate is our from_shares/to_shares -- both exact."""

    @dataclass
    class StubSplit:
        symbol: str
        ex_date: datetime
        old_rate: float
        new_rate: float

    class StubActions:
        def __init__(self) -> None:
            self.data = {
                "forward_splits": [
                    StubSplit(
                        symbol=SYMBOL,
                        ex_date=datetime(2024, 6, 10, tzinfo=UTC),
                        old_rate=1.0,
                        new_rate=10.0,
                    )
                ]
            }

    actions = normalise_corporate_actions(StubActions(), symbol=SYMBOL)

    assert len(actions) == 1
    assert actions[0].split_ratio == Decimal(10)


# ---------------------------------------------------------------------------
# Transport: retries, timeouts, failure classification
# ---------------------------------------------------------------------------
async def test_rate_limit_is_retried_and_then_succeeds() -> None:
    client = StubStockClient([StubHttpError(429), StubBarSet({SYMBOL: [make_bar()]})])
    provider = AlpacaMarketDataProvider(make_settings(), MarketDataSettings(), stock_client=client)

    candles = await provider.get_historical_candles(
        SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
    )

    assert client.calls == 2, "a 429 must be retried"
    assert len(candles) == 1


async def test_retryable_server_errors_are_retried_then_reported() -> None:
    """Exhausting the budget raises rather than returning an empty result.

    Returning ``[]`` on failure would be indistinguishable from "the market was
    closed", and a caller would store the silence as data.
    """
    client = StubStockClient([StubHttpError(503), StubHttpError(503), StubHttpError(503)])
    provider = AlpacaMarketDataProvider(
        make_settings(max_retries=2), MarketDataSettings(), stock_client=client
    )

    with pytest.raises(ProviderError):
        await provider.get_historical_candles(
            SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
        )

    assert client.calls == 3, "max_retries=2 means three attempts in total"


async def test_authentication_failure_is_not_retried() -> None:
    """Retrying a 401 four times only delays telling the operator their key is wrong."""
    client = StubStockClient([StubHttpError(401), StubBarSet({SYMBOL: [make_bar()]})])
    provider = AlpacaMarketDataProvider(make_settings(), MarketDataSettings(), stock_client=client)

    with pytest.raises(AlpacaAuthenticationError):
        await provider.get_historical_candles(
            SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
        )

    assert client.calls == 1


async def test_forbidden_is_also_terminal() -> None:
    client = StubStockClient([StubHttpError(403)])
    provider = AlpacaMarketDataProvider(make_settings(), MarketDataSettings(), stock_client=client)

    with pytest.raises(AlpacaAuthenticationError):
        await provider.get_latest_quote(SYMBOL)

    assert client.calls == 1


async def test_a_timeout_is_bounded_and_reported() -> None:
    """A hung provider must fail the call, not the process."""

    class HangingClient:
        calls = 0

        def get_stock_latest_quote(self, request: Any) -> Any:
            HangingClient.calls += 1
            import time

            time.sleep(0.5)
            return None

    provider = AlpacaMarketDataProvider(
        make_settings(max_retries=0, request_timeout_seconds=0.02),
        MarketDataSettings(),
        stock_client=HangingClient(),
    )

    with pytest.raises(MarketDataProviderUnavailableError):
        await asyncio.wait_for(provider.get_latest_quote(SYMBOL), timeout=5)


async def test_a_retry_after_header_is_honoured() -> None:
    """The provider knows when it will accept traffic better than our curve does."""
    client = StubStockClient(
        [StubHttpError(429, retry_after="0.01"), StubBarSet({SYMBOL: [make_bar()]})]
    )
    provider = AlpacaMarketDataProvider(
        make_settings(backoff_base_seconds=30.0, backoff_max_seconds=30.0),
        MarketDataSettings(),
        stock_client=client,
    )

    # Completing well inside the 30s backoff proves the header, not the curve,
    # set the wait.
    candles = await asyncio.wait_for(
        provider.get_historical_candles(
            SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
        ),
        timeout=5,
    )

    assert len(candles) == 1


async def test_a_malformed_response_raises_rather_than_returning_nothing() -> None:
    """A response shaped like nothing we recognise is an error, not empty data."""
    provider = make_provider([object()])

    with pytest.raises(ProviderError):
        await provider.get_latest_quote(SYMBOL)


async def test_a_response_missing_the_symbol_is_empty_not_an_error() -> None:
    """No bars for a window is an ordinary outcome -- a closed market, a new listing."""
    provider = make_provider([StubBarSet({})])

    candles = await provider.get_historical_candles(
        SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
    )

    assert candles == []


async def test_a_partial_response_keeps_the_valid_bars() -> None:
    bad = StubBar(
        timestamp=BAR_TIME + timedelta(minutes=1), open=1.0, high=0.5, low=0.4, close=0.45
    )
    provider = make_provider([StubBarSet({SYMBOL: [make_bar(), bad]})])

    candles = await provider.get_historical_candles(
        SYMBOL, Timeframe.D1, BAR_TIME - timedelta(days=1), BAR_TIME + timedelta(days=1)
    )

    assert len(candles) == 1


async def test_an_inverted_window_is_refused_before_any_request() -> None:
    client = StubStockClient([])
    provider = AlpacaMarketDataProvider(make_settings(), MarketDataSettings(), stock_client=client)

    with pytest.raises(ProviderError):
        await provider.get_historical_candles(SYMBOL, Timeframe.D1, BAR_TIME, BAR_TIME)

    assert client.calls == 0


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_the_module_imports_without_credentials() -> None:
    """Construction must never require a key -- the API must be importable anywhere."""
    provider = AlpacaMarketDataProvider(AlpacaSettings(), MarketDataSettings())

    assert provider.name == "alpaca"
    assert provider.last_error is None


async def test_an_unconfigured_provider_fails_with_guidance_not_a_stack_trace() -> None:
    provider = AlpacaMarketDataProvider(AlpacaSettings(), MarketDataSettings())

    with pytest.raises(ConfigurationError) as caught:
        await provider.get_latest_quote(SYMBOL)

    message = str(caught.value)
    assert "ALPACA_API_KEY" in message
    assert "mock" in message


async def test_a_failure_message_never_leaks_a_credential() -> None:
    """The last line of defence before a key reaches a log file."""
    leaky = StubHttpError(500, message=f"request failed: api_key={FAKE_KEY} secret=hunter2hunter2")
    provider = make_provider([leaky, leaky, leaky], max_retries=1)

    with pytest.raises(ProviderError) as caught:
        await provider.get_latest_quote(SYMBOL)

    rendered = str(caught.value)
    assert FAKE_KEY not in rendered
    assert "hunter2hunter2" not in rendered
    assert provider.last_error is not None
    assert FAKE_KEY not in provider.last_error


@pytest.mark.parametrize(
    "text",
    [
        f"api_key={FAKE_KEY}",
        f"API-KEY: {FAKE_KEY}",
        f"Authorization: Bearer {FAKE_KEY}",
        f'{{"secret_key": "{FAKE_KEY}"}}',
        f"token={FAKE_KEY}",
        f"unlabelled {FAKE_KEY} in prose",
    ],
)
def test_redaction_masks_every_credential_shape(text: str) -> None:
    assert FAKE_KEY not in redact(text)


def test_redaction_masks_the_whole_authorization_value() -> None:
    """`Bearer <token>` puts the secret in the *second* word."""
    redacted = redact("Authorization: Bearer sk-live-abcdefghijklmnop")

    assert "sk-live-abcdefghijklmnop" not in redacted
    assert "Bearer" not in redacted


def test_settings_do_not_expose_secrets_when_printed_or_dumped() -> None:
    settings = make_settings()

    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in str(settings.model_dump())
    assert settings.api_key.get_secret_value() == FAKE_KEY, "still retrievable deliberately"


def test_is_configured_requires_both_halves_of_the_credential() -> None:
    assert not AlpacaSettings().is_configured
    assert not AlpacaSettings(api_key=SecretStr(FAKE_KEY)).is_configured
    assert not AlpacaSettings(api_secret=SecretStr("s")).is_configured
    assert make_settings().is_configured
