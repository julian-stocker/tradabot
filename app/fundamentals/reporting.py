"""Deriving a company's reporting currency and taxonomy from its own filings.

Why this is not seeded
----------------------
It was, and it was wrong. The Xetra listing of SAP SE resolves -- correctly --
to the company row the SEC knows as CIK 0001000184, which is the *same company*
as the New York ADR. That row had been created when the US universe was seeded
and carried the defaults that made sense for it then: ``USD`` and ``us-gaap``.

So ``/check SAP.DE`` printed euro revenue with a dollar sign. Every figure was
right and the sentence was wrong, which is the harder error to notice.

A seed cannot fix this, because the fact is not a property of the ticker or the
venue: Canadian National Railway files **us-gaap in CAD**, SAP files
**ifrs-full in EUR**, Shopify files **us-gaap in USD** from Ottawa. Taxonomy and
currency vary independently, and neither follows from country.

The filings already say it
--------------------------
Every fact carries the taxonomy that published it and the unit it was reported
in. That is the issuer's own answer, it arrives with the data, and it corrects
itself on the next sync. Nothing here guesses from a country code.

Recency matters
---------------
A company that changes presentation currency -- as several have on
redomiciliation -- would have both units in its history. The dominant unit of
the *recent* window wins, because what the Advisor renders is recent, and a
decade-old presentation currency describing today's revenue is the same defect
in slower motion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.core.logging import get_logger

logger = get_logger(__name__)

RECENT_YEARS: int = 3
"""How far back the currency vote looks. Long enough that a company filing
annually is represented, short enough that a superseded presentation currency
cannot outvote the current one."""

CURRENCY_MAJORITY: float = 0.75
"""How much of the recent monetary evidence must agree before the registry is
overwritten. A EUR filer tags the odd USD fact, so unanimity is the wrong bar;
a genuinely mixed history is left alone rather than resolved by a coin toss."""

_YEAR_DIGITS: int = 4
"""An ISO date whose year is unreadable cannot anchor a recency window."""

_ISO_4217_LENGTH: int = 3
"""Monetary units are three-letter codes. ``shares``, ``pure`` and ``USD/shares``
are units too, and none of them is a reporting currency."""


@dataclass(frozen=True, slots=True)
class Reported:
    """What one company's filings say about how it reports."""

    cik: int
    currency: str | None
    taxonomy: str | None
    facts: int
    currency_share: float
    """Fraction of recent monetary facts in the winning currency. Below 1.0 is
    ordinary -- a EUR filer still tags a handful of USD facts -- but a low share
    means the answer is genuinely mixed and worth seeing."""

    @property
    def confident(self) -> bool:
        return self.currency is not None and self.currency_share >= CURRENCY_MAJORITY


def observe(facts: pl.DataFrame) -> dict[int, Reported]:
    """Read reporting currency and taxonomy out of a fact frame, per CIK.

    Args:
        facts: the persisted fact store, or any subset of it.

    Returns:
        One entry per CIK present. A company whose facts are all non-monetary
        yields ``currency=None`` rather than a default.
    """
    if facts.height == 0:
        return {}

    # `dei` is cover-page metadata filed by us-gaap and ifrs filers alike, so it
    # cannot vote on taxonomy without making every IFRS filer look mixed.
    accounting = facts.filter(pl.col("taxonomy") != "dei")
    if accounting.height == 0:
        return {}

    cutoff = _cutoff(accounting)
    monetary = accounting.filter(
        (pl.col("unit").str.len_chars() == _ISO_4217_LENGTH) & (pl.col("period_end") >= cutoff)
    )

    taxonomies = _dominant(accounting, "taxonomy")
    currencies = _dominant(monetary, "unit")
    totals = {
        int(row["cik"]): int(row["len"])
        for row in accounting.group_by("cik").len().iter_rows(named=True)
    }

    out: dict[int, Reported] = {}
    for cik, total in totals.items():
        currency, share = currencies.get(int(cik), (None, 0.0))
        taxonomy, _ = taxonomies.get(int(cik), (None, 0.0))
        out[int(cik)] = Reported(
            cik=int(cik),
            currency=currency,
            taxonomy=taxonomy,
            facts=int(total),
            currency_share=share,
        )
    return out


def _cutoff(frame: pl.DataFrame) -> str:
    """The start of the recency window, as an ISO date string.

    Derived from the data rather than from the clock: a store rebuilt from a
    cache months old must produce the same answer it did when it was written.
    """
    latest = frame["period_end"].max()
    if not isinstance(latest, str) or len(latest) < _YEAR_DIGITS:
        return "0000-01-01"
    return f"{int(latest[:_YEAR_DIGITS]) - RECENT_YEARS:04d}{latest[_YEAR_DIGITS:]}"


def _dominant(frame: pl.DataFrame, column: str) -> dict[int, tuple[str, float]]:
    """Per CIK, the most common value in ``column`` and its share."""
    if frame.height == 0:
        return {}
    counts = frame.group_by(["cik", column]).len()
    totals = counts.group_by("cik").agg(pl.col("len").sum().alias("total"))
    ranked = (
        counts.join(totals, on="cik")
        # Ties broken by value, not by row order: the same store must give the
        # same answer on every machine and every polars version.
        .sort(["cik", "len", column], descending=[False, True, False])
        .group_by("cik", maintain_order=True)
        .first()
    )
    return {
        int(row["cik"]): (str(row[column]), float(row["len"]) / float(row["total"]))
        for row in ranked.iter_rows(named=True)
    }


def apply(
    database: str,
    *,
    store: Path = Path("data/sec_facts.parquet"),
    dry_run: bool = False,
) -> list[tuple[str, str, str]]:
    """Correct ``companies.reporting_currency`` and ``.taxonomy`` from the store.

    Args:
        database: path to the SQLite database.
        store: the persisted fact store to read the truth from.
        dry_run: compute and report the changes without writing them.

    Returns:
        One ``(cik, field, "old -> new")`` tuple per correction. Empty when the
        registry already agrees with the filings.
    """
    import sqlite3  # noqa: PLC0415

    if not store.exists():
        logger.warning("no fact store; reporting metadata left unchanged", store=str(store))
        return []

    observed = observe(pl.read_parquet(store, columns=["cik", "taxonomy", "unit", "period_end"]))
    changes: list[tuple[str, str, str]] = []

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, cik, reporting_currency, taxonomy FROM companies WHERE cik IS NOT NULL"
        ).fetchall()
        for company_id, cik, currency, taxonomy in rows:
            found = observed.get(int(cik))
            if found is None:
                continue
            # A metric the filings do not establish leaves the stored value
            # alone. Absence of evidence is not evidence of USD.
            if found.currency and found.currency != currency and found.confident:
                changes.append((cik, "reporting_currency", f"{currency} -> {found.currency}"))
                if not dry_run:
                    connection.execute(
                        "UPDATE companies SET reporting_currency = ? WHERE id = ?",
                        (found.currency, company_id),
                    )
            if found.taxonomy and found.taxonomy != taxonomy:
                changes.append((cik, "taxonomy", f"{taxonomy} -> {found.taxonomy}"))
                if not dry_run:
                    connection.execute(
                        "UPDATE companies SET taxonomy = ? WHERE id = ?",
                        (found.taxonomy, company_id),
                    )
        if not dry_run:
            connection.commit()
    finally:
        connection.close()

    logger.info(
        "reporting metadata reconciled",
        companies=len(observed),
        changes=len(changes),
        dry_run=dry_run,
    )
    return changes


def backfill_sic(
    database: str,
    *,
    client: object | None = None,
    only_missing: bool = True,
) -> tuple[int, int]:
    """Record each company's SIC classification from EDGAR submissions.

    Args:
        database: path to the SQLite database.
        client: an :class:`~app.fundamentals.client.EdgarClient`. Constructed
            if omitted.
        only_missing: skip companies that already carry a code, so a rerun
            costs one request per genuinely new company rather than a thousand.

    Returns:
        ``(updated, failed)``. A company whose profile cannot be read keeps a
        null code and is classified ``unknown``, which the Advisor renders as
        not knowing rather than as an ordinary industrial company.
    """
    import sqlite3  # noqa: PLC0415

    from app.fundamentals.client import EdgarClient, EdgarUnavailableError  # noqa: PLC0415

    reader = client if client is not None else EdgarClient()
    connection = sqlite3.connect(database)
    updated = failed = 0
    try:
        query = "SELECT id, cik FROM companies WHERE cik IS NOT NULL"
        if only_missing:
            query += " AND sic IS NULL"
        for company_id, cik in connection.execute(query).fetchall():
            try:
                profile = reader.profile(int(cik))  # type: ignore[attr-defined]
            except EdgarUnavailableError as exc:
                logger.warning("sic unavailable", cik=cik, reason=str(exc))
                failed += 1
                continue
            code = (profile.get("sic") or "").strip()
            if not code:
                failed += 1
                continue
            connection.execute(
                "UPDATE companies SET sic = ?, sic_description = ? WHERE id = ?",
                (code[:4], (profile.get("sic_description") or "")[:128], company_id),
            )
            updated += 1
        connection.commit()
    finally:
        connection.close()
    logger.info("sic backfill complete", updated=updated, failed=failed)
    return updated, failed


__all__ = [
    "CURRENCY_MAJORITY",
    "RECENT_YEARS",
    "Reported",
    "apply",
    "backfill_sic",
    "observe",
]
