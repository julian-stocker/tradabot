"""What a number on the card is allowed to mean.

Everything here defends one boundary: an indicator says something about a
*metric in a stated context*, never about a company as an investment. The tests
that matter most are the ones asserting a colour is **withheld** -- a green
badge nobody can justify is worse than no badge, because a reader who cannot
evaluate a 33% operating margin certainly cannot evaluate why it was called
good.

Three refusals are pinned here because two owning layers already made them and
this consumer must not quietly undo them:

* a peer percentile never produces green or red, because
  ``MetricComparison.higher_is_not_better`` is permanently true;
* a direction alone never produces green or red, because ``Direction`` is
  ``EXPANDING``/``COMPRESSING`` and not ``IMPROVING``/``DECLINING``;
* revenue produces nothing at all, on a measurement rather than on caution.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.publishing import indicators
from app.publishing.indicators import (
    HIGH_PCT,
    LOW_PCT,
    VERY_HIGH_PCT,
    VERY_LOW_PCT,
    Evidence,
    margin_indicator,
    ordinal,
    peer_only,
)
from app.publishing.presentation import COLOURS, Semantic

GRID = (0.0, 5.0, 10.0, 24.9, 25.0, 50.0, 74.9, 75.0, 90.0, 99.0, 100.0)


# ------------------------------------------------------------ status mapping
def test_every_category_has_a_badge_and_a_colour_and_they_are_the_same_six() -> None:
    """One vocabulary. A state cannot be green on the embed's edge and orange
    in the middle of it, because both read the same enum."""
    assert set(indicators.BADGES) == set(COLOURS) == set(Semantic)
    assert len(set(indicators.BADGES.values())) == len(Semantic)


def test_the_badge_set_contains_no_symbol_that_implies_an_action() -> None:
    """Circles only. An arrow points somewhere and a tick approves of something;
    both would say more than a category is entitled to."""
    for badge in indicators.BADGES.values():
        assert badge in {"🟢", "🔴", "🟠", "🟡", "🔵", "⚪"}


# ------------------------------------------------------------- band alignment
def test_the_bands_match_the_advisors_own_cuts_across_a_scale_change() -> None:
    """The project has two percentile scales and one set of cuts.

    The Advisor works in 0-1; history and peers return 0-100, which is what
    arrives here. Copying ``0.10`` across would have placed every band below the
    first percentile and marked every margin in the universe favourable, so the
    equivalence is asserted rather than assumed.
    """
    from app.advisor.service import _P10, _P25, _P75, _P90

    assert (VERY_LOW_PCT, LOW_PCT, HIGH_PCT, VERY_HIGH_PCT) == (
        _P10 * 100,
        _P25 * 100,
        _P75 * 100,
        _P90 * 100,
    )


def test_an_ordinal_never_reads_as_a_decimal_or_a_zeroth() -> None:
    assert ordinal(98.0) == "98th"
    assert ordinal(2.0) == "2nd"
    assert ordinal(3.4) == "3rd"
    assert ordinal(11.0) == "11th"
    assert ordinal(21.0) == "21st"
    assert "." not in ordinal(83.33)


# ----------------------------------------------------- own history direction
@pytest.mark.parametrize("metric", sorted(indicators.MARGIN_METRICS))
@pytest.mark.parametrize(
    ("percentile", "expected"),
    [
        (98.0, Semantic.GOOD),
        (75.0, Semantic.GOOD),
        (74.9, Semantic.NEUTRAL),
        (50.0, Semantic.NEUTRAL),
        (25.1, Semantic.NEUTRAL),
        (25.0, Semantic.BAD),
        (1.0, Semantic.BAD),
    ],
)
def test_a_margin_takes_its_direction_from_its_own_recorded_range(
    metric: str, percentile: float, expected: Semantic
) -> None:
    """Self-referential, and immune to the objection that sinks peer comparison.

    A structurally low-margin business is compared only against itself, so
    "near the best this company has recorded" is a claim about an observed
    condition rather than about whether it is a good business.
    """
    found = margin_indicator(metric, own_percentile=percentile, peer_percentile=None)
    assert found is not None
    assert found.status is expected
    assert found.evidence is Evidence.OWN_HISTORY
    assert ordinal(percentile) in found.reason


def test_the_reason_states_the_observation_not_the_conclusion() -> None:
    found = margin_indicator("operating_margin", own_percentile=98.0, peer_percentile=83.0)
    assert found is not None
    assert found.reason == "98th pct of own history · 83rd pct among peers"
    assert "good" not in found.reason.lower()
    assert "strong" not in found.reason.lower()


# --------------------------------------------------------- peers never direct
@pytest.mark.parametrize("peer", GRID)
def test_a_peer_percentile_alone_is_never_favourable_or_unfavourable(peer: float) -> None:
    """``higher_is_not_better`` is always true, and its docstring says the field
    exists so a consumer wanting to sort by "good" has to confront that the data
    does not support it. This module is that consumer."""
    found = peer_only("operating_margin", peer)
    assert found is None or found.status is Semantic.UNUSUAL
    with_own_absent = margin_indicator(
        "operating_margin", own_percentile=None, peer_percentile=peer
    )
    assert with_own_absent is None or with_own_absent.status is Semantic.UNUSUAL


def test_an_extreme_peer_position_is_notable_and_an_ordinary_one_is_silent() -> None:
    assert peer_only("fcf_margin", 96.0) is not None
    assert peer_only("fcf_margin", 96.0).status is Semantic.UNUSUAL  # type: ignore[union-attr]
    assert peer_only("fcf_margin", 4.0) is not None
    assert peer_only("fcf_margin", 60.0) is None
    assert peer_only("fcf_margin", None) is None


# ------------------------------------------------------------ disagreement
def test_disagreement_is_reported_as_disagreement_and_never_averaged() -> None:
    """Apple's gross margin is at the 98th percentile of its own record and the
    25th among peers. Neither is wrong. Averaging them to the 61st would produce
    a number describing nothing, and picking one would be a preference."""
    found = margin_indicator("gross_margin", own_percentile=98.0, peer_percentile=25.0)
    assert found is not None
    assert found.status is Semantic.UNUSUAL
    assert found.evidence is Evidence.MIXED_CONTEXT
    assert found.reason == "98th pct of own history; 25th pct among peers"
    assert "61" not in found.reason


def test_disagreement_is_detected_in_both_directions() -> None:
    low_own_high_peer = margin_indicator(
        "operating_margin", own_percentile=5.0, peer_percentile=95.0
    )
    assert low_own_high_peer is not None
    assert low_own_high_peer.status is Semantic.UNUSUAL
    assert low_own_high_peer.evidence is Evidence.MIXED_CONTEXT


def test_agreement_keeps_the_direction_and_says_both_numbers() -> None:
    found = margin_indicator("fcf_margin", own_percentile=1.0, peer_percentile=24.0)
    assert found is not None
    assert found.status is Semantic.BAD
    assert found.reason == "1st pct of own history · 24th pct among peers"


def test_no_hidden_score_is_computed_from_two_percentiles() -> None:
    """Two contexts never combine into a third number.

    Asserted on the source: ``margin_indicator`` may compare percentiles and may
    print them, and may not add, average or weight them into anything.
    """
    source = Path("app/publishing/indicators.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            assert not isinstance(node.op, ast.Add | ast.Div | ast.Mult | ast.Sub), (
                "indicators performs arithmetic; two contexts must not become a score"
            )


# ------------------------------------------------------------- what is refused
@pytest.mark.parametrize("metric", ["revenue", "revenue_ttm", "eps_ttm", "cash", "total_debt"])
@pytest.mark.parametrize("own", [None, 0.0, 50.0, 98.0])
@pytest.mark.parametrize("peer", [None, 0.0, 50.0, 98.0])
def test_a_descriptive_metric_gets_no_indicator_at_any_percentile(
    metric: str, own: float | None, peer: float | None
) -> None:
    """Revenue, earnings, cash and debt are scale quantities.

    Large revenue is not favourable, a large cash balance is not favourable and
    debt is not unfavourable. Revenue is excluded on measurement rather than on
    taste: its own-history percentile has a median of 98 across the universe and
    sits at or above the 95th for 60% of companies, so it says "this company has
    grown" and nothing else.
    """
    assert margin_indicator(metric, own_percentile=own, peer_percentile=peer) is None


def test_a_metric_with_no_context_at_all_gets_no_indicator() -> None:
    assert margin_indicator("operating_margin", own_percentile=None, peer_percentile=None) is None


def test_a_sign_never_decides_anything() -> None:
    """The rule the whole module exists to avoid.

    A negative net cash figure is net debt, a falling share count is a buyback,
    and a positive multiple is not an endorsement. No value is passed to this
    module at all -- only positions -- so a sign has nowhere to be read.
    """
    source = ast.parse(Path("app/publishing/indicators.py").read_text())
    signatures = [
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    ]
    for function in signatures:
        names = {a.arg for a in function.args.args + function.args.kwonlyargs}
        assert "value" not in names, f"{function.name} takes a raw value"
        assert not names & {"amount", "figure", "magnitude"}, function.name


# ---------------------------------------------------------------- ownership
def test_a_state_indicator_defers_entirely_to_presentation() -> None:
    """Buyback, dilution, net cash and leverage were decided in earlier phases.

    Re-deriving them here would mean the same condition could be green on the
    card and red in a newsletter. ``from_state`` looks the colour up.
    """
    for internal, expected in (
        ("BUYBACK_REDUCING_SHARE_COUNT", Semantic.GOOD),
        ("MATERIAL_DILUTION", Semantic.BAD),
        ("NET_CASH", Semantic.GOOD),
        ("LEVERAGED", Semantic.BAD),
        ("ACCEPTABLE", Semantic.NEUTRAL),
        ("VERY_HIGH_VS_HISTORY", Semantic.UNUSUAL),
        ("NORMAL_VS_HISTORY", Semantic.NEUTRAL),
        ("SECTOR_SPECIFIC_MODEL_REQUIRED", Semantic.UNAVAILABLE),
        ("INSUFFICIENT_DATA", Semantic.UNAVAILABLE),
        ("PARTIAL_PORTFOLIO", Semantic.UNCERTAIN),
    ):
        found = indicators.from_state("m", internal, "because")
        assert found is not None, internal
        assert found.status is expected, internal
        assert found.evidence is Evidence.ADVISOR_STATE


def test_an_extreme_valuation_is_notable_and_never_unfavourable() -> None:
    """Expensive is not bad and cheap is not good.

    Both ends of the valuation range are orange, which is the existing
    presentation decision and the reason this phase did not have to make one.
    """
    for internal in (
        "VERY_HIGH_VS_HISTORY",
        "HIGH_VS_HISTORY",
        "LOW_VS_HISTORY",
        "VERY_LOW_VS_HISTORY",
    ):
        found = indicators.from_state("pe_ttm", internal, "because")
        assert found is not None
        assert found.status is Semantic.UNUSUAL, internal
        assert found.status is not Semantic.BAD
        assert found.status is not Semantic.GOOD


def test_missing_data_is_never_unfavourable() -> None:
    """A gap in Tradabot's coverage is not a finding about the company."""
    for internal in (
        "INSUFFICIENT_DATA",
        "INSUFFICIENT_HISTORY",
        "SPLIT_ADJUSTMENT_REQUIRED",
        "SECTOR_SPECIFIC_MODEL_REQUIRED",
        "UNKNOWN",
    ):
        found = indicators.from_state("m", internal, "because")
        assert found is not None
        assert found.status is not Semantic.BAD, internal
        assert found.status in (Semantic.UNAVAILABLE, Semantic.UNCERTAIN)


# ------------------------------------------------------------- architecture
def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_indicator_layer_holds_no_service_and_can_fetch_nothing() -> None:
    """It receives positions that owning services already computed.

    If it could call a service it could compute a percentile, and a percentile
    computed here would eventually disagree with the one printed two lines above
    it on the same card.
    """
    app_imports = {
        m for m in _imports(Path("app/publishing/indicators.py")) if m.startswith("app.")
    }
    assert app_imports == {"app.publishing.presentation"}


def test_the_renderer_still_computes_no_financial_quantity() -> None:
    """The badge came from a service result, not from arithmetic in the view."""
    forbidden = {
        "FactStore",
        "AdvisorService",
        "CompanyHistoryService",
        "PeerComparisonService",
        "percentile_rank",
        "midrank_percentile",
    }
    tree = ast.parse(Path("app/discord_bot/render.py").read_text())
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not (used & forbidden), used & forbidden


def test_no_indicator_can_reach_synthesis_or_a_model() -> None:
    """Phase 18.2 is deterministic. Nothing here touches Phase 18.1."""
    for path in (Path("app/publishing/indicators.py"), Path("app/discord_bot/render.py")):
        for module in _imports(path):
            assert "synthesis" not in module, f"{path} imports {module}"
            assert "openai" not in module, f"{path} imports {module}"


def _emittable_strings(path: Path) -> str:
    """Every string literal the module could put on a card, and no docstring.

    Docstrings are excluded deliberately. The first version of the check below
    read the whole file and failed on the word "score" -- inside the sentence
    explaining that nothing here computes a score. That is the sixth time in
    this project a text gate has matched its own explanation of why the thing
    it bans is banned, so this one is built from the syntax tree and looks only
    at strings that can reach a reader.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        # A bare string statement is documentation: a module, class or function
        # docstring, or the attribute docstring this codebase writes under a
        # constant. None of them can reach a card.
        body = getattr(node, "body", None)
        # `body` is a list on statements and a single expression on things like
        # a conditional expression, which is why this is checked rather than
        # iterated hopefully.
        if not isinstance(body, list):
            continue
        for child in body:
            if (
                isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Constant)
                and isinstance(child.value.value, str)
            ):
                docstrings.add(id(child.value))
    return " ".join(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    )


def test_the_module_names_no_recommendation_and_no_forecast() -> None:
    """Whole words, over the strings this module can actually emit.

    Word boundaries because this project has repeatedly caught itself banning a
    substring and rejecting an innocent word -- ``rating`` inside ``operating``,
    ``registry`` inside a sentence explaining there is no registry.
    """
    emitted = _emittable_strings(Path("app/publishing/indicators.py"))
    banned = (
        "buy",
        "sell",
        "hold",
        "bullish",
        "bearish",
        "undervalued",
        "overvalued",
        "target",
        "forecast",
        "predict",
        "outperform",
        "score",
        "rating",
    )
    for word in banned:
        assert not re.search(rf"\b{word}\b", emitted, re.IGNORECASE), word


# -------------------------------------------------------------- on the card
def _metric(value: float | None) -> SimpleNamespace:
    return SimpleNamespace(value=value, available=value is not None)


def _trajectory(**percentiles: float | None) -> SimpleNamespace:
    metrics = {
        name: SimpleNamespace(
            metric=name,
            percentile=pct,
            available=True,
            unit="ratio",
            current=0.332,
            direction="EXPANDING",
            changes={
                "3y": SimpleNamespace(
                    window="3y",
                    from_value=0.292,
                    absolute=3.9,
                    relative=0.13,
                    annualised=None,
                )
            },
        )
        for name, pct in percentiles.items()
    }
    return SimpleNamespace(metrics=metrics, get=metrics.get, detail=None)


def _peers(**percentiles: float) -> SimpleNamespace:
    comparisons = [
        SimpleNamespace(
            metric=name,
            label=name.replace("_", " ").title(),
            percentile=pct,
            median=0.2,
            value=0.33,
            unit="PERCENT",
        )
        for name, pct in percentiles.items()
    ]
    return SimpleNamespace(
        available=True,
        comparisons=comparisons,
        detail=None,
        group=SimpleNamespace(
            size=13,
            label="SIC 357",
            included=[SimpleNamespace(symbol="CSCO"), SimpleNamespace(symbol="DELL")],
            mixed_taxonomy=False,
            subject_taxonomy="us-gaap",
            peer_taxonomy="us-gaap",
        ),
    )


def _card(**kw: object) -> str:
    """Render one card through the production renderer and flatten it."""
    from app.discord_bot import Availability, Resolution
    from app.discord_bot.analysis import StockCheck
    from app.discord_bot.render import check_message

    defaults: dict[str, object] = {
        "requested": "AAPL",
        "symbol": "AAPL",
        "resolution": Resolution.SUPPORTED,
        "market_data": Availability.AVAILABLE,
        "fundamentals": Availability.AVAILABLE,
        "as_of": "2026-09-01",
        "checked_at": datetime(2026, 9, 1, tzinfo=UTC),
        "report": _fixture_report(),
    }
    defaults.update(kw)
    message = check_message(StockCheck(**defaults))  # type: ignore[arg-type]
    fields = getattr(message, "fields", {}) or {}
    return "\n".join([message.title, message.body, *(f"{k}\n{v}" for k, v in fields.items())])


def _fixture_report(
    *, assessment: str = "ACCEPTABLE", dilution: str = "BUYBACK_REDUCING_SHARE_COUNT"
) -> SimpleNamespace:
    quality = [
        SimpleNamespace(
            name="GROWTH", metrics={"revenue_ttm": _metric(4.6e11)}, labels={}, confidence="HIGH"
        ),
        SimpleNamespace(
            name="PROFITABILITY",
            metrics={"operating_margin": _metric(0.332), "gross_margin": _metric(0.487)},
            labels={},
            confidence="HIGH",
        ),
        SimpleNamespace(
            name="CASH GENERATION",
            metrics={"free_cash_flow": _metric(1.36e11), "fcf_margin": _metric(0.293)},
            labels={},
            confidence="HIGH",
        ),
        SimpleNamespace(
            name="BALANCE SHEET",
            metrics={"cash": _metric(3.95e10), "net_cash_or_debt": _metric(-4.2e10)},
            labels={"assessment": assessment},
            confidence="HIGH",
        ),
        SimpleNamespace(
            name="CAPITAL STRUCTURE",
            metrics={"shares_outstanding": _metric(1.459e10)},
            labels={"dilution": dilution},
            confidence="HIGH",
        ),
    ]
    return SimpleNamespace(
        company_quality=quality,
        valuation=SimpleNamespace(
            metrics={"pe_ttm": _metric(35.1)}, labels={"ps_context": "VERY_HIGH_VS_HISTORY"}
        ),
        market_position=SimpleNamespace(metrics={"relative_strength_252d": _metric(0.31)}),
        confidence={"company_analysis": "HIGH"},
        summary="AAPL: trailing revenue is 466.82B.",
    )


def test_a_margin_near_its_own_best_is_marked_on_the_card() -> None:
    card = _card(
        trajectory=_trajectory(operating_margin=98.0),
        peers=_peers(operating_margin=83.0),
    )
    assert "🟢 Operating margin — 98th pct of own history · 83rd pct among peers" in card


def test_a_disagreement_reaches_the_card_as_orange_and_says_both() -> None:
    card = _card(
        trajectory=_trajectory(gross_margin=98.0),
        peers=_peers(gross_margin=25.0),
    )
    assert "🟠 Gross margin — 98th pct of own history; 25th pct among peers" in card


def test_a_mid_range_margin_adds_no_line_at_all() -> None:
    """Silence is the default. A blue badge on every figure is noise, and noise
    is what stops a reader finding the two lines that matter."""
    card = _card(trajectory=_trajectory(operating_margin=50.0), peers=None)
    assert "Operating margin — " not in card
    assert "🔵 Operating margin" not in card


def test_revenue_never_carries_a_badge_on_the_card() -> None:
    card = _card(trajectory=_trajectory(revenue=99.0), peers=_peers(revenue=99.0))
    assert "🟢 **Revenue**" not in card
    assert "🔴 **Revenue**" not in card
    assert "Revenue TTM" in card


def test_an_advisor_state_keeps_its_own_colour_on_the_card() -> None:
    assert "🟢 Share count decreasing" in _card()
    assert "🔴 Material share-count increase" in _card(
        report=_fixture_report(dilution="MATERIAL_DILUTION")
    )
    assert "🟢 Net cash" in _card(report=_fixture_report(assessment="NET_CASH"))
    assert "🔴 Leveraged" in _card(report=_fixture_report(assessment="LEVERAGED"))


def test_an_extreme_valuation_reaches_the_card_orange() -> None:
    card = _card()
    assert "🟠 _Priced near the top of its own historical range" in card
    assert "🔴 _Priced near the top" not in card


def test_the_card_still_names_no_recommendation_and_no_forecast() -> None:
    """The whole point of the phase, checked on the rendered text.

    A badge is a category. If adding one had smuggled in a word like
    "undervalued" or "should", the feature would have become the thing it was
    built to avoid.
    """
    card = _card(
        trajectory=_trajectory(operating_margin=98.0, gross_margin=2.0),
        peers=_peers(operating_margin=95.0, gross_margin=5.0),
    ).lower()
    for word in (
        "buy",
        "sell",
        "bullish",
        "bearish",
        "undervalued",
        "overvalued",
        "price target",
        "should own",
        "outperform",
        "will rise",
        "will fall",
        "expected to",
        "likely to",
    ):
        assert not re.search(rf"\b{re.escape(word)}\b", card), word


def test_a_badge_never_appears_inside_an_emphasis_marker() -> None:
    """``_⚪ text_`` puts the emoji inside the italics, where Discord renders it
    as part of the sentence rather than as its marker."""
    card = _card()
    for badge in indicators.BADGES.values():
        assert f"_{badge}" not in card


def test_every_field_stays_within_discords_limit() -> None:
    from app.discord_bot import Availability, Resolution
    from app.discord_bot.analysis import StockCheck
    from app.discord_bot.render import check_message

    message = check_message(
        StockCheck(
            requested="AAPL",
            symbol="AAPL",
            resolution=Resolution.SUPPORTED,
            market_data=Availability.AVAILABLE,
            fundamentals=Availability.AVAILABLE,
            as_of="2026-09-01",
            checked_at=datetime(2026, 9, 1, tzinfo=UTC),
            report=_fixture_report(),
            trajectory=_trajectory(operating_margin=98.0, fcf_margin=1.0, gross_margin=50.0),
            peers=_peers(operating_margin=95.0, fcf_margin=5.0, gross_margin=50.0),
        )
    )
    for name, value in (message.fields or {}).items():
        assert len(value) <= 1024, f"{name} is {len(value)} characters"
    assert len(message.body) <= 4096


# ------------------------------------------------- pinned representative cases
def _section(
    name: str, metrics: dict[str, float | None], labels: dict[str, str]
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        metrics={k: _metric(v) for k, v in metrics.items()},
        labels=labels,
        confidence="HIGH",
    )


def _listing(**kw: object) -> SimpleNamespace:
    defaults: dict[str, object] = {
        "taxonomy": "us-gaap",
        "reporting_currency": "USD",
        "asset_type": "STOCK",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_aapl_gross_margin_disagreement_is_pinned() -> None:
    """The case the mixed-context rule exists for, fixed as a regression.

    High for Apple, low for a peer group whose median is lifted by companies
    selling software. Both true. If a later change silently picks a side, this
    fails.
    """
    card = _card(
        trajectory=_trajectory(operating_margin=98.0, gross_margin=98.0),
        peers=_peers(operating_margin=83.0, gross_margin=25.0),
    )
    assert "🟠 Gross margin — 98th pct of own history; 25th pct among peers" in card
    assert "🟢 Operating margin — 98th pct of own history · 83rd pct among peers" in card
    assert "🟢 Gross margin" not in card
    assert "🔴 Gross margin" not in card


def test_msft_fcf_margin_at_the_bottom_of_its_own_record_is_unfavourable() -> None:
    """Both contexts agree, so the direction stands: 28.1% to 20.2% over three
    years leaves it at the 1st percentile of everything it has reported."""
    card = _card(
        trajectory=_trajectory(fcf_margin=1.0),
        peers=_peers(fcf_margin=24.0),
    )
    assert "🔴 FCF margin — 1st pct of own history · 24th pct among peers" in card


def test_nvda_split_refusal_survives_and_produces_no_direction() -> None:
    """A ten-for-one split is not dilution.

    The Advisor refuses the share-count trend for exactly this reason and the
    refusal is grey. Nothing in this phase may turn that into a judgement, and
    nothing may badge the metric from a percentile computed over a series the
    owning layer has already declared unusable.
    """
    report = _fixture_report()
    report.company_quality = [
        s for s in report.company_quality if s.name != "CAPITAL STRUCTURE"
    ] + [
        _section(
            "CAPITAL STRUCTURE",
            {"shares_outstanding": 2.44e10},
            {"dilution": "SPLIT_ADJUSTMENT_REQUIRED"},
        )
    ]
    card = _card(report=report)
    assert "⚪ Share-count trend unavailable" in card
    assert "🟢 Share count" not in card
    assert "🔴 Share count" not in card


def test_ko_partial_section_wording_survives_the_badge() -> None:
    """The regression this phase caused once and must not cause again.

    Coca-Cola shows an exact cash figure and stopped reporting the debt
    concepts, so the section cannot be assessed while the number above it is
    correct. The rule that says so once compared rendered text; adding a badge
    to that text silently reverted it to "Insufficient data".
    """
    report = _fixture_report()
    report.company_quality = [s for s in report.company_quality if s.name != "BALANCE SHEET"] + [
        _section("BALANCE SHEET", {"cash": 1.057e10}, {"assessment": "INSUFFICIENT_DATA"})
    ]
    card = _card(report=report)
    assert "🟡 _Partial — the figures shown are as filed" in card
    assert "⚪ Insufficient data" not in card
    assert "$10.57B" in card


def test_a_section_state_decision_never_reads_rendered_text() -> None:
    """Asserted structurally, because that is how the bug got in.

    ``_section_states`` returns the codes the services emit. The partial rule
    compares against those; a decoration cannot reach it.
    """
    from app.discord_bot.render import _section_states

    section = _section("BALANCE SHEET", {"cash": 1.0}, {"assessment": "INSUFFICIENT_DATA"})
    assert _section_states(section) == ["INSUFFICIENT_DATA"]
    for code in _section_states(section):
        for badge in indicators.BADGES.values():
            assert badge not in code


@pytest.mark.parametrize(
    ("name", "labels", "taxonomy"),
    [
        ("JPM", {"assessment": "SECTOR_SPECIFIC_MODEL_REQUIRED"}, "us-gaap"),
        ("RY.TO", {"assessment": "SECTOR_SPECIFIC_MODEL_REQUIRED"}, "ifrs-full"),
        ("SAP.DE", {"assessment": "INSUFFICIENT_DATA"}, "ifrs-full"),
    ],
)
def test_a_refusal_badge_sits_outside_the_emphasis_it_labels(
    name: str, labels: dict[str, str], taxonomy: str
) -> None:
    """``_⚪ text_`` renders the emoji as part of the sentence, not as its mark."""
    report = _fixture_report()
    report.company_quality = [s for s in report.company_quality if s.name != "BALANCE SHEET"] + [
        _section("BALANCE SHEET", {"cash": 3.0e11}, labels)
    ]
    card = _card(symbol=name, report=report, listing=_listing(taxonomy=taxonomy))
    assert "⚪ _" in card
    assert "_⚪" not in card


def test_a_financial_company_keeps_its_refusals_and_its_share_count() -> None:
    """Industrial metrics stay refused; a share count is still a share count."""
    report = _fixture_report()
    report.company_quality = [
        _section("GROWTH", {"eps_ttm": 23.35}, {}),
        _section(
            "BALANCE SHEET",
            {"cash": 3.09e11, "net_cash_or_debt": 2.37e11},
            {"assessment": "SECTOR_SPECIFIC_MODEL_REQUIRED"},
        ),
        _section(
            "CAPITAL STRUCTURE",
            {"shares_outstanding": 2.66e9},
            {"dilution": "BUYBACK_REDUCING_SHARE_COUNT"},
        ),
    ]
    card = _card(symbol="JPM", report=report, peers=None, trajectory=None)
    assert "⚪ _Not assessed for a financial company" in card
    assert "🟢 Share count decreasing" in card
    assert "🟢 Net cash" not in card
    assert "🔴 Leveraged" not in card


def test_an_ifrs_filer_keeps_its_limitations_and_gains_no_borrowed_valuation() -> None:
    """SAP.DE may be read against its own annual history and nothing else.

    No price from the US line, no currency conversion, no valuation badge that
    could only have come from a listing this one is not.
    """
    report = _fixture_report()
    report.company_quality = [
        s for s in report.company_quality if s.name != "CAPITAL STRUCTURE"
    ] + [
        _section(
            "CAPITAL STRUCTURE", {"shares_outstanding": 1.17e9}, {"dilution": "INSUFFICIENT_DATA"}
        )
    ]
    card = _card(
        symbol="SAP.DE",
        report=report,
        listing=_listing(taxonomy="ifrs-full", reporting_currency="EUR"),
        valuation_refusal="the price and the earnings are in different currencies",
        trajectory=_trajectory(gross_margin=77.0),
        peers=_peers(gross_margin=33.0),
    )
    assert "🟢 Gross margin — 77th pct of own history · 33rd pct among peers" in card
    assert "🟠 _Priced near the top" not in card
    assert "vs own history" not in card


def test_a_fund_gains_no_operating_semantics_at_all() -> None:
    """SPY has holdings and a net asset value. There is nothing to assess, and
    an assessment of nothing would be an invention."""
    from app.discord_bot import Availability

    report = _fixture_report()
    report.company_quality = [
        _section("BALANCE SHEET", {}, {"assessment": "INSUFFICIENT_DATA"}),
        _section("CAPITAL STRUCTURE", {}, {"dilution": "INSUFFICIENT_DATA"}),
    ]
    card = _card(
        symbol="SPY",
        report=report,
        fundamentals=Availability.UNAVAILABLE,
        listing=_listing(asset_type="ETF"),
        trajectory=None,
        peers=None,
    )
    assert "⚪ _This is a fund, not an operating company" in card
    for badge in ("🟢", "🔴"):
        assert badge not in card


# ------------------------------------------------------- no composite status
def test_no_indicator_is_ever_combined_with_another() -> None:
    """Disagreement is the information. Collapsing it would delete it.

    Asserted on the renderer's call graph: nothing sums, averages, sorts or
    ranks indicators, and no name suggests a company-level verdict.
    """
    tree = ast.parse(Path("app/discord_bot/render.py").read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for banned in ("score", "rating", "grade", "overall_status", "aggregate", "verdict"):
        assert not any(banned in name for name in names), banned

    # Deliberately not a ban on `sum`: the renderer counts characters against
    # the trajectory budget with it, and banning the builtin caught that instead
    # of anything to do with meaning. The guarantee that matters is in the
    # signatures -- every entry point returns one indicator or none, so there is
    # no collection of them for anything to reduce.
    import inspect
    import typing

    for name in ("margin_indicator", "from_state", "peer_only"):
        returns = typing.get_type_hints(getattr(indicators, name))["return"]
        assert returns == indicators.MetricIndicator | None, name
        assert "list" not in str(inspect.signature(getattr(indicators, name)))


def test_indicators_expose_no_aggregate_entry_point() -> None:
    public = {n for n in dir(indicators) if not n.startswith("_")}
    for banned in ("score", "rating", "overall", "aggregate", "rank", "grade"):
        assert not any(banned in name.lower() for name in public), banned
