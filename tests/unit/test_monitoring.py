"""The monitoring engine must be able to say "nothing happened".

That is the property under test throughout. An engine that reports something
every run is worthless in a way that is hard to notice, because every individual
message it sends is true. So the tests below care less about whether a given
change is detected than about whether an *unremarkable* one is kept quiet.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.monitoring import (
    Bars,
    EventConfidence,
    EventJournal,
    EventKind,
    InMemoryStateStore,
    JsonStateStore,
    Materiality,
    MonitoringEngine,
    MonitoringInputs,
    build_digest,
    detectors,
    rank,
    weakest,
)
from app.monitoring import materiality as rules
from app.monitoring.digest import (
    biggest_market_changes,
    most_important_portfolio_changes,
)
from app.monitoring.schemas import ChangeEvent, Evidence, Scope, ScopeKind

PACKAGE = Path("app/monitoring")
NOW = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)

FORBIDDEN_IMPORTS = ("app.broker", "alpaca", "app.db", "sqlalchemy")
ACTION_WORDS = ("BUY", "SELL", "ROTATE", "REPLACE", "REDUCE", "INCREASE",
                "target_weight", "expected_return", "recommend")


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in PACKAGE.glob("*.py")]


def _flat(closes: dict[str, float], volumes: dict[str, float] | None = None) -> Bars:
    return Bars(closes, volumes or dict.fromkeys(closes, 1_000.0))


def _series(days: int, start: str = "2025-01-01", price: float = 100.0,
            volume: float = 1_000.0) -> tuple[dict[str, float], dict[str, float]]:
    base = datetime.fromisoformat(start)
    closes, volumes = {}, {}
    for i in range(days):
        day = (base + timedelta(days=i)).date().isoformat()
        closes[day] = price
        volumes[day] = volume
    return closes, volumes


class TestCannotTradeOrRecommend:
    def test_no_module_reaches_a_broker_or_the_database(self) -> None:
        """**The gate.** Monitoring observes; it holds no handle that could act."""
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

    def test_no_action_vocabulary(self) -> None:
        """**The gate.** Naming a change is allowed; prescribing a response is not."""
        for path, source in _sources():
            body = source.split('"""', 2)[-1]
            for word in ACTION_WORDS:
                assert not re.search(rf"\b{word}\b", body), f"{path} emits {word}"

    def test_no_forward_looking_input(self) -> None:
        """**The gate.** Every observation is bounded by ``as_of``.

        A monitoring layer that could see the next session's price would be an
        alpha model, and this repository has established it has no validated one.
        """
        source = (PACKAGE / "observations.py").read_text()
        assert "upto(" in source
        # The module docstring promises exactly this, so scan the code below it
        # rather than matching the promise itself, and skip the __future__
        # import, which is a language feature and not a data source.
        body = "\n".join(
            line
            for line in source.split('"""', 2)[-1].splitlines()
            if "__future__" not in line
        )
        for forbidden in ("y_r_", "forward", "future", "next_session", "shift(-"):
            assert forbidden not in body, f"observations reference {forbidden}"


class TestNothingHappened:
    @staticmethod
    def _inputs(as_of: str = "2025-06-30") -> MonitoringInputs:
        closes, volumes = _series(400)
        return MonitoringInputs(
            as_of=as_of,
            bars={"SPY": Bars(closes, volumes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
            sectors={"AAA": "tech"},
        )

    def test_a_flat_market_reports_nothing(self) -> None:
        """**The gate.** Unchanging inputs must produce silence, not a heartbeat."""
        store = InMemoryStateStore()
        MonitoringEngine(store, now=NOW).run(self._inputs())
        run = MonitoringEngine(store, now=NOW + timedelta(days=1)).run(self._inputs())
        assert run.quiet
        assert run.events == ()

    def test_the_first_run_establishes_a_baseline_without_announcing_it(self) -> None:
        """A fresh install must not narrate the entire current state of the world."""
        run = MonitoringEngine(InMemoryStateStore(), now=NOW).run(self._inputs())
        assert run.quiet

    def test_a_quiet_run_still_reports_what_it_examined(self) -> None:
        run = MonitoringEngine(InMemoryStateStore(), now=NOW).run(self._inputs())
        assert run.subjects_examined >= 2


class TestSomethingHappened:
    def test_a_volume_spike_is_reported(self) -> None:
        closes, volumes = _series(300)
        spike = max(volumes)
        volumes[spike] = 1_000.0 * (rules.VOLUME_RATIO_SIGNIFICANT + 1)
        inputs = MonitoringInputs(
            as_of=spike,
            bars={"SPY": _flat(closes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
        )
        run = MonitoringEngine(InMemoryStateStore(), now=NOW).run(inputs)
        assert not run.quiet
        event = run.events[0]
        assert event.kind is EventKind.UNUSUAL_VOLUME
        assert event.materiality is Materiality.SIGNIFICANT
        assert event.evidence[0].threshold == rules.VOLUME_RATIO_NOTABLE

    def test_a_volume_ratio_below_the_threshold_is_not_reported(self) -> None:
        """**The gate.** The threshold has to actually keep something out."""
        closes, volumes = _series(300)
        day = max(volumes)
        volumes[day] = 1_000.0 * (rules.VOLUME_RATIO_NOTABLE - 0.5)
        inputs = MonitoringInputs(
            as_of=day,
            bars={"SPY": _flat(closes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
        )
        assert MonitoringEngine(InMemoryStateStore(), now=NOW).run(inputs).quiet

    def test_a_quiet_session_is_never_unusual_volume(self) -> None:
        """Low volume is common and uninformative; only elevation is unusual."""
        closes, volumes = _series(300)
        day = max(volumes)
        volumes[day] = 1.0
        inputs = MonitoringInputs(
            as_of=day,
            bars={"SPY": _flat(closes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
        )
        assert MonitoringEngine(InMemoryStateStore(), now=NOW).run(inputs).quiet


class TestMaterialityRules:
    def test_bands_are_symmetric_in_magnitude(self) -> None:
        assert rules.band(0.30, 0.10, 0.25) is Materiality.SIGNIFICANT
        assert rules.band(-0.30, 0.10, 0.25) is Materiality.SIGNIFICANT
        assert rules.band(0.05, 0.10, 0.25) is Materiality.ROUTINE

    def test_portfolio_thresholds_are_borrowed_not_redeclared(self) -> None:
        """**The gate.** One calibration, one owner; a copy would drift silently."""
        from app.portfolio_fit import CORRELATION_PERCENTILES, MATERIAL_WEIGHT_SHIFT

        assert rules.WEIGHT_SHIFT == MATERIAL_WEIGHT_SHIFT
        assert rules.CORRELATION_BANDS == CORRELATION_PERCENTILES

    def test_the_threshold_record_says_it_is_not_outcome_fitted(self) -> None:
        assert rules.as_dict()["fitted_to_forward_returns"] is False

    def test_confidence_is_the_minimum(self) -> None:
        assert weakest(EventConfidence.HIGH, EventConfidence.LOW) is EventConfidence.LOW
        assert weakest() is EventConfidence.INSUFFICIENT


class TestDetectors:
    def test_a_regime_flip_must_persist_before_it_is_announced(self) -> None:
        """**The gate.** A two-day wobble is not a regime change."""
        previous = {"regime": "TRENDING_UP", "distance_from_ma200": 0.05}
        current = {"regime": "TRENDING_DOWN", "distance_from_ma200": -0.04}
        early = detectors.detect_market_regime(
            previous, current, now=NOW, as_of="2026-08-14",
            sessions_in_state=rules.REGIME_MIN_SESSIONS_IN_STATE - 1,
        )
        assert early == []
        confirmed = detectors.detect_market_regime(
            previous, current, now=NOW, as_of="2026-08-14",
            sessions_in_state=rules.REGIME_MIN_SESSIONS_IN_STATE,
        )
        assert len(confirmed) == 1
        assert confirmed[0].current_state == "TRENDING_DOWN"

    def test_a_new_filing_is_identified_by_its_accession(self) -> None:
        previous = {"latest_accession": "0001-25-A", "latest_form": "10-Q"}
        current = {"latest_accession": "0001-26-A", "latest_form": "10-K",
                   "latest_filed": "2026-01-20"}
        events = detectors.detect_company(
            "AAA", previous, current, now=NOW, as_of="2026-08-14"
        )
        filings = [e for e in events if e.kind is EventKind.NEW_SEC_FILING]
        assert len(filings) == 1
        assert filings[0].materiality is Materiality.SIGNIFICANT
        assert filings[0].key().endswith("0001-26-A")

    def test_an_unchanged_filing_is_not_news(self) -> None:
        state = {"latest_accession": "0001-26-A", "latest_form": "10-K"}
        events = detectors.detect_company(
            "AAA", state, state, now=NOW, as_of="2026-08-14"
        )
        assert [e for e in events if e.kind is EventKind.NEW_SEC_FILING] == []

    def test_a_position_appearing_and_leaving_are_both_events(self) -> None:
        previous = {"positions": ["AAA"], "weights": {"AAA": 0.5}, "cash_pct": 0.5}
        current = {"positions": ["BBB"], "weights": {"BBB": 0.5}, "cash_pct": 0.5}
        kinds = {
            e.kind
            for e in detectors.detect_portfolio(
                "PAPER_3K", previous, current, now=NOW, as_of="2026-08-14"
            )
        }
        assert EventKind.POSITION_ADDED in kinds
        assert EventKind.POSITION_REMOVED in kinds

    def test_a_portfolio_event_names_its_account(self) -> None:
        """**The gate.** One account's change must never be filed under another."""
        events = detectors.detect_portfolio(
            "PAPER_3K",
            {"positions": [], "weights": {}, "cash_pct": 1.0},
            {"positions": ["AAA"], "weights": {"AAA": 0.4}, "cash_pct": 0.6},
            now=NOW,
            as_of="2026-08-14",
        )
        assert all(e.scope.account == "PAPER_3K" for e in events)
        assert all("PAPER_3K" in e.key() for e in events)

    def test_a_correlation_cluster_crossing_is_reported(self) -> None:
        events = detectors.detect_portfolio(
            "PAPER_3K",
            {"positions": ["A"], "weights": {"A": 1.0}, "average_correlation": 0.10},
            {"positions": ["A"], "weights": {"A": 1.0}, "average_correlation": 0.55},
            now=NOW,
            as_of="2026-08-14",
        )
        cluster = [e for e in events if e.kind is EventKind.CORRELATION_CLUSTER_CHANGE]
        assert len(cluster) == 1
        assert cluster[0].current_state == "EXTREME_OVERLAP"

    def test_a_sector_label_caps_a_sector_event_confidence(self) -> None:
        """Proxy-derived labels cannot produce a high-confidence sector claim."""
        events = detectors.detect_portfolio(
            "PAPER_3K",
            {"positions": ["A"], "weights": {"A": 1.0}, "sector_weights": {"tech": 0.1}},
            {"positions": ["A"], "weights": {"A": 1.0}, "sector_weights": {"tech": 0.5}},
            now=NOW,
            as_of="2026-08-14",
            sector_confidence=EventConfidence.LOW,
        )
        sector = [e for e in events if e.kind is EventKind.SECTOR_CONCENTRATION_CHANGE]
        assert sector
        assert all(e.confidence is EventConfidence.LOW for e in sector)

    def test_data_health_loss_is_critical_and_recovery_is_not(self) -> None:
        lost = detectors.detect_health(
            {"status": "READY"}, {"status": "DATA_STALE"}, now=NOW, as_of="2026-08-14"
        )
        back = detectors.detect_health(
            {"status": "DATA_STALE"}, {"status": "READY"}, now=NOW, as_of="2026-08-14"
        )
        assert lost[0].materiality is Materiality.CRITICAL
        assert back[0].materiality is Materiality.NOTABLE


class TestSuppression:
    @staticmethod
    def _spike_inputs(day: str, closes: dict[str, float],
                      volumes: dict[str, float]) -> MonitoringInputs:
        return MonitoringInputs(
            as_of=day,
            bars={"SPY": Bars(closes, volumes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
        )

    def test_a_repeat_inside_the_cooldown_is_suppressed(self) -> None:
        """**The gate.** A condition that persists must not report every session."""
        closes, volumes = _series(300)
        days = sorted(volumes)
        for day in days[-2:]:
            volumes[day] = 1_000.0 * 10
        store = InMemoryStateStore()
        first = MonitoringEngine(store, now=NOW).run(
            self._spike_inputs(days[-2], closes, volumes)
        )
        second = MonitoringEngine(store, now=NOW + timedelta(hours=24)).run(
            self._spike_inputs(days[-1], closes, volumes)
        )
        assert not first.quiet
        assert second.quiet
        assert second.suppressed_cooldown >= 1

    def test_the_same_condition_reports_again_once_the_cooldown_expires(self) -> None:
        closes, volumes = _series(300)
        days = sorted(volumes)
        for day in days[-2:]:
            volumes[day] = 1_000.0 * 10
        store = InMemoryStateStore()
        MonitoringEngine(store, now=NOW).run(self._spike_inputs(days[-2], closes, volumes))
        hours = rules.cooldown_hours(EventKind.UNUSUAL_VOLUME) + 1
        later = MonitoringEngine(store, now=NOW + timedelta(hours=hours)).run(
            self._spike_inputs(days[-1], closes, volumes)
        )
        assert not later.quiet

    def test_duplicates_within_one_run_collapse(self) -> None:
        event = ChangeEvent(
            kind=EventKind.UNUSUAL_VOLUME,
            occurred_at=NOW,
            subject="AAA",
            previous_state=None,
            current_state="3.0x median",
            materiality=Materiality.NOTABLE,
            summary="",
            dedup_key="same",
        )
        engine = MonitoringEngine(InMemoryStateStore(), now=NOW)
        kept, _routine, duplicate, _cooled = engine._filter([event, event], NOW)
        assert len(kept) == 1
        assert duplicate == 1

    def test_routine_changes_are_counted_but_never_reported(self) -> None:
        """A change below its threshold is observed, not announced."""
        events = detectors.detect_company(
            "AAA",
            {"metric_revenue_ttm": 100.0},
            {"metric_revenue_ttm": 101.0},
            now=NOW,
            as_of="2026-08-14",
        )
        assert events
        assert all(e.materiality is Materiality.ROUTINE for e in events)
        assert all(not e.reportable for e in events)


class TestRankingAndDigest:
    @staticmethod
    def _event(kind: EventKind, subject: str, band: Materiality,
               confidence: EventConfidence = EventConfidence.HIGH,
               change: float = 0.0) -> ChangeEvent:
        return ChangeEvent(
            kind=kind,
            occurred_at=NOW,
            subject=subject,
            previous_state="a",
            current_state="b",
            materiality=band,
            summary=f"{subject} moved",
            evidence=(Evidence("m", 0.0, change, change=change),),
            confidence=confidence,
            scope=Scope(ScopeKind.COMPANY),
        )

    def test_materiality_outranks_magnitude(self) -> None:
        small_but_critical = self._event(
            EventKind.DATA_HEALTH_CHANGE, "store", Materiality.CRITICAL, change=0.01
        )
        large_but_notable = self._event(
            EventKind.UNUSUAL_VOLUME, "AAA", Materiality.NOTABLE, change=9.0
        )
        assert rank([large_but_notable, small_but_critical])[0] is small_but_critical

    def test_confidence_breaks_a_materiality_tie(self) -> None:
        solid = self._event(
            EventKind.SECTOR_MOVE, "tech", Materiality.NOTABLE, EventConfidence.HIGH
        )
        shaky = self._event(
            EventKind.SECTOR_MOVE, "energy", Materiality.NOTABLE, EventConfidence.LOW
        )
        assert rank([shaky, solid])[0] is solid

    def test_ranking_is_stable(self) -> None:
        events = [
            self._event(EventKind.SECTOR_MOVE, name, Materiality.NOTABLE)
            for name in ("c", "a", "b")
        ]
        assert [e.subject for e in rank(events)] == [e.subject for e in rank(events[::-1])]

    def test_the_digest_answers_each_question_separately(self) -> None:
        events = [
            self._event(EventKind.UNUSUAL_VOLUME, "AAA", Materiality.NOTABLE).as_dict(),
            self._event(EventKind.SECTOR_MOVE, "tech", Materiality.NOTABLE).as_dict(),
            self._event(
                EventKind.FUNDAMENTAL_CHANGE, "AAA", Materiality.SIGNIFICANT
            ).as_dict(),
            self._event(
                EventKind.VALUATION_STATE_CHANGE, "AAA", Materiality.NOTABLE
            ).as_dict(),
        ]
        digest = build_digest(events, {}, since="2026-08-08", until="2026-08-14")
        titles = {s.title: s for s in digest.sections}
        assert not titles["Market"].empty
        assert not titles["Sectors"].empty
        assert not titles["Company fundamentals"].empty
        assert not titles["Valuation"].empty
        assert titles["Portfolios"].empty
        assert not digest.quiet

    def test_an_empty_period_is_a_quiet_digest(self) -> None:
        digest = build_digest([], {}, since="2026-08-08", until="2026-08-14")
        assert digest.quiet
        assert all(section.empty for section in digest.sections)

    def test_unresolved_risks_come_from_current_state_not_events(self) -> None:
        """A risk that persisted all week emits no event and must still surface."""
        state = {
            "health": {"sec_fact_store": {"status": "DATA_STALE"}},
            "portfolio": {
                "PAPER_3K": {
                    "sector_weights": {"semiconductors": 0.62},
                    "average_correlation": 0.51,
                    "concentration": "HIGH_CONCENTRATION",
                    "top3_pct": 0.9,
                }
            },
        }
        digest = build_digest([], state, since="2026-08-08", until="2026-08-14")
        risks = {r["risk"] for s in digest.sections for r in s.rows if "risk" in r}
        assert "DATA_NOT_READY" in risks
        assert "SECTOR_CONCENTRATED" in risks
        assert not digest.quiet

    def test_a_section_says_how_many_it_omitted(self) -> None:
        events = [
            self._event(EventKind.SECTOR_MOVE, f"s{i}", Materiality.NOTABLE).as_dict()
            for i in range(9)
        ]
        digest = build_digest(events, {}, since="a", until="b", limit=3)
        sectors = next(s for s in digest.sections if s.title == "Sectors")
        assert len(sectors.rows) == 3
        assert sectors.omitted == 6


class TestPersistence:
    def test_state_survives_a_new_process(self, tmp_path: Path) -> None:
        """**The gate.** A restart must not re-announce everything it knows."""
        closes, volumes = _series(300)
        inputs = MonitoringInputs(
            as_of=max(closes),
            bars={"SPY": Bars(closes, volumes), "AAA": Bars(closes, volumes)},
            watched=("AAA",),
        )
        MonitoringEngine(JsonStateStore(tmp_path), now=NOW).run(inputs)
        reopened = JsonStateStore(tmp_path)
        assert reopened.get("symbol", "AAA") is not None
        assert MonitoringEngine(reopened, now=NOW + timedelta(days=1)).run(inputs).quiet

    def test_a_damaged_baseline_is_discarded_not_half_read(self, tmp_path: Path) -> None:
        (tmp_path).mkdir(parents=True, exist_ok=True)
        (tmp_path / "symbol.json").write_text("{ not json")
        assert JsonStateStore(tmp_path).get("symbol", "AAA") is None

    def test_the_journal_round_trips_and_filters_by_date(self, tmp_path: Path) -> None:
        journal = EventJournal(tmp_path)
        event = ChangeEvent(
            kind=EventKind.SECTOR_MOVE,
            occurred_at=NOW,
            subject="tech",
            previous_state="a",
            current_state="b",
            materiality=Materiality.NOTABLE,
            summary="tech moved",
        )
        assert journal.append([event]) == 1
        assert len(journal.read()) == 1
        assert journal.read(since=NOW.date() + timedelta(days=1)) == []

    def test_a_damaged_journal_line_is_skipped(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "2026-08.jsonl").write_text('{"broken"\n')
        assert EventJournal(tmp_path).read() == []


class TestEventShape:
    def test_every_event_carries_the_required_fields(self) -> None:
        """The brief's contract: each field is present and populated."""
        events = detectors.detect_health(
            {"status": "READY", "rows": 10}, {"status": "DATA_STALE", "rows": 10},
            now=NOW, as_of="2026-08-14",
        )
        payload = events[0].as_dict()
        for field in (
            "occurred_at", "subject", "previous_state", "current_state",
            "materiality", "evidence", "confidence", "provenance", "scope",
            "dedup_key",
        ):
            assert payload[field] not in (None, "", []), f"{field} is empty"

    def test_a_dedup_key_is_derived_when_not_supplied(self) -> None:
        event = ChangeEvent(
            kind=EventKind.SECTOR_MOVE,
            occurred_at=NOW,
            subject="tech",
            previous_state=None,
            current_state="up",
            materiality=Materiality.NOTABLE,
            summary="",
        )
        assert event.key() == "SECTOR_MOVE:-:tech:up"

    @pytest.mark.parametrize(
        ("band", "reportable"),
        [
            (Materiality.ROUTINE, False),
            (Materiality.NOTABLE, True),
            (Materiality.SIGNIFICANT, True),
            (Materiality.CRITICAL, True),
        ],
    )
    def test_only_notable_and_above_are_reportable(
        self, band: Materiality, reportable: bool
    ) -> None:
        event = ChangeEvent(
            kind=EventKind.SECTOR_MOVE,
            occurred_at=NOW,
            subject="tech",
            previous_state="a",
            current_state="b",
            materiality=band,
            summary="",
        )
        assert event.reportable is reportable


class TestValuationHysteresis:
    """A percentile band flips on ordinary price movement; report the durable ones."""

    @staticmethod
    def _context(band: str) -> object:
        return SimpleNamespace(
            available=True,
            valuation_context=band,
            valuation_value=10.0,
            labels={},
            metrics={},
            confidence="HIGH",
        )

    def _run(self, store: InMemoryStateStore, band: str, day: str, now: datetime):
        closes, volumes = _series(300)
        return MonitoringEngine(store, now=now).run(
            MonitoringInputs(
                as_of=day,
                bars={"SPY": Bars(closes, volumes)},
                company_contexts={"AAA": self._context(band)},
            )
        )

    def test_a_single_pass_band_flip_is_not_reported(self) -> None:
        """**The gate.** One observation of a new band is not yet a change."""
        store = InMemoryStateStore()
        self._run(store, "NORMAL_VS_HISTORY", "2025-06-01", NOW)
        flip = self._run(store, "HIGH_VS_HISTORY", "2025-06-08", NOW + timedelta(days=7))
        assert flip.quiet

    def test_a_band_that_holds_is_reported(self) -> None:
        store = InMemoryStateStore()
        self._run(store, "NORMAL_VS_HISTORY", "2025-06-01", NOW)
        self._run(store, "HIGH_VS_HISTORY", "2025-06-08", NOW + timedelta(days=7))
        held = self._run(store, "HIGH_VS_HISTORY", "2025-06-15", NOW + timedelta(days=14))
        assert not held.quiet
        event = held.events[0]
        assert event.kind is EventKind.VALUATION_STATE_CHANGE
        assert event.previous_state == "NORMAL_VS_HISTORY"
        assert event.current_state == "HIGH_VS_HISTORY"

    def test_an_oscillation_reports_nothing(self) -> None:
        """**The gate.** Flip-flopping across a boundary is noise, not news."""
        store = InMemoryStateStore()
        bands = ["NORMAL_VS_HISTORY", "HIGH_VS_HISTORY", "NORMAL_VS_HISTORY",
                 "HIGH_VS_HISTORY", "NORMAL_VS_HISTORY"]
        runs = [
            self._run(store, band, f"2025-06-{1 + 7 * i:02d}", NOW + timedelta(days=7 * i))
            for i, band in enumerate(bands)
        ]
        assert all(run.quiet for run in runs)


class TestDigestCollapsesRepeats:
    def test_one_row_per_subject_at_its_most_significant(self) -> None:
        """**The gate.** Four volatile days for one stock are one weekly line."""
        def event(subject: str, band: Materiality, change: float) -> dict:
            return ChangeEvent(
                kind=EventKind.UNUSUAL_VOLATILITY,
                occurred_at=NOW,
                subject=subject,
                previous_state="a",
                current_state="b",
                materiality=band,
                summary=f"{subject} {change}",
                evidence=(Evidence("m", 0.0, change, change=change),),
                scope=Scope(ScopeKind.COMPANY),
            ).as_dict()

        events = [
            event("AMZN", Materiality.NOTABLE, 1.0),
            event("AMZN", Materiality.SIGNIFICANT, 3.0),
            event("AMZN", Materiality.NOTABLE, 2.0),
            event("MSFT", Materiality.NOTABLE, 1.5),
        ]
        section = biggest_market_changes(events)
        assert [r["subject"] for r in section.rows] == ["AMZN", "MSFT"]
        assert section.rows[0]["materiality"] == "SIGNIFICANT"

    def test_the_same_subject_in_two_accounts_stays_separate(self) -> None:
        """A weight change in one account is not the same event as in another."""
        def event(account: str) -> dict:
            return ChangeEvent(
                kind=EventKind.PORTFOLIO_WEIGHT_CHANGE,
                occurred_at=NOW,
                subject="NVDA",
                previous_state="10%",
                current_state="20%",
                materiality=Materiality.NOTABLE,
                summary=f"{account} NVDA",
                scope=Scope(ScopeKind.PORTFOLIO, account=account),
            ).as_dict()

        section = most_important_portfolio_changes(
            [event("PAPER_1K"), event("PAPER_3K")]
        )
        assert {r["account"] for r in section.rows} == {"PAPER_1K", "PAPER_3K"}
