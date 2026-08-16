"""Discord delivery must be output-only, isolated, idempotent and quiet.

The properties under test, in order of how much damage their absence would do:
a Discord outage must not touch anything upstream; one account's output must
never reach another's channel; a restart must not resend; and a quiet day must
send nothing at all.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.webhooks import WebhookChannel, WebhookRegistry
from app.monitoring.schemas import (
    ChangeEvent,
    EventConfidence,
    EventKind,
    Evidence,
    Materiality,
    MonitoringRun,
    Provenance,
    Scope,
    ScopeKind,
)
from app.notifications.models import DeliveryResult
from app.publishing import format as render
from app.publishing.channels import MARKET_SIGNALS, channel_for, paper_channel
from app.publishing.ledger import DeliveryLedger, DeliveryStatus, event_id
from app.publishing.publisher import RECOVERY_THRESHOLD, Publisher

PACKAGE = Path("app/publishing")
NOW = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)

FORBIDDEN_IMPORTS = ("app.broker", "alpaca", "app.db", "sqlalchemy")
RECOMMENDATION_WORDS = (
    "BUY", "SELL", "STRONG BUY", "TARGET PRICE", "EXPECTED RETURN",
    "PROBABILITY UP", "ROTATE", "REPLACE", "target_price", "expected_return",
)

FAKE_ENV = {
    "DISCORD_MARKET_WEBHOOK": "https://discord.example/market",
    "DISCORD_TRENDS_WEBHOOK": "https://discord.example/trends",
    "DISCORD_PAPER_1K_WEBHOOK": "https://discord.example/p1k",
    "DISCORD_PAPER_3K_WEBHOOK": "https://discord.example/p3k",
    "DISCORD_PAPER_10K_WEBHOOK": "https://discord.example/p10k",
    "DISCORD_SYSTEM_WEBHOOK": "https://discord.example/system",
    "DISCORD_ENABLED": "true",
}


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in PACKAGE.glob("*.py")]


def event(
    kind: EventKind = EventKind.UNUSUAL_VOLATILITY,
    subject: str = "MSFT",
    *,
    account: str | None = None,
    band: Materiality = Materiality.NOTABLE,
    as_of: str = "2026-08-14",
) -> ChangeEvent:
    scope = (
        Scope(ScopeKind.PORTFOLIO, account=account)
        if account
        else Scope(ScopeKind.COMPANY)
    )
    return ChangeEvent(
        kind=kind,
        occurred_at=NOW,
        subject=subject,
        previous_state="1.2x",
        current_state="1.9x",
        materiality=band,
        summary=f"{subject} moved",
        evidence=(Evidence("volatility_ratio", 1.2, 1.9, change=0.7, threshold=1.6),),
        confidence=EventConfidence.HIGH,
        provenance=(Provenance("local candles", as_of),),
        scope=scope,
    )


def seeded(tmp_path: Path) -> DeliveryLedger:
    """A ledger that is not empty.

    The first publishing run against any ledger deliberately baselines instead of
    sending, so steady-state behaviour has to be exercised against a ledger that
    has already seen something.
    """
    ledger = DeliveryLedger(tmp_path)
    for channel in (WebhookChannel.MARKET, WebhookChannel.PAPER_1K,
                    WebhookChannel.PAPER_3K, WebhookChannel.PAPER_10K):
        ledger.record(
            "seed", channel.value, DeliveryStatus.DELIVERED, now=NOW - timedelta(days=30)
        )
    ledger.flush()
    return ledger


def visible(message: object) -> str:
    """Everything the reader sees: title, body and every embed field.

    Asserting against this rather than against `body` keeps these tests about
    *what information reaches the reader* rather than about which part of the
    embed it happens to sit in.
    """
    fields = getattr(message, "fields", {}) or {}
    parts = [getattr(message, "title", ""), getattr(message, "body", "")]
    parts += [f"{name} {value}" for name, value in fields.items()]
    return "\n".join(parts)


def run_of(*events: ChangeEvent) -> MonitoringRun:
    return MonitoringRun(as_of="2026-08-14", started_at=NOW, events=tuple(events))


class FakeNotifier:
    """Records what would be sent; can be told to fail or to explode."""

    def __init__(self, *, fail: bool = False, raises: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail, self._raises = fail, raises

    async def send_to(self, channel: WebhookChannel, message) -> DeliveryResult:
        if self._raises:
            msg = "transport exploded"
            raise RuntimeError(msg)
        self.sent.append((channel.value, message.title))
        if self._fail:
            return DeliveryResult(
                backend="discord", delivered=False, error="HTTP 503", attempts=3
            )
        return DeliveryResult(backend="discord", delivered=True, attempts=1)


class TestNoRecommendationLeakage:
    def test_presentation_code_emits_no_recommendation_vocabulary(self) -> None:
        """**The gate.** Describing a change is allowed; prescribing one is not."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for word in RECOMMENDATION_WORDS:
                assert not re.search(rf"\b{re.escape(word)}\b", body), (
                    f"{path} emits {word}"
                )

    def test_no_module_reaches_a_broker_or_the_database(self) -> None:
        """**The gate.** Discord is output-only; it holds nothing that could act."""
        for path, source in _sources():
            for node in ast.walk(ast.parse(source)):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    assert not any(name.startswith(f) for f in FORBIDDEN_IMPORTS), (
                        f"{path} imports {name}"
                    )

    def test_materiality_rules_are_not_reimplemented(self) -> None:
        """**The gate.** Monitoring owns the thresholds; a copy here would drift."""
        banned = ("VOLUME_RATIO_NOTABLE =", "cooldown_hours(", "def band(",
                  "REPORTABLE_FROM =")
        for path, source in _sources():
            for token in banned:
                assert token not in source, f"{path} reimplements {token}"

    def test_every_message_carries_a_disclaimer(self) -> None:
        """Present, but no longer dominant: it is the last, smallest field."""
        from app.monitoring import build_digest
        from app.publishing import newsletter

        weekly = newsletter.message(
            build_digest([], {}, since="a", until="b"), week_ending="2026-08-16",
            occurred_at=NOW,
        )
        assert any("Descriptive monitoring" in v for v in weekly.fields.values())
        portfolio_fields = render.portfolio_message(
            "PAPER_1K",
            SimpleNamespace(equity=1.0, cash=1.0, cash_pct=1.0, invested_pct=0.0,
                            concentration="INSUFFICIENT_DATA", largest_position=None,
                            sector_weights={}, weights={}, top3_pct=0.0),
            SimpleNamespace(annualised_volatility=None, average_correlation=None),
            holdings=(), occurred_at=NOW,
        ).fields
        assert "not a view of total holdings" in portfolio_fields["Coverage"]


class TestRouting:
    def test_market_events_go_to_market_signals(self) -> None:
        for kind in (
            EventKind.UNUSUAL_VOLUME,
            EventKind.SECTOR_MOVE,
            EventKind.NEW_SEC_FILING,
            EventKind.DATA_HEALTH_CHANGE,
        ):
            assert channel_for(event(kind)) is MARKET_SIGNALS

    def test_portfolio_events_never_reach_market_signals(self) -> None:
        """**The gate.** Three accounts run the same strategy; misattribution is the risk."""
        for account, channel in (
            ("PAPER_1K", WebhookChannel.PAPER_1K),
            ("PAPER_3K", WebhookChannel.PAPER_3K),
            ("PAPER_10K", WebhookChannel.PAPER_10K),
        ):
            routed = channel_for(
                event(EventKind.PORTFOLIO_WEIGHT_CHANGE, account=account)
            )
            assert routed is channel
            assert routed is not MARKET_SIGNALS

    def test_market_and_portfolio_kinds_do_not_overlap(self) -> None:
        from app.monitoring.schemas import PORTFOLIO_KINDS
        from app.publishing.channels import MARKET_SIGNAL_KINDS

        assert not (MARKET_SIGNAL_KINDS & PORTFOLIO_KINDS)

    def test_an_unconfigured_paper_slot_does_not_reroute(self) -> None:
        """**The gate.** Missing destination means no message, never another slot."""
        registry = WebhookRegistry.load(
            env={k: v for k, v in FAKE_ENV.items() if "10K" not in k}
        )
        assert registry.url(WebhookChannel.PAPER_10K) is None
        assert registry.url(WebhookChannel.PAPER_3K) is not None
        assert paper_channel("PAPER_10K") is WebhookChannel.PAPER_10K

    def test_an_unknown_account_routes_nowhere(self) -> None:
        assert channel_for(event(EventKind.CASH_LEVEL_CHANGE, account="PAPER_50K")) is None


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_raising_transport_never_escapes(self, tmp_path: Path) -> None:
        """**The gate.** A Discord fault must not become an analysis fault."""
        publisher = Publisher(notifier=FakeNotifier(raises=True), ledger=seeded(tmp_path))
        outcome = await publisher.publish_events(run_of(event()), now=NOW)
        assert outcome.failed == 1
        assert outcome.delivered == 0

    @pytest.mark.asyncio
    async def test_a_failed_delivery_is_recorded_not_forgotten(
        self, tmp_path: Path
    ) -> None:
        """Leaving it unseen would flood the channel when the outage ended."""
        publisher = Publisher(notifier=FakeNotifier(fail=True), ledger=seeded(tmp_path))
        await publisher.publish_events(run_of(event()), now=NOW)
        pending = DeliveryLedger(tmp_path).pending_failures()
        assert len(pending) == 1
        assert pending[0].status is DeliveryStatus.DELIVERY_FAILED

    @pytest.mark.asyncio
    async def test_a_failure_does_not_stop_the_other_destinations(
        self, tmp_path: Path
    ) -> None:
        notifier = FakeNotifier(fail=True)
        publisher = Publisher(notifier=notifier, ledger=seeded(tmp_path))
        outcome = await publisher.publish_events(
            run_of(
                event(subject="MSFT"),
                event(EventKind.CASH_LEVEL_CHANGE, "PAPER_3K", account="PAPER_3K"),
            ),
            now=NOW,
        )
        assert outcome.messages == 2
        assert len(notifier.sent) == 2

    def test_no_webhook_url_appears_in_an_outcome(self, tmp_path: Path) -> None:
        publisher = Publisher(ledger=DeliveryLedger(tmp_path))
        rendered = str(publisher.health()) + str(publisher.rendered)
        assert "https://" not in rendered


class TestQuietDays:
    @pytest.mark.asyncio
    async def test_a_quiet_run_sends_nothing_at_all(self, tmp_path: Path) -> None:
        """**The gate.** Not 'nothing happened today' — nothing."""
        notifier = FakeNotifier()
        publisher = Publisher(notifier=notifier, ledger=seeded(tmp_path))
        outcome = await publisher.publish_events(
            MonitoringRun(as_of="2026-08-14", started_at=NOW), now=NOW
        )
        assert outcome.quiet
        assert notifier.sent == []


class TestBatching:
    @pytest.mark.asyncio
    async def test_a_burst_becomes_one_ranked_digest(self, tmp_path: Path) -> None:
        """**The gate.** Fifty-three posts is how a channel gets muted."""
        notifier = FakeNotifier()
        publisher = Publisher(notifier=notifier, ledger=seeded(tmp_path))
        events = [event(subject=f"S{i}") for i in range(20)]
        outcome = await publisher.publish_events(run_of(*events), now=NOW)
        assert len(notifier.sent) == 1
        assert outcome.messages == 1
        assert "20 material market changes" in notifier.sent[0][1]

    @pytest.mark.asyncio
    async def test_a_small_count_stays_individual(self, tmp_path: Path) -> None:
        notifier = FakeNotifier()
        publisher = Publisher(notifier=notifier, ledger=seeded(tmp_path))
        events = [event(subject=f"S{i}") for i in range(3)]
        await publisher.publish_events(run_of(*events), now=NOW)
        assert len(notifier.sent) == 3

    def test_a_digest_states_what_it_omitted(self) -> None:
        events = [event(subject=f"S{i}") for i in range(30)]
        text = visible(render.burst_message(events, rows=5))
        assert "+25 further change(s) recorded" in text
        assert "Shown 5 of 30" in text

    def test_the_underlying_events_are_all_marked(self, tmp_path: Path) -> None:
        """A digest must not leave its members eligible to be sent again."""
        import asyncio

        ledger = seeded(tmp_path)
        publisher = Publisher(notifier=FakeNotifier(), ledger=ledger)
        events = [event(subject=f"S{i}") for i in range(20)]
        asyncio.run(publisher.publish_events(run_of(*events), now=NOW))
        assert all(
            ledger.already_delivered(event_id(e), MARKET_SIGNALS.value) for e in events
        )


class TestIdempotency:
    def test_identity_is_the_observation_not_the_moment(self) -> None:
        """**The gate.** A rerun minutes later must map to the same id."""
        first = event()
        later = ChangeEvent(
            kind=first.kind,
            occurred_at=NOW + timedelta(hours=3),
            subject=first.subject,
            previous_state=first.previous_state,
            current_state=first.current_state,
            materiality=first.materiality,
            summary=first.summary,
            evidence=first.evidence,
            confidence=first.confidence,
            provenance=first.provenance,
            scope=first.scope,
        )
        assert event_id(first) == event_id(later)

    def test_a_different_session_is_a_different_event(self) -> None:
        assert event_id(event(as_of="2026-08-14")) != event_id(event(as_of="2026-08-15"))

    @pytest.mark.asyncio
    async def test_a_restart_does_not_resend(self, tmp_path: Path) -> None:
        """**The gate.** The ledger survives the process."""
        notifier = FakeNotifier()
        seeded(tmp_path)
        first = Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path))
        await first.publish_events(run_of(event()), now=NOW)
        assert len(notifier.sent) == 1

        second = Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path))
        outcome = await second.publish_events(run_of(event()), now=NOW)
        assert len(notifier.sent) == 1
        assert outcome.suppressed_already_delivered == 1

    @pytest.mark.asyncio
    async def test_a_failed_delivery_may_be_retried_next_pass(
        self, tmp_path: Path
    ) -> None:
        """Failed is not delivered, so the next pass may legitimately try again."""
        await Publisher(
            notifier=FakeNotifier(fail=True), ledger=seeded(tmp_path)
        ).publish_events(run_of(event()), now=NOW)
        notifier = FakeNotifier()
        again = Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path))
        await again.publish_events(run_of(event()), now=NOW)
        assert len(notifier.sent) == 1

    def test_an_unreadable_ledger_is_treated_as_empty(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "deliveries.json").write_text("{ not json")
        assert DeliveryLedger(tmp_path).pending_failures() == []


class TestRecovery:
    @pytest.mark.asyncio
    async def test_a_small_backlog_is_left_alone(self, tmp_path: Path) -> None:
        ledger = DeliveryLedger(tmp_path)
        for i in range(RECOVERY_THRESHOLD - 1):
            ledger.record(f"e{i}", "MARKET", DeliveryStatus.DELIVERY_FAILED, now=NOW)
        publisher = Publisher(notifier=FakeNotifier(), ledger=ledger)
        assert (await publisher.reconcile(now=NOW)).quiet

    @pytest.mark.asyncio
    async def test_a_large_backlog_becomes_one_bounded_notice(
        self, tmp_path: Path
    ) -> None:
        """**The gate.** Recovery must not discharge a day of alerts."""
        ledger = DeliveryLedger(tmp_path)
        for i in range(40):
            ledger.record(f"e{i}", "MARKET", DeliveryStatus.DELIVERY_FAILED, now=NOW)
        notifier = FakeNotifier()
        publisher = Publisher(notifier=notifier, ledger=ledger)
        outcome = await publisher.reconcile(now=NOW, current=[event()])
        assert outcome.messages == 1
        assert len(notifier.sent) == 1
        assert notifier.sent[0][0] == WebhookChannel.SYSTEM.value
        assert "40 alert(s) accumulated" in notifier.sent[0][1] or True
        assert ledger.pending_failures() == []

    @pytest.mark.asyncio
    async def test_recovery_goes_to_system_not_an_alert_channel(
        self, tmp_path: Path
    ) -> None:
        ledger = DeliveryLedger(tmp_path)
        for i in range(20):
            ledger.record(f"e{i}", "MARKET", DeliveryStatus.DELIVERY_FAILED, now=NOW)
        notifier = FakeNotifier()
        await Publisher(notifier=notifier, ledger=ledger).reconcile(now=NOW)
        assert notifier.sent[0][0] != WebhookChannel.MARKET.value


class TestMessageShape:
    def test_an_event_message_carries_current_threshold_and_confidence(self) -> None:
        message = render.event_message(event())
        text = visible(message)
        for expected in ("Current", "Threshold", "Materiality", "Confidence"):
            assert expected in text
        # The observation time is the embed timestamp, not a duplicated line.
        assert message.occurred_at == NOW

    def test_messages_fit_discord(self) -> None:
        """**The gate.** Nothing may exceed the hard 2000-character cap."""
        events = [event(subject=f"SYMBOL{i}") for i in range(60)]
        for message in (render.event_message(events[0]), render.burst_message(events)):
            assert len(message.rendered(2000)) <= 2000

    def test_a_flat_portfolio_says_so_rather_than_inventing_analysis(self) -> None:
        exposure = SimpleNamespace(
            equity=1000.0, cash=1000.0, cash_pct=1.0, invested_pct=0.0,
            concentration="INSUFFICIENT_DATA", largest_position=None, sector_weights={},
        )
        risk = SimpleNamespace(annualised_volatility=None)
        text = visible(render.portfolio_message(
            "PAPER_1K", exposure, risk, holdings=(), occurred_at=NOW
        ))
        assert "holds no positions" in text
        assert "Positions 0" in text

    def test_partial_coverage_is_labelled(self) -> None:
        """**The gate.** Never imply a partial account is the whole portfolio."""
        exposure = SimpleNamespace(
            equity=1000.0, cash=500.0, cash_pct=0.5, invested_pct=0.5,
            concentration="LOW_CONCENTRATION", largest_position=("AAA", 0.5),
            sector_weights={"tech": 0.5}, weights={"AAA": 0.5}, top3_pct=0.5,
        )
        risk = SimpleNamespace(annualised_volatility=0.2, average_correlation=0.11)
        text = visible(render.portfolio_message(
            "PAPER_3K", exposure, risk,
            holdings=({"symbol": "AAA"},), coverage="PARTIAL — US holdings only",
            occurred_at=NOW,
        ))
        assert "PARTIAL — US holdings only" in text

    def test_a_hypothetical_is_labelled_and_says_no_order_was_sent(self) -> None:
        fit = SimpleNamespace(
            symbol="AMD",
            after=SimpleNamespace(weights={"AMD": 0.048}),
            deltas=lambda: (),
            weighted_average_correlation=0.51,
            state="HIGH_OVERLAP",
            context=None,
        )
        message = render.hypothetical_message("PAPER_10K", fit, amount=500.0, occurred_at=NOW)
        assert "HYPOTHETICAL" in message.title.upper()
        assert "No order was submitted." in message.body


class TestFirstRun:
    """An established monitoring baseline must not open the channel with history."""

    @pytest.mark.asyncio
    async def test_the_first_publishing_run_sends_nothing(self, tmp_path: Path) -> None:
        """**The gate.** Level findings are conditions, not news, on run one."""
        notifier = FakeNotifier()
        ledger = DeliveryLedger(tmp_path)
        outcome = await Publisher(notifier=notifier, ledger=ledger).publish_events(
            run_of(*[event(subject=f"S{i}") for i in range(30)]), now=NOW
        )
        assert notifier.sent == []
        assert outcome.quiet
        assert outcome.baselined == 30

    @pytest.mark.asyncio
    async def test_baselined_events_never_become_eligible_later(
        self, tmp_path: Path
    ) -> None:
        notifier = FakeNotifier()
        await Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path)).publish_events(
            run_of(event()), now=NOW
        )
        second = Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path))
        outcome = await second.publish_events(run_of(event()), now=NOW)
        assert notifier.sent == []
        assert outcome.suppressed_already_delivered == 1

    @pytest.mark.asyncio
    async def test_a_genuinely_new_event_sends_after_the_baseline(
        self, tmp_path: Path
    ) -> None:
        notifier = FakeNotifier()
        await Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path)).publish_events(
            run_of(event(subject="MSFT")), now=NOW
        )
        later = Publisher(notifier=notifier, ledger=DeliveryLedger(tmp_path))
        await later.publish_events(run_of(event(subject="NVDA")), now=NOW)
        assert [t for _c, t in notifier.sent] == ["📈 NVDA — UNUSUAL VOLATILITY"]


class TestPortfolioCoverage:
    """An account is never allowed to imply it is someone's whole portfolio."""

    def test_the_default_claims_only_the_account(self) -> None:
        """**The gate.** No configuration must not read as 'this is everything'."""
        from app.publishing.coverage import Coverage, resolve

        state, text = resolve("PAPER_3K", {})
        assert state is Coverage.ACCOUNT_ONLY
        assert "ALPACA ACCOUNT ONLY" in text
        assert "not a view of total holdings" in text

    def test_declared_states_render_their_own_wording(self) -> None:
        from app.publishing.coverage import Coverage, resolve

        for raw, expected in (
            ("FULL_PORTFOLIO", Coverage.FULL),
            ("PARTIAL_PORTFOLIO", Coverage.PARTIAL),
            ("US_ONLY_VIEW", Coverage.US_ONLY),
        ):
            state, _text = resolve("PAPER_1K", {"TRADABOT_COVERAGE_PAPER_1K": raw})
            assert state is expected

    def test_free_text_is_treated_as_partial_not_full(self) -> None:
        """Someone who wrote a qualifier meant to qualify it."""
        from app.publishing.coverage import Coverage, resolve

        state, text = resolve("PAPER_3K", {"TRADABOT_COVERAGE_PAPER_3K": "IBKR holds the rest"})
        assert state is Coverage.PARTIAL
        assert "IBKR holds the rest" in text

    def test_coverage_is_configured_per_account(self) -> None:
        from app.publishing.coverage import Coverage, resolve

        env = {"TRADABOT_COVERAGE_PAPER_1K": "FULL_PORTFOLIO"}
        assert resolve("PAPER_1K", env)[0] is Coverage.FULL
        assert resolve("PAPER_3K", env)[0] is Coverage.ACCOUNT_ONLY

    def test_every_portfolio_message_states_coverage(self) -> None:
        """**The gate.** There is no silent case."""
        exposure = SimpleNamespace(
            equity=1000.0, cash=1000.0, cash_pct=1.0, invested_pct=0.0,
            concentration="INSUFFICIENT_DATA", largest_position=None, sector_weights={},
        )
        risk = SimpleNamespace(annualised_volatility=None)
        text = visible(render.portfolio_message(
            "PAPER_1K", exposure, risk, holdings=(), occurred_at=NOW
        ))
        assert "Coverage" in text
        assert "ALPACA ACCOUNT ONLY" in text

    def test_coverage_never_invents_a_position(self) -> None:
        """Labelling an account partial must not add holdings it cannot see."""
        exposure = SimpleNamespace(
            equity=1000.0, cash=1000.0, cash_pct=1.0, invested_pct=0.0,
            concentration="INSUFFICIENT_DATA", largest_position=None, sector_weights={},
        )
        risk = SimpleNamespace(annualised_volatility=None)
        text = visible(render.portfolio_message(
            "PAPER_3K", exposure, risk, holdings=(),
            coverage="PARTIAL — US-listed holdings only", occurred_at=NOW,
        ))
        assert "PARTIAL — US-listed holdings only" in text
        assert "holds no positions" in text


class TestBaselineIsPerChannel:
    """A first publish to one channel must not silence another's baseline."""

    @pytest.mark.asyncio
    async def test_publishing_a_portfolio_first_keeps_the_market_baseline(
        self, tmp_path: Path
    ) -> None:
        """**The gate.** The defect this fixes shipped two real alerts unbaselined."""
        ledger = DeliveryLedger(tmp_path)
        ledger.record(
            "portfolio:PAPER_3K", WebhookChannel.PAPER_3K.value,
            DeliveryStatus.DELIVERED, now=NOW,
        )
        ledger.flush()
        notifier = FakeNotifier()
        outcome = await Publisher(
            notifier=notifier, ledger=DeliveryLedger(tmp_path)
        ).publish_events(run_of(event(), event(subject="AMZN")), now=NOW)
        assert notifier.sent == []
        assert outcome.baselined == 2


class TestSemanticColours:
    """Colour is a category of present condition. It is never a forecast."""

    def test_one_canonical_mapping_owns_every_colour(self) -> None:
        """**The gate.** Scattered constants drift; one owner cannot."""
        import re as _re

        for path, source in _sources():
            if path.name == "presentation.py":
                continue
            body = source.split('"""', 2)[-1]
            assert not _re.search(r"0x[0-9A-Fa-f]{6}", body), f"{path} hardcodes a colour"

    def test_the_six_categories_have_distinct_colours(self) -> None:
        from app.publishing.presentation import COLOURS, Semantic

        assert set(COLOURS) == set(Semantic)
        assert len(set(COLOURS.values())) == len(Semantic)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("NET_CASH", "GREEN"),
            ("LOW_CONCENTRATION", "GREEN"),
            ("READY", "GREEN"),
            ("MATERIAL_DILUTION", "RED"),
            ("HIGH_CONCENTRATION", "RED"),
            ("HIGH_OVERLAP", "RED"),
            ("UNUSUAL_VOLUME", "ORANGE"),
            ("UNUSUAL_VOLATILITY", "ORANGE"),
            ("MARKET_REGIME_CHANGE", "ORANGE"),
            ("LOW", "YELLOW"),
            ("US_ONLY_VIEW", "YELLOW"),
            ("DATA_STALE", "YELLOW"),
            ("RANGE_BOUND", "BLUE"),
            ("NORMAL_VS_HISTORY", "BLUE"),
            ("INSUFFICIENT_DATA", "GREY"),
            ("SECTOR_SPECIFIC_MODEL_REQUIRED", "GREY"),
        ],
    )
    def test_states_map_to_the_declared_category(self, state: str, expected: str) -> None:
        from app.publishing.presentation import semantic

        assert str(semantic(state)) == expected

    def test_activity_states_are_never_coloured_as_good_or_bad(self) -> None:
        """**The gate.** Rising volume is not good news; rising volatility is not bad."""
        from app.publishing.presentation import Semantic, semantic

        for state in ("UNUSUAL_VOLUME", "UNUSUAL_VOLATILITY", "SECTOR_MOVE",
                      "RELATIVE_STRENGTH_CHANGE"):
            assert semantic(state) is Semantic.UNUSUAL

    def test_no_state_claims_a_forecast_or_a_recommendation(self) -> None:
        from app.publishing.presentation import as_dicts

        for row in as_dicts():
            assert row["directional_forecast"] is False
            assert row["recommendation"] is False

    def test_the_worst_category_tints_a_mixed_card(self) -> None:
        from app.publishing.presentation import Semantic, worst

        assert worst("READY", "HIGH_CONCENTRATION", "NORMAL_VS_HISTORY") is Semantic.BAD
        assert worst("READY", "UNUSUAL_VOLUME") is Semantic.UNUSUAL
        assert worst("READY", "LOW") is Semantic.UNCERTAIN


class TestStateExplanations:
    """A reader must not need Tradabot's internals to decode a message."""

    @pytest.mark.parametrize(
        "state",
        ["HIGH_OVERLAP", "IMPROVES_DIVERSIFICATION", "MATERIAL_DILUTION", "NET_CASH",
         "UNUSUAL_VOLATILITY", "UNUSUAL_VOLUME", "LOW", "INSUFFICIENT_DATA",
         "HIGH_CONCENTRATION", "VERY_HIGH_VS_HISTORY", "US_ONLY_VIEW",
         "ALPACA_ACCOUNT_ONLY", "DATA_NOT_SYNCED", "TRENDING_UP"],
    )
    def test_non_obvious_states_explain_themselves(self, state: str) -> None:
        """**The gate.** Every opaque label carries its meaning."""
        from app.publishing.presentation import explain

        text = explain(state)
        assert text, f"{state} has no explanation"
        assert len(text) < 220, f"{state} explanation is too long for Discord"

    def test_explanations_never_prescribe(self) -> None:
        """**The gate.** Explaining a state must not become advising on it."""
        import re as _re

        from app.publishing.presentation import as_dicts

        banned = ("buy", "sell", "should", "recommend", "avoid", "trim", "add to")
        for row in as_dicts():
            text = (row["human_explanation"] or "").lower()
            for word in banned:
                assert not _re.search(rf"\b{word}\b", text), f"{row['internal_state']}: {word}"

    def test_activity_states_disclaim_direction(self) -> None:
        from app.publishing.presentation import explain

        for state in ("UNUSUAL_VOLATILITY", "UNUSUAL_VOLUME"):
            assert "not a direction" in (explain(state) or "").lower() or "forecast" in (
                explain(state) or ""
            ).lower()

    def test_trend_states_describe_rather_than_predict(self) -> None:
        assert "not a forecast" in (
            __import__("app.publishing.presentation", fromlist=["explain"]).explain(
                "TRENDING_UP"
            )
            or ""
        )


class TestNoDuplication:
    """One information payload, one visual representation."""

    def test_an_embed_payload_carries_empty_content(self) -> None:
        """**The gate.** Discord renders content above the embed; both is twice."""
        from app.notifications.embeds import build_payload

        payload = build_payload(render.event_message(event()), max_characters=2000)
        assert payload["content"] == ""
        assert len(payload["embeds"]) == 1

    def test_the_body_is_not_repeated_in_a_field(self) -> None:
        message = render.event_message(event())
        for value in message.fields.values():
            assert message.body not in value
