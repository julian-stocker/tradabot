"""The interactive bot must never guess, never trade, and never analyse.

Three properties, in order of how much damage their absence would do:

* it cannot reach an execution path;
* it never substitutes one instrument for another;
* it computes no financial figure of its own, so it cannot disagree with the
  Advisor about the same company on the same day.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.events import EventCategory, EventType, Severity
from app.discord_bot import (
    Availability,
    BotConfigurationError,
    Resolution,
    StockAnalyst,
    check_message,
    load,
    normalise,
    presence,
    resolve,
)
from app.discord_bot.render import _dominant_colour
from app.publishing import presentation

PACKAGE = Path("app/discord_bot")
NOW = datetime(2026, 8, 16, 15, 0, tzinfo=UTC)

UNIVERSE = ["AAPL", "AMZN", "MSFT", "NVDA", "SPY", "TSLA"]
FUNDAMENTALS = frozenset({"AAPL", "AMZN", "MSFT", "NVDA", "TSLA"})

EXECUTION_TOKENS = (
    "submit_order",
    "cancel_order",
    "cancel_orders",
    "replace_order",
    "close_position",
    "close_all_positions",
    "liquidate",
    "submit_exit",
    "TradingClient",
    "PaperOrderSubmitter",
    "MarketOrderRequest",
)
RECOMMENDATION_WORDS = (
    "BUY",
    "SELL",
    "HOLD",
    "STRONG BUY",
    "TARGET PRICE",
    "EXPECTED RETURN",
    "PROBABILITY UP",
    "TOTAL_SCORE",
    "price_target",
    "expected_return",
)
RECOMPUTED = (
    "revenue_ttm =",
    "def ttm",
    "operating_margin =",
    "gross_margin =",
    "free_cash_flow =",
    # Computing a percentile, not naming one. The peer layer owns the midrank
    # convention; this package reads `comparison.percentile` off the result and
    # formats it, exactly as it reads a margin off a Section. Banning the bare
    # word would forbid the read as well as the arithmetic, which is the one
    # thing this package is supposed to do.
    "percentile_rank(",
    "percentile =",
    "pstdev",
    "def _correlation",
    "annualis",
    "herfindahl",
)

FAKE_ENV = {
    "DISCORD_APPLICATION_ID": "1234567890123456789",
    "DISCORD_BOT_TOKEN": "not-a-real-token-value-for-tests",
    "DISCORD_GUILD_ID": "9876543210987654321",
    "DISCORD_STOCKS_CHANNEL_ID": "1112223334445556667",
}


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in PACKAGE.glob("*.py")]


def _visible(message: object) -> str:
    fields = getattr(message, "fields", {}) or {}
    parts = [getattr(message, "title", ""), getattr(message, "body", "")]
    parts += [f"{name} {value}" for name, value in fields.items()]
    return "\n".join(parts)


def _report(
    *,
    confidence: str = "HIGH",
    labels: dict[str, str] | None = None,
    ps_context: str = "NORMAL_VS_HISTORY",
    net: float = -3.9e10,
    assessment: str = "ACCEPTABLE",
) -> SimpleNamespace:
    def metric(value: float | None) -> SimpleNamespace:
        return SimpleNamespace(value=value, available=value is not None)

    quality = [
        SimpleNamespace(
            name="GROWTH",
            metrics={"revenue_ttm": metric(4.6e11)},
            labels={},
            confidence="HIGH",
        ),
        SimpleNamespace(
            name="BALANCE SHEET",
            metrics={
                "cash": metric(3.95e10),
                "total_debt": metric(8.23e10),
                "net_cash_or_debt": metric(net),
            },
            labels={"assessment": assessment},
            confidence="HIGH",
        ),
        SimpleNamespace(
            name="CAPITAL STRUCTURE",
            metrics={"shares_outstanding": metric(1.459e10)},
            labels=labels or {"dilution": "BUYBACK_REDUCING_SHARE_COUNT"},
            confidence="HIGH",
        ),
    ]
    return SimpleNamespace(
        company_quality=quality,
        valuation=SimpleNamespace(
            metrics={"pe_ttm": metric(35.1), "ps_ttm": metric(9.6)},
            labels={"ps_context": ps_context},
        ),
        market_position=SimpleNamespace(metrics={"relative_strength_252d": metric(0.31)}),
        confidence={"company_analysis": confidence},
        summary="AAPL: trailing revenue is 466.82B; the balance sheet reads NET_CASH.",
    )


class FakeAdvisor:
    def __init__(self, report: object | None = None, raises: bool = False) -> None:
        self._report, self._raises = report, raises
        self.calls: list[str] = []
        self.company_keys: list[str | None] = []
        self.markets: list[object] = []

    def analyse(
        self,
        symbol: str,
        as_of: str | None = None,
        company_key: str | None = None,
        market: object = None,
    ) -> object:
        # Three identities, deliberately separate. `company_key` names the
        # reporting entity, which the Advisor gained when facts moved from
        # ticker to company identity; `market` names the price series and
        # benchmark, which it gained when a Xetra listing turned out to be
        # reading its US ADR's history.
        self.calls.append(symbol)
        self.company_keys.append(company_key)
        self.markets.append(market)
        if self._raises:
            msg = "advisor exploded"
            raise RuntimeError(msg)
        return self._report or _report()


def analyst(**kw: object) -> StockAnalyst:
    defaults: dict[str, object] = {
        "advisor": FakeAdvisor(),
        "universe": UNIVERSE,
        "fundamentals": FUNDAMENTALS,
        "fact_store_ready": True,
        "as_of": "2026-08-14",
    }
    defaults.update(kw)
    return StockAnalyst(**defaults)  # type: ignore[arg-type]


class TestReadOnlySafety:
    def test_no_module_reaches_an_execution_path(self) -> None:
        """**The gate.** A stock check must not be able to trade."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in EXECUTION_TOKENS:
                assert token not in body, f"{path} references {token}"

    def test_no_module_imports_a_broker_or_the_database(self) -> None:
        """**The gate.** Broker contact goes through the read-only snapshot only."""
        for path, source in _sources():
            for node in ast.walk(ast.parse(source)):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not name.startswith(("app.broker", "alpaca", "app.db", "sqlalchemy")), (
                        f"{path} imports {name}"
                    )

    def test_no_recommendation_vocabulary(self) -> None:
        """**The gate.** Describing is allowed; prescribing is not."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for word in RECOMMENDATION_WORDS:
                assert not re.search(rf"\b{re.escape(word)}\b", body), f"{path} emits {word}"

    def test_the_package_computes_no_financial_figure(self) -> None:
        """**The gate.** A second implementation would drift within a quarter."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in RECOMPUTED:
                assert token not in body, f"{path} recomputes {token}"

    def test_no_module_writes_state(self) -> None:
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for token in ("open(", "write_text", "mark_notified", "flush()"):
                assert token not in body, f"{path} writes state via {token}"


class TestConfiguration:
    def test_presence_reports_without_reading_values(self) -> None:
        found = presence(env=FAKE_ENV, dotenv=None)
        assert all(found.values())
        assert not any(v in str(found) for v in FAKE_ENV.values())

    def test_the_token_is_a_secret_type(self) -> None:
        """**The gate.** A bot token is the whole account."""
        settings = load(env=FAKE_ENV, dotenv=None)
        assert FAKE_ENV["DISCORD_BOT_TOKEN"] not in repr(settings)
        assert settings.describe()["bot_token_type"] == "SecretStr"
        assert not any(v in str(settings.describe()) for v in FAKE_ENV.values())

    def test_missing_configuration_names_the_variable_not_the_value(self) -> None:
        with pytest.raises(BotConfigurationError) as excinfo:
            load(env={"DISCORD_APPLICATION_ID": "1234567890123456789"}, dotenv=None)
        assert "DISCORD_BOT_TOKEN" in str(excinfo.value)

    @pytest.mark.parametrize("bad", ["", "abc", "12345", "1" * 21, "123456789012345678x"])
    def test_a_malformed_snowflake_is_refused(self, bad: str) -> None:
        env = {**FAKE_ENV, "DISCORD_GUILD_ID": bad}
        with pytest.raises(BotConfigurationError):
            load(env=env, dotenv=None)

    def test_the_webhook_publisher_is_untouched(self) -> None:
        """The bot is a second transport, not a replacement.

        The config docstring names the registry while explaining that it is
        separate, so the check is against the code below it.
        """
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            assert "WebhookRegistry" not in body, f"{path} resolves webhooks"


class TestSymbolResolution:
    @pytest.mark.parametrize(
        ("raw", "expected"), [(" nvda ", "NVDA"), ("aapl", "AAPL"), ("MsFt", "MSFT")]
    )
    def test_case_and_whitespace_are_normalised(self, raw: str, expected: str) -> None:
        assert normalise(raw) == expected

    def test_a_typo_never_resolves_to_another_symbol(self) -> None:
        """**The gate.** A confident report about the wrong company is the worst
        available outcome."""
        found = resolve("NVD", universe=UNIVERSE, fundamentals=FUNDAMENTALS)
        assert found.resolution is Resolution.UNKNOWN_SYMBOL
        assert found.symbol == "NVD"
        assert found.suggestion == "NVDA"

    def test_a_suggestion_is_offered_but_never_executed(self) -> None:
        found = resolve("NVD", universe=UNIVERSE, fundamentals=FUNDAMENTALS)
        message = check_message(analyst().check("NVD", now=NOW))
        assert found.suggestion == "NVDA"
        assert "not found" in message.title
        assert "will not substitute" in _visible(message)

    def test_an_ambiguous_near_miss_suggests_nothing(self) -> None:
        found = resolve("AAPLX", universe=["AAPL", "AAPLY"], fundamentals=None)
        assert found.resolution is Resolution.UNKNOWN_SYMBOL
        assert found.suggestion is None

    def test_an_international_listing_does_not_become_a_us_adr(self) -> None:
        """**The gate.** SAP.DE and SAP are different instruments."""
        found = resolve("SAP.DE", universe=["SAP"], fundamentals=frozenset({"SAP"}))
        assert found.resolution is Resolution.UNKNOWN_SYMBOL
        assert found.symbol == "SAP.DE"
        assert found.suggestion != "SAP" or found.suggestion is None

    def test_malformed_input_is_its_own_state(self) -> None:
        found = resolve("!!!", universe=UNIVERSE, fundamentals=None)
        assert found.resolution is Resolution.MALFORMED_SYMBOL

    def test_a_known_symbol_without_fundamentals_is_market_data_only(self) -> None:
        """Missing fundamentals is an absence of data, not a weak company."""
        found = resolve("SPY", universe=UNIVERSE, fundamentals=FUNDAMENTALS)
        assert found.resolution is Resolution.MARKET_DATA_ONLY
        assert found.market_data is Availability.AVAILABLE
        assert found.fundamentals is Availability.UNAVAILABLE
        assert "not a judgement" in (found.detail or "")

    def test_an_unsynced_fact_store_is_its_own_state(self) -> None:
        found = resolve("AAPL", universe=UNIVERSE, fundamentals=None, fact_store_ready=False)
        assert found.resolution is Resolution.DATA_NOT_SYNCED

    def test_the_outcomes_are_not_collapsed(self) -> None:
        outcomes = {
            resolve("NVD", universe=UNIVERSE, fundamentals=FUNDAMENTALS).resolution,
            resolve("!!!", universe=UNIVERSE, fundamentals=FUNDAMENTALS).resolution,
            resolve("SPY", universe=UNIVERSE, fundamentals=FUNDAMENTALS).resolution,
            resolve("AAPL", universe=UNIVERSE, fundamentals=FUNDAMENTALS).resolution,
            resolve(
                "AAPL", universe=UNIVERSE, fundamentals=None, fact_store_ready=False
            ).resolution,
        }
        assert len(outcomes) == 5


class TestAnalysisReuse:
    def test_the_production_advisor_is_called(self) -> None:
        advisor = FakeAdvisor()
        analyst(advisor=advisor).check("AAPL", now=NOW)
        assert advisor.calls == ["AAPL"]

    def test_an_advisor_failure_is_a_state_not_a_crash(self) -> None:
        result = analyst(advisor=FakeAdvisor(raises=True)).check("AAPL", now=NOW)
        assert result.resolution is Resolution.ANALYSIS_FAILED
        assert result.report is None

    def test_a_missing_symbol_never_reaches_the_advisor(self) -> None:
        advisor = FakeAdvisor()
        analyst(advisor=advisor).check("NVD", now=NOW)
        assert advisor.calls == []

    def test_no_broker_snapshot_is_required(self) -> None:
        """**The gate.** /check answers a company question, so an Alpaca outage
        cannot slow it or degrade it."""
        import inspect

        signature = inspect.signature(StockAnalyst.__init__)
        assert "accounts" not in signature.parameters
        source = (PACKAGE / "analysis.py").read_text()
        assert "snapshot" not in source.split('"""', 2)[-1]

    def test_no_account_appears_on_the_card(self) -> None:
        """**The gate.** Portfolio fit is a different question with its own owner."""
        text = _visible(check_message(analyst().check("AAPL", now=NOW)))
        for slot in ("PAPER_1K", "PAPER_3K", "PAPER_10K"):
            assert slot not in text
        assert "Portfolio" not in text

    def test_portfolio_fit_itself_is_untouched(self) -> None:
        """Removed from this path only; the layer still exists and still works."""
        from app.portfolio_fit import PortfolioFitService

        assert hasattr(PortfolioFitService, "candidate_fit")
        assert hasattr(PortfolioFitService, "clusters")


class TestPresentation:
    def test_the_card_is_company_analysis_only(self) -> None:
        """The title says what the card is, and it is not a portfolio view."""
        message = check_message(analyst().check("AAPL", now=NOW))
        assert "stock analysis" in message.title
        assert "Portfolio context" not in message.fields

    def test_non_obvious_states_carry_their_explanations(self) -> None:
        """Valuation bands are opaque without one; balance-sheet labels are not."""
        advisor = FakeAdvisor(_report(ps_context="VERY_HIGH_VS_HISTORY"))
        text = _visible(check_message(analyst(advisor=advisor).check("AAPL", now=NOW)))
        assert presentation.explain("VERY_HIGH_VS_HISTORY") in text

    def test_labels_are_human_readable_not_enum_names(self) -> None:
        """**The gate.** No internal enum name should reach a reader."""
        advisor = FakeAdvisor(_report(labels={"dilution": "BUYBACK_REDUCING_SHARE_COUNT"}))
        text = _visible(check_message(analyst(advisor=advisor).check("AAPL", now=NOW)))
        assert "BUYBACK_REDUCING_SHARE_COUNT" not in text
        assert "Share count decreasing" in text
        assert "VERY_HIGH_VS_HISTORY" not in text

    def test_confidence_is_stated_once(self) -> None:
        """**The gate.** Repeating it under every block pushed figures off screen."""
        message = check_message(analyst().check("AAPL", now=NOW))
        text = _visible(message)
        assert text.lower().count("confidence") == 1
        assert "Confidence" in message.fields["Data quality"]

    def test_a_weaker_section_surfaces_its_own_confidence(self) -> None:
        """The exception matters: a less trustworthy block must say so locally."""
        report = _report()
        report.company_quality[1].confidence = "LOW"
        text = _visible(check_message(analyst(advisor=FakeAdvisor(report)).check("AAPL", now=NOW)))
        assert "Data here: low confidence" in text

    def test_there_is_no_prose_summary_above_the_sections(self) -> None:
        """**The gate.** The report presents each figure once."""
        message = check_message(analyst().check("AAPL", now=NOW))
        assert message.body == ""

    def test_metrics_are_not_duplicated(self) -> None:
        message = check_message(analyst().check("AAPL", now=NOW))
        text = _visible(message)
        assert text.count("Revenue TTM") == 1
        assert text.count("Operating margin") <= 2  # once as a metric, once in Summary

    def test_the_summary_is_bounded_and_descriptive(self) -> None:
        from app.discord_bot.render import _MAX_BULLETS

        message = check_message(analyst().check("AAPL", now=NOW))
        bullets = message.fields["Summary"].splitlines()
        assert 1 <= len(bullets) <= _MAX_BULLETS
        for banned in (
            "buy",
            "sell",
            "hold",
            "should",
            "attractive",
            "undervalued",
            "overvalued",
            "expected",
        ):
            assert banned not in message.fields["Summary"].lower()

    def test_figures_are_formatted_for_a_reader(self) -> None:
        """EPS is money per share, not a percentage; negatives sign the amount."""
        from app.discord_bot.render import _value

        assert _value("eps_ttm", 8.71) == "$8.71"
        assert _value("net_cash_or_debt", -4.276e10) == "-$42.76B"
        assert _value("pe_ttm", 35.13) == "35.13\u00d7"
        assert _value("operating_margin", 0.332) == "33.2%"

    def test_a_sound_company_does_not_produce_a_green_card(self) -> None:
        """**The gate.** A green card reads as approval, which is a recommendation."""
        result = analyst().check("AAPL", now=NOW)
        assert _dominant_colour(result) == presentation.COLOURS[presentation.Semantic.NEUTRAL]

    def test_a_severe_present_risk_dominates(self) -> None:
        advisor = FakeAdvisor(_report(labels={"dilution": "MATERIAL_DILUTION"}))
        result = analyst(advisor=advisor).check("AAPL", now=NOW)
        assert _dominant_colour(result) == presentation.COLOURS[presentation.Semantic.BAD]

    def test_data_limitations_dominate(self) -> None:
        result = analyst().check("SPY", now=NOW)
        assert _dominant_colour(result) == presentation.COLOURS[presentation.Semantic.UNCERTAIN]

    def test_low_confidence_reads_as_uncertain(self) -> None:
        advisor = FakeAdvisor(_report(confidence="LOW"))
        result = analyst(advisor=advisor).check("AAPL", now=NOW)
        assert _dominant_colour(result) == presentation.COLOURS[presentation.Semantic.UNCERTAIN]

    def test_unusual_valuation_reads_as_unusual(self) -> None:
        advisor = FakeAdvisor(_report(ps_context="VERY_HIGH_VS_HISTORY"))
        result = analyst(advisor=advisor).check("AAPL", now=NOW)
        assert _dominant_colour(result) == presentation.COLOURS[presentation.Semantic.UNUSUAL]

    def test_missing_fundamentals_are_labelled_as_absent_data(self) -> None:
        text = _visible(check_message(analyst().check("SPY", now=NOW)))
        assert "an absence of data, not a judgement" in text

    def test_one_payload_one_representation(self) -> None:
        from app.notifications.embeds import build_payload

        payload = build_payload(
            check_message(analyst().check("AAPL", now=NOW)), max_characters=2000
        )
        assert payload["content"] == ""
        assert len(payload["embeds"]) == 1

    def test_the_embed_stays_inside_discords_limits(self) -> None:
        from app.notifications.embeds import build_embed

        embed = build_embed(check_message(analyst().check("AAPL", now=NOW)))
        total = len(embed["title"]) + len(embed.get("description", ""))
        total += sum(len(f["name"]) + len(f["value"]) for f in embed.get("fields", []))
        assert total <= 6000
        assert len(embed.get("fields", [])) <= 25
        assert all(len(f["value"]) <= 1024 for f in embed.get("fields", []))

    def test_the_observation_date_is_labelled_not_implied(self) -> None:
        """The card opts out of Discord's timestamp; see TestFooterAndTimestamp."""
        result = analyst().check("AAPL", now=NOW)
        assert "14 Aug 2026" in check_message(result).fields["Data quality"]
        assert check_message(result).show_timestamp is False


class TestChannelRestriction:
    def test_the_refusal_names_the_channel_not_its_id(self) -> None:
        """**The gate.** A numeric channel ID is configuration, not user-facing."""
        from app.discord_bot.bot import WRONG_CHANNEL_MESSAGE

        assert "#stocks" in WRONG_CHANNEL_MESSAGE
        assert not re.search(r"\d{17,20}", WRONG_CHANNEL_MESSAGE)

    def test_the_handler_compares_against_the_configured_channel(self) -> None:
        source = (PACKAGE / "bot.py").read_text()
        assert "interaction.channel_id != self._settings.stocks_channel_id" in source

    def test_no_privileged_intent_is_requested(self) -> None:
        """**The gate.** The bot cannot read messages it was not sent."""
        source = (PACKAGE / "bot.py").read_text()
        assert "discord.Intents.none()" in source
        for privileged in ("message_content", "members", "presences", "Intents.all"):
            assert privileged not in source

    def test_exactly_one_command_is_registered(self) -> None:
        source = (PACKAGE / "bot.py").read_text()
        assert source.count("@self.tree.command(") == 1
        assert 'name="check"' in source
        for forbidden in ("/buy", "/sell", "/portfolio", "/compare", "/chart"):
            assert f'name="{forbidden.lstrip("/")}"' not in source


class TestConcurrency:
    def test_concurrent_analyses_are_bounded(self) -> None:
        from app.discord_bot.bot import MAX_CONCURRENT_CHECKS

        assert 1 <= MAX_CONCURRENT_CHECKS <= 8

    def test_a_busy_bot_says_so(self) -> None:
        from app.discord_bot.bot import BUSY_MESSAGE

        assert "try again" in BUSY_MESSAGE.lower()

    def test_no_result_cache_hides_a_stale_as_of(self) -> None:
        """A cached valuation served without its as-of is worse than a slow one."""
        source = (PACKAGE / "bot.py").read_text()
        assert "lru_cache" not in source
        result = analyst().check("AAPL", now=NOW)
        assert result.as_of == "2026-08-14"


class TestNetPosition:
    """The label and the sign must never contradict each other."""

    def test_a_net_debt_company_says_net_debt_with_a_positive_figure(self) -> None:
        """**The gate.** "Net cash / debt  -$42.76B" made the reader decode both."""
        advisor = FakeAdvisor(_report(net=-4.276e10, assessment="ACCEPTABLE"))
        block = check_message(analyst(advisor=advisor).check("AAPL", now=NOW)).fields[
            "Balance sheet"
        ]
        assert "Net debt" in block
        assert "$42.76B" in block
        assert "-$" not in block
        assert "Net cash" not in block

    def test_a_net_cash_company_says_net_cash_with_a_positive_figure(self) -> None:
        advisor = FakeAdvisor(_report(net=3.9e10, assessment="NET_CASH"))
        block = check_message(analyst(advisor=advisor).check("AAPL", now=NOW)).fields[
            "Balance sheet"
        ]
        assert "Net cash" in block
        assert "$39.00B" in block
        assert "-$" not in block
        assert "Net debt" not in block

    def test_the_canonical_metric_is_unchanged(self) -> None:
        """Presentation splits the sign out; the Advisor's number is untouched."""
        report = _report(net=-4.276e10)
        section = next(s for s in report.company_quality if s.name == "BALANCE SHEET")
        assert section.metrics["net_cash_or_debt"].value == -4.276e10


class TestInterpretiveSummary:
    def test_the_summary_does_not_repeat_the_figures_above_it(self) -> None:
        """**The gate.** A summary that restates the numbers is a second copy."""
        message = check_message(analyst().check("AAPL", now=NOW))
        summary = message.fields["Summary"]
        for figure in ("46.8%", "33.2%", "$466.82B", "$39.54B", "$82.30B", "35.13"):
            assert figure not in summary
        assert not re.search(r"\$[\d,]+\.?\d*[BMT]?", summary)

    def test_the_summary_is_at_most_four_bullets(self) -> None:
        from app.discord_bot.render import _MAX_BULLETS

        assert _MAX_BULLETS == 4
        bullets = check_message(analyst().check("AAPL", now=NOW)).fields["Summary"].splitlines()
        assert 1 <= len(bullets) <= 4

    def test_the_summary_describes_states_that_already_exist(self) -> None:
        summary = check_message(analyst().check("AAPL", now=NOW)).fields["Summary"]
        assert "moderate net debt" in summary
        assert "decreasing" in summary

    def test_a_negative_condition_may_be_described_negatively(self) -> None:
        advisor = FakeAdvisor(
            _report(labels={"dilution": "MATERIAL_DILUTION"}, assessment="LEVERAGED")
        )
        summary = check_message(analyst(advisor=advisor).check("AAPL", now=NOW)).fields["Summary"]
        assert "substantial net debt" in summary
        assert "materially increasing" in summary

    def test_the_summary_grades_nothing(self) -> None:
        """No Advisor state says "strong", so the summary must not either."""
        summary = check_message(analyst().check("AAPL", now=NOW)).fields["Summary"]
        for graded in ("strong", "weak", "excellent", "poor", "healthy", "attractive"):
            assert graded not in summary.lower()

    def test_the_summary_carries_no_recommendation(self) -> None:
        summary = check_message(analyst().check("AAPL", now=NOW)).fields["Summary"]
        for banned in (
            "buy",
            "sell",
            "hold",
            "should",
            "undervalued",
            "overvalued",
            "expected",
            "target",
            "likely",
        ):
            assert banned not in summary.lower()

    def test_market_direction_reads_the_sign_not_a_threshold(self) -> None:
        from app.discord_bot.render import _market_direction

        report = _report()
        assert "ahead of the benchmark" in (_market_direction(report) or "")
        report.market_position.metrics["relative_strength_252d"] = SimpleNamespace(
            value=-0.1, available=True
        )
        assert "behind the benchmark" in (_market_direction(report) or "")


class TestShareCountWording:
    @pytest.mark.parametrize(
        ("internal", "expected"),
        [
            ("BUYBACK_REDUCING_SHARE_COUNT", "Share count decreasing"),
            ("STABLE", "Share count stable"),
            ("DILUTING", "Share count increasing"),
            ("MATERIAL_DILUTION", "Material share-count increase"),
            ("SPLIT_ADJUSTMENT_REQUIRED", "Share-count trend unavailable"),
        ],
    )
    def test_labels_are_reader_facing(self, internal: str, expected: str) -> None:
        assert presentation.label(internal) == expected

    def test_a_buyback_is_not_called_shareholder_friendly(self) -> None:
        """**The gate.** Describing a fact must not become endorsing it."""
        explanation = presentation.explain("BUYBACK_REDUCING_SHARE_COUNT") or ""
        for banned in ("shareholder-friendly", "good for", "rewards", "returns capital"):
            assert banned not in explanation.lower()
        assert "Fewer shares are outstanding" in explanation

    def test_one_owner_defines_these_states(self) -> None:
        """No second dictionary competes with the presentation owner."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            assert "BUYBACK_REDUCING_SHARE_COUNT" not in body, f"{path} redefines it"


class TestFooterAndTimestamp:
    def test_the_disclaimer_appears_exactly_once(self) -> None:
        from app.notifications.embeds import build_embed

        embed = build_embed(check_message(analyst().check("AAPL", now=NOW)))
        rendered = json.dumps(embed)
        assert rendered.count("No forecast or investment recommendation") == 1
        assert embed["footer"]["text"].startswith("Descriptive analysis only")

    def test_no_timestamp_is_concatenated_into_the_footer(self) -> None:
        """**The gate.** Discord joins footer and timestamp itself; we must not."""
        from app.notifications.embeds import build_embed

        embed = build_embed(check_message(analyst().check("AAPL", now=NOW)))
        footer = embed["footer"]["text"]
        assert "timestamp" not in embed
        assert not re.search(r"\d{4}-\d{2}-\d{2}", footer)
        assert "•" not in footer
        assert not footer.startswith("**")

    def test_the_observation_date_stays_in_data_quality(self) -> None:
        message = check_message(analyst().check("AAPL", now=NOW))
        assert "14 Aug 2026" in message.fields["Data quality"]
        assert "14 Aug 2026" not in (message.footer or "")

    def test_other_embeds_keep_their_native_timestamp(self) -> None:
        """Opting out is per message; the publisher's cards are unaffected."""
        from app.notifications.embeds import build_embed
        from app.notifications.models import NotificationMessage

        message = NotificationMessage(
            category=EventCategory.MARKET,
            severity=Severity.INFO,
            title="t",
            body="b",
            event_type=EventType.MARKET_TRENDS,
            occurred_at=NOW,
        )
        assert build_embed(message)["timestamp"] == NOW.isoformat()


class TestTimingInstrumentation:
    def test_timings_record_stages_without_changing_the_answer(self) -> None:
        from app.discord_bot.timing import Timings

        clock = Timings()
        with_timing = analyst().check("AAPL", now=NOW, timings=clock)
        without = analyst().check("AAPL", now=NOW)
        assert with_timing.symbol == without.symbol
        assert with_timing.resolution == without.resolution
        assert set(clock.stages) >= {"resolve", "advisor"}

    def test_a_failing_stage_is_still_recorded(self) -> None:
        from app.discord_bot.timing import Timings

        def boom() -> None:
            msg = "stage failed"
            raise RuntimeError(msg)

        clock = Timings()
        with pytest.raises(RuntimeError), clock.stage("boom"):
            boom()
        assert "boom" in clock.stages

    def test_timings_carry_no_configuration(self) -> None:
        """**The gate.** Only stage names, which are literals, and durations."""
        from app.discord_bot.timing import Timings

        clock = Timings()
        analyst().check("AAPL", now=NOW, timings=clock)
        rendered = json.dumps(clock.as_dict())
        for value in FAKE_ENV.values():
            assert value not in rendered
        assert all(isinstance(v, (int, float)) for v in clock.stages.values())


class TestNeutralDirection:
    """A sentence must never contradict the figure printed above it."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.101, "ahead of the benchmark"),
            (-0.101, "behind the benchmark"),
            (0.0, "in line with the benchmark"),
            (0.0004, "in line with the benchmark"),  # renders as +0.0%
            (-0.0004, "in line with the benchmark"),  # renders as -0.0%
            (0.0006, "ahead of the benchmark"),  # renders as +0.1%
        ],
    )
    def test_the_sign_matches_what_is_displayed(self, value: float, expected: str) -> None:
        """**The gate.** +0.0% is neither ahead nor behind."""
        from app.discord_bot.render import _market_direction

        report = _report()
        report.market_position.metrics["relative_strength_252d"] = SimpleNamespace(
            value=value, available=True
        )
        assert expected in (_market_direction(report) or "")

    def test_the_tolerance_matches_the_rendered_precision(self) -> None:
        """A display tolerance, not a financial threshold."""
        from app.discord_bot.render import _DISPLAY_TOLERANCE

        assert _DISPLAY_TOLERANCE == 0.0005
        assert f"{_DISPLAY_TOLERANCE * 100:+.1f}%" == "+0.1%"
        assert f"{(_DISPLAY_TOLERANCE - 1e-6) * 100:+.1f}%" == "+0.0%"

    def test_a_flat_200_day_average_is_described_as_level(self) -> None:
        from app.discord_bot.render import _market_direction

        report = _report()
        report.market_position.metrics["distance_from_ma200"] = SimpleNamespace(
            value=0.0, available=True
        )
        assert "at its 200-day average" in (_market_direction(report) or "")


class TestFiftyTwoWeekHigh:
    def test_being_at_the_high_is_named_not_signed(self) -> None:
        """**The gate.** "+0.0% from the high" invites a question with no answer."""
        report = _report()
        report.market_position.metrics["drawdown_from_252d_high"] = SimpleNamespace(
            value=0.0, available=True
        )
        block = check_message(analyst(advisor=FakeAdvisor(report)).check("AAPL", now=NOW)).fields[
            "Market position"
        ]
        assert "at the high" in block
        assert "+0.0%" not in block

    def test_a_drawdown_is_shown_as_a_positive_distance_below(self) -> None:
        report = _report()
        report.market_position.metrics["drawdown_from_252d_high"] = SimpleNamespace(
            value=-0.10, available=True
        )
        block = check_message(analyst(advisor=FakeAdvisor(report)).check("AAPL", now=NOW)).fields[
            "Market position"
        ]
        assert "Below 52w high" in block
        assert "10.0%" in block
        assert "-10.0%" not in block

    def test_the_canonical_drawdown_value_is_unchanged(self) -> None:
        report = _report()
        report.market_position.metrics["drawdown_from_252d_high"] = SimpleNamespace(
            value=-0.10, available=True
        )
        assert report.market_position.metrics["drawdown_from_252d_high"].value == -0.10


class TestPartialDataSummary:
    @staticmethod
    def _partial() -> object:
        """A real partial-data case: priced, no SEC company facts."""
        return analyst().check("SPY", now=NOW)

    def test_the_limitation_is_stated_once(self) -> None:
        """**The gate.** Absent sections are one limitation, not several findings."""
        summary = check_message(self._partial()).fields["Summary"]
        assert summary.lower().count("unavailable") == 1

    def test_absent_sections_are_not_summarised_as_findings(self) -> None:
        summary = check_message(self._partial()).fields["Summary"]
        assert "insufficient data" not in summary.lower()
        assert "Balance sheet:" not in summary

    def test_what_exists_is_still_summarised(self) -> None:
        summary = check_message(self._partial()).fields["Summary"]
        assert "market-position" in summary.lower()
        assert "trading" in summary.lower()

    def test_missing_fundamentals_are_never_a_company_judgement(self) -> None:
        text = _visible(check_message(self._partial()))
        assert "an absence of data, not a judgement" in text
        for graded in ("weak", "poor", "unhealthy"):
            assert graded not in text.lower()

    def test_valuation_absence_uses_valuation_wording(self) -> None:
        """INSUFFICIENT_HISTORY is also a regime state; its sentence is about
        price history and would be wrong under a valuation heading."""
        advisor = FakeAdvisor(_report(ps_context="INSUFFICIENT_HISTORY"))
        block = check_message(analyst(advisor=advisor).check("SPY", now=NOW)).fields["Valuation"]
        assert "not enough valuation history" in block
        assert "describe a regime" not in block

    def test_the_partial_summary_carries_no_recommendation(self) -> None:
        summary = check_message(self._partial()).fields["Summary"]
        for banned in ("buy", "sell", "hold", "should", "expected", "target"):
            assert banned not in summary.lower()


class TestFullySupportedRenderingUnchanged:
    def test_the_accepted_card_still_renders_as_accepted(self) -> None:
        """**The gate.** 12.40b's AAPL card was signed off; do not regress it."""
        message = check_message(analyst().check("AAPL", now=NOW))
        text = _visible(message)
        assert (
            _dominant_colour(analyst().check("AAPL", now=NOW))
            == presentation.COLOURS[presentation.Semantic.NEUTRAL]
        )
        assert "Net debt" in message.fields["Balance sheet"]
        assert "Portfolio" not in text
        assert message.body == ""
        assert message.show_timestamp is False
        assert message.footer is not None
        assert "moderate net debt" in message.fields["Summary"]

    def test_advisor_values_are_passed_through_untouched(self) -> None:
        report = _report()
        message = check_message(analyst(advisor=FakeAdvisor(report)).check("AAPL", now=NOW))
        assert "$460.00B" in message.fields["Growth"]
        assert report.valuation.metrics["pe_ttm"].value == 35.1
        assert "35.10\u00d7" in message.fields["Valuation"]
