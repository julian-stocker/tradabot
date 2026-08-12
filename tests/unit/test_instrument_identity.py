"""Provider-neutral instrument identity, and the XNAS regression.

Every instrument once claimed `exchange = XNAS` and `name = symbol`, because
`get_instruments()` fabricates both. Thirty-two of fifty-two were NYSE-listed.
It was invisible because `exchange` only feeds calendar selection, and both US
venues share sessions -- so nothing failed, the data was simply wrong.
"""

from __future__ import annotations

from app.market_data.provider import AssetCatalogue, AssetMetadata
from app.market_data.providers.alpaca import _MIC_BY_ALPACA_EXCHANGE, _mic_for


class FakeCatalogue:
    """A provider that reports real venues."""

    name = "fake"

    def __init__(self, venues: dict[str, str]) -> None:
        self._venues = venues

    async def get_asset_metadata(self, symbols: list[str]) -> dict[str, AssetMetadata]:
        return {
            symbol: AssetMetadata(
                symbol=symbol,
                name=f"{symbol} Corporation",
                exchange=self._venues.get(symbol),
            )
            for symbol in symbols
            if symbol in self._venues
        }


# ---------------------------------------------------------------------------
# MIC mapping
# ---------------------------------------------------------------------------
def test_nyse_maps_to_xnys_not_xnas() -> None:
    """**The regression.** JPM, KO and XOM are NYSE."""
    assert _mic_for("NYSE") == "XNYS"
    assert _mic_for("NASDAQ") == "XNAS"


def test_an_unknown_venue_maps_to_none_never_a_default() -> None:
    """Defaulting is exactly how everything came to claim XNAS."""
    assert _mic_for("SOME_NEW_VENUE") is None
    assert _mic_for(None) is None


def test_every_mapped_venue_is_a_four_letter_mic() -> None:
    """ISO 10383 codes, not vendor spellings -- XETR and XTKS slot straight in."""
    for mic in _MIC_BY_ALPACA_EXCHANGE.values():
        assert len(mic) == 4
        assert mic.isupper()


def test_an_enum_like_exchange_is_unwrapped() -> None:
    """Alpaca returns `AssetExchange.NYSE`, not the string."""

    class Wrapped:
        value = "NYSE"

    assert _mic_for(Wrapped()) == "XNYS"


# ---------------------------------------------------------------------------
# Provider-neutral identity
# ---------------------------------------------------------------------------
def test_asset_metadata_requires_only_a_symbol() -> None:
    """Every other field is omitted rather than guessed."""
    metadata = AssetMetadata(symbol="NVDA")

    assert metadata.symbol == "NVDA"
    assert metadata.name is None
    assert metadata.exchange is None
    assert metadata.currency is None
    assert metadata.country is None


def test_asset_metadata_carries_international_identity() -> None:
    """The same shape describes a Xetra listing -- no schema change needed."""
    metadata = AssetMetadata(
        symbol="SMHN",
        name="SUSS MicroTec SE",
        exchange="XETR",
        currency="EUR",
        country="DE",
    )

    assert metadata.exchange == "XETR"
    assert metadata.currency == "EUR"


def test_a_catalogue_satisfies_the_protocol_structurally() -> None:
    """No provider names Alpaca; a future European feed implements the same thing."""
    assert isinstance(FakeCatalogue({}), AssetCatalogue)


async def test_a_catalogue_omits_symbols_it_does_not_know() -> None:
    """The point is to stop inventing metadata, so absence stays absence."""
    catalogue = FakeCatalogue({"JPM": "XNYS"})

    found = await catalogue.get_asset_metadata(["JPM", "285A.T"])

    assert set(found) == {"JPM"}
    assert "285A.T" not in found


async def test_a_mixed_universe_does_not_collapse_to_one_exchange() -> None:
    """**The assertion that fails if the defect returns.**"""
    catalogue = FakeCatalogue({"JPM": "XNYS", "KO": "XNYS", "NVDA": "XNAS", "AAPL": "XNAS"})

    found = await catalogue.get_asset_metadata(["JPM", "KO", "NVDA", "AAPL"])
    venues = {meta.exchange for meta in found.values()}

    assert len(venues) > 1, "every instrument resolved to the same exchange"
    assert venues == {"XNYS", "XNAS"}


async def test_a_company_name_is_not_the_ticker() -> None:
    catalogue = FakeCatalogue({"JPM": "XNYS"})

    found = await catalogue.get_asset_metadata(["JPM"])

    assert found["JPM"].name is not None
    assert found["JPM"].name != "JPM"
