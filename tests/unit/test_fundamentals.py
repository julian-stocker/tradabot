"""The fact store must be rebuildable, and must be honest about its own state.

The failure this suite exists for already happened once: the Advisor's data was
produced by a research script into a directory that was later deleted, and the
only surviving copy was in a temporary cache. So the tests here are less about
arithmetic than about durability -- that a rebuild is repeatable, that it
resumes, and that a store which is missing, damaged or old says so rather than
answering anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from app.advisor.facts import FactStore
from app.fundamentals import FactStoreStatus, health, schema_hash, sync_facts
from app.fundamentals.concepts import CONCEPTS, FACT_COLUMNS, METRIC_BY_CONCEPT
from app.fundamentals.store import _REQUIRED
from app.fundamentals.sync import _extract, _frame

TICKERS = {"AAA": 111, "BBB": 222}

QUARTERS = [
    ("2025-01-01", "2025-03-31", 100.0, "2025-04-20", "0001-25-A"),
    ("2025-04-01", "2025-06-30", 110.0, "2025-07-20", "0001-25-B"),
    ("2025-07-01", "2025-09-30", 120.0, "2025-10-20", "0001-25-C"),
    ("2025-10-01", "2025-12-31", 130.0, "2026-01-20", "0001-26-A"),
]


def companyfacts() -> dict[str, Any]:
    """A minimal but realistic payload: four clean quarters plus junk to drop."""
    entries = [
        {
            "start": start,
            "end": end,
            "val": value,
            "filed": filed,
            "accn": accession,
            "form": "10-Q",
            "fy": 2025,
            "fp": "Q1",
        }
        for start, end, value, filed, accession in QUARTERS
    ]
    entries += [
        {"start": "2025-01-01", "end": "2025-03-31", "val": 999.0, "filed": "2025-04-20"},
        {
            "start": "2025-01-01",
            "end": "2025-03-31",
            "val": None,
            "filed": "2025-04-20",
            "accn": "0001-25-X",
        },
    ]
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": entries}},
                "NotATrackedConcept": {"units": {"USD": entries}},
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "end": "2025-12-31",
                                "val": 50.0,
                                "filed": "2026-01-20",
                                "accn": "0001-26-A",
                                "form": "10-K",
                            }
                        ]
                    }
                }
            },
        }
    }


class FakeEdgar:
    """Counts requests, so 'resumable' can be asserted rather than assumed."""

    def __init__(self) -> None:
        self.facts_calls = 0
        self.acceptance_calls = 0

    def company_tickers(self) -> dict[str, int]:
        return dict(TICKERS)

    def companyfacts(self, cik: int) -> dict[str, Any]:
        self.facts_calls += 1
        return companyfacts()

    def acceptance_times(self, cik: int) -> dict[str, str]:
        self.acceptance_calls += 1
        return {a: f"{f}T21:30:00.000Z" for _s, _e, _v, f, a in QUARTERS}


def run(tmp_path: Path, client: Any, **kw: Any) -> Any:
    return sync_facts(
        ["AAA", "BBB"],
        client=client,
        output=tmp_path / "facts.parquet",
        cache_dir=tmp_path / "cache",
        tickers=dict(TICKERS),
        **kw,
    )


class TestConceptMap:
    def test_no_concept_belongs_to_two_metrics(self) -> None:
        """A concept counted under two metrics would be double-counted in TTM."""
        seen: dict[str, str] = {}
        for metric, concepts in CONCEPTS.items():
            for concept in concepts:
                assert concept not in seen, f"{concept} in {seen.get(concept)} and {metric}"
                seen[concept] = metric
        assert seen == METRIC_BY_CONCEPT

    def test_the_persisted_schema_covers_what_the_advisor_reads(self) -> None:
        assert set(FACT_COLUMNS) >= _REQUIRED
        assert "accepted" in FACT_COLUMNS


class TestExtraction:
    def test_untracked_concepts_and_unusable_entries_are_dropped(self) -> None:
        rows = _extract(companyfacts(), {})
        assert {r["concept"] for r in rows} == {
            "Revenues",
            "EntityCommonStockSharesOutstanding",
        }
        # Four usable quarters survive; the entry with no accession and the one
        # with no value do not, because neither can be attributed or used.
        assert sum(1 for r in rows if r["metric"] == "revenue") == len(QUARTERS)

    def test_acceptance_timestamps_attach_by_accession(self) -> None:
        rows = _extract(companyfacts(), {"0001-25-A": "2025-04-20T21:30:00.000Z"})
        by_accession = {r["accession"]: r["accepted"] for r in rows}
        assert by_accession["0001-25-A"] == "2025-04-20T21:30:00.000Z"
        assert by_accession["0001-25-B"] is None

    def test_instant_facts_with_no_period_start_do_not_break_the_schema(self) -> None:
        """Inference types the null column first and then fails on a real date."""
        frame = _frame({**r, "symbol": "AAA", "cik": 111} for r in _extract(companyfacts(), {}))
        assert frame.schema["period_start"] == pl.String
        assert frame["period_start"].null_count() == 1


class TestRebuild:
    def test_a_rebuild_is_byte_identical(self, tmp_path: Path) -> None:
        """**The gate.** Same inputs, same file -- not merely the same numbers."""
        first = run(tmp_path / "a", FakeEdgar())
        second = run(tmp_path / "b", FakeEdgar())
        assert first.output.read_bytes() == second.output.read_bytes()
        assert (first.written, first.symbols) == (second.written, second.symbols)

    def test_the_second_run_resumes_from_cache(self, tmp_path: Path) -> None:
        client = FakeEdgar()
        run(tmp_path, client)
        assert (client.facts_calls, client.acceptance_calls) == (2, 2)
        again = run(tmp_path, client)
        assert (client.facts_calls, client.acceptance_calls) == (2, 2)
        assert again.from_cache == 2
        assert again.fetched == 0

    def test_force_refetches(self, tmp_path: Path) -> None:
        client = FakeEdgar()
        run(tmp_path, client)
        run(tmp_path, client, force=True)
        assert client.facts_calls == 4

    def test_an_unknown_ticker_is_reported_not_invented(self, tmp_path: Path) -> None:
        result = sync_facts(
            ["AAA", "ZZZ"],
            client=FakeEdgar(),
            output=tmp_path / "f.parquet",
            cache_dir=tmp_path / "c",
            tickers=dict(TICKERS),
        )
        assert result.unmapped == ("ZZZ",)
        assert result.symbols == 1

    def test_a_damaged_cache_entry_is_refetched_not_trusted(self, tmp_path: Path) -> None:
        client = FakeEdgar()
        run(tmp_path, client)
        (tmp_path / "cache" / "AAA.json").write_text("{ this is not json")
        result = run(tmp_path, client)
        assert result.symbols == 2
        assert result.fetched == 1

    def test_the_advisor_can_read_what_the_sync_writes(self, tmp_path: Path) -> None:
        """**The gate.** One format, end to end: rebuild then a real TTM sum."""
        result = run(tmp_path, FakeEdgar())
        store = FactStore.from_parquet(result.output)
        assert "AAA" in store.symbols
        ttm = store.ttm("AAA", "revenue", "2026-06-30")
        assert ttm.value == pytest.approx(sum(q[2] for q in QUARTERS))

    def test_point_in_time_is_preserved_through_the_rebuild(self, tmp_path: Path) -> None:
        """A quarter filed in January is not knowable the previous October."""
        store = FactStore.from_parquet(run(tmp_path, FakeEdgar()).output)
        early = store.ttm("AAA", "revenue", "2025-11-01")
        assert early.value != sum(q[2] for q in QUARTERS)


class TestHealth:
    def test_a_missing_store_is_not_synced(self, tmp_path: Path) -> None:
        state = health(tmp_path / "absent.parquet")
        assert state.status is FactStoreStatus.NOT_SYNCED
        assert not state.present
        assert not state.ok

    def test_an_unreadable_file_is_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.parquet"
        path.write_bytes(b"not a parquet file at all")
        state = health(path)
        assert state.status is FactStoreStatus.CORRUPT
        assert state.present
        assert not state.readable

    def test_a_store_missing_required_columns_is_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "thin.parquet"
        pl.DataFrame({"symbol": ["AAA"], "value": [1.0]}).write_parquet(path)
        state = health(path)
        assert state.status is FactStoreStatus.CORRUPT
        assert "metric" in state.missing_columns

    def test_an_empty_store_is_corrupt_not_ready(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.parquet"
        _frame([]).write_parquet(path)
        assert health(path).status is FactStoreStatus.CORRUPT

    def test_an_old_store_is_stale(self, tmp_path: Path) -> None:
        from datetime import date

        path = run(tmp_path, FakeEdgar()).output
        assert health(path, as_of=date(2026, 2, 1)).status is FactStoreStatus.READY
        stale = health(path, as_of=date(2027, 1, 1))
        assert stale.status is FactStoreStatus.STALE
        assert "sync" in (stale.detail or "")

    def test_a_current_store_is_ready_and_reports_acceptance(self, tmp_path: Path) -> None:
        from datetime import date

        state = health(run(tmp_path, FakeEdgar()).output, as_of=date(2026, 2, 1))
        assert state.status is FactStoreStatus.READY
        assert state.ok
        assert state.rows > 0
        assert state.symbols == 2
        assert state.newest_filed == "2026-01-20"
        assert state.newest_accepted is not None
        assert 0.0 < (state.acceptance_coverage or 0.0) <= 1.0
        assert state.schema_hash == schema_hash()

    def test_health_never_fetches(self, tmp_path: Path) -> None:
        """A slow network must not be able to make a status check hang."""
        path = run(tmp_path, FakeEdgar()).output
        source = Path("app/fundamentals/store.py").read_text()
        assert "EdgarClient" not in source
        assert "urllib" not in source
        assert health(path).rows > 0


class TestNoSecrets:
    def test_the_outcome_carries_counts_not_payloads(self, tmp_path: Path) -> None:
        result = run(tmp_path, FakeEdgar())
        rendered = json.dumps(
            {
                "requested": result.requested,
                "written": result.written,
                "per_symbol": [o.status for o in result.per_symbol],
            }
        )
        assert "api_key" not in rendered.lower()
        assert "secret" not in rendered.lower()

    def test_the_client_reads_no_credential_settings(self) -> None:
        source = Path("app/fundamentals/client.py").read_text()
        for token in ("SecretStr", "api_key", "api_secret", "get_secret_value"):
            assert token not in source, f"client references {token}"
