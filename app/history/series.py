"""Building a comparable series out of filings, and knowing when not to.

Everything here is a pure function of what the fact store returns for a given
``as_of``. No network, no clock, no cache: a trajectory asked about 2024 is
built only from rows whose ``filed`` date was on or before 2024, which is what
makes a historical question answerable rather than merely recomputable.

Why the run must be the most recent one
---------------------------------------
A company that reported cleanly from 2009 to 2016, went quiet, and resumed in
2020 has two runs. The older one is longer. Using it would produce a confident
trajectory describing a business that stopped existing in that form a decade
ago, labelled with today's date. So the run that *ends at the latest
observation* wins, however short, and if it is too short the answer is
``INSUFFICIENT_HISTORY`` rather than the wrong decade.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from app.history.schemas import Observation, SeriesBasis

QUARTER_MIN_DAYS: Final = 80
QUARTER_MAX_DAYS: Final = 100
"""What counts as consecutive quarters. The same 80/100-day window the fact
store already uses to decide a trailing-twelve-month sum is contiguous, so the
two layers cannot disagree about whether a gap exists."""

YEAR_MIN_DAYS: Final = 330
YEAR_MAX_DAYS: Final = 400

FILING_MIN_DAYS: Final = 60
FILING_MAX_DAYS: Final = 130
"""Contiguity for a series dated by *filing* rather than by period end.

A cover-page share count is stamped with the day the filing went out, and
filing dates drift: measured over 48,168 consecutive steps the median gap is 91
days but the 5th and 95th percentiles are 63 and 122. The strict period-end
window accepts only 58.6% of those steps and would have refused Coca-Cola a
share-count trajectory it reports every quarter; 60--130 days accepts 94.9%.
The question contiguity asks is whether a reporting period was missed, and for
a filing-dated series consecutive filings are the right unit."""

SPLIT_RATIO_LOW: Final = 2 / 3
SPLIT_RATIO_HIGH: Final = 3 / 2
"""Consecutive share-count ratio outside which the series is not one series.

Share counts are **not split-adjusted** in the fact store, so NVIDIA's ten-for-one
split reads as a 900% issuance and produced a trajectory of "+249.8% a year".
Measured over 33,113 consecutive steps across the universe: 97% fall within
+-15%, the 5th and 95th percentiles are 0.972 and 1.036, and only 1.86% fall
outside 2/3--3/2. The cut is set three times looser than ordinary variation, so
a real equity raise is never mistaken for a split; beyond it, the two segments
are counts of different things and the older one is dropped."""

TTM_QUARTERS: Final = 4


def _gap(earlier: str, later: str) -> int:
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def latest_run(periods: list[str], *, low: int, high: int) -> list[str]:
    """The contiguous run ending at the newest period. Never interpolated."""
    if not periods:
        return []
    ordered = sorted(periods)
    run = [ordered[-1]]
    for earlier, later in zip(reversed(ordered[:-1]), reversed(ordered), strict=False):
        if low <= _gap(earlier, later) <= high:
            run.append(earlier)
        else:
            break
    return list(reversed(run))


def ttm_series(quarters: dict[str, dict[str, Any]]) -> list[Observation]:
    """Rolling four-quarter sums over the most recent contiguous run.

    Each point is dated by the quarter it ends on and carries the *latest* of
    the four filing dates, because a trailing-twelve-month figure only becomes
    knowable when its final quarter is filed. Dating it any earlier would let a
    point appear before the information existed.
    """
    run = latest_run(list(quarters), low=QUARTER_MIN_DAYS, high=QUARTER_MAX_DAYS)
    if len(run) < TTM_QUARTERS:
        return []
    out: list[Observation] = []
    for index in range(len(run) - TTM_QUARTERS + 1):
        window = run[index : index + TTM_QUARTERS]
        rows = [quarters[p] for p in window]
        filed = max((str(r.get("filed")) for r in rows if r.get("filed")), default=None)
        out.append(
            Observation(
                period_end=window[-1],
                value=sum(float(r["value"]) for r in rows),
                filed=filed,
                concept=str(rows[-1].get("concept") or ""),
                unit=str(rows[-1].get("unit") or ""),
            )
        )
    return out


def annual_series(rows: list[dict[str, Any]]) -> list[Observation]:
    """One point per fiscal year, over the most recent contiguous run.

    The only basis available to a filer that reports annually. Measured: SAP
    has zero quarterly observations, so a trailing-twelve-month series for it
    does not exist and must not be improvised from partial-year figures.
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        end = str(row.get("period_end") or "")
        if not end:
            continue
        held = latest.get(end)
        if held is None or str(row.get("filed")) > str(held.get("filed")):
            latest[end] = row
    run = latest_run(list(latest), low=YEAR_MIN_DAYS, high=YEAR_MAX_DAYS)
    return [
        Observation(
            period_end=p,
            value=float(latest[p]["value"]),
            filed=str(latest[p].get("filed") or "") or None,
            concept=str(latest[p].get("concept") or ""),
            unit=str(latest[p].get("unit") or ""),
        )
        for p in run
    ]


def instant_series(
    points: list[tuple[str, float, Any]], *, filing_dated: bool = False
) -> tuple[list[Observation], bool]:
    """Balance-sheet or share-count observations, and whether they are annual.

    Spacing is **measured, not assumed**. Apple, Microsoft and AMD report share
    counts every 91 days; NVIDIA and JPMorgan report them once a year. Forcing a
    quarterly rule on all of them refused 204 companies for a property of the
    rule rather than of the data, so the cadence is read off the series and the
    run is cut at whichever spacing it actually uses.

    Returns the run and ``True`` when that spacing is annual, so the caller can
    convert its windows to years instead of quarters.
    """
    low = FILING_MIN_DAYS if filing_dated else QUARTER_MIN_DAYS
    high = FILING_MAX_DAYS if filing_dated else QUARTER_MAX_DAYS
    by_period = {p: (v, prov) for p, v, prov in points if v > 0}
    quarterly = latest_run(list(by_period), low=low, high=high)
    yearly = latest_run(list(by_period), low=YEAR_MIN_DAYS, high=YEAR_MAX_DAYS)
    annual = len(yearly) > len(quarterly)
    run = _after_last_split(yearly if annual else quarterly, by_period)
    out: list[Observation] = []
    for period in run:
        value, prov = by_period[period]
        out.append(
            Observation(
                period_end=period,
                value=value,
                filed=str(getattr(prov, "filed", "") or "") or None,
                concept=str(getattr(prov, "concept", "") or ""),
                unit=str(getattr(prov, "unit", "") or ""),
            )
        )
    return out, annual


def _after_last_split(run: list[str], values: dict[str, tuple[float, Any]]) -> list[str]:
    """The part of the run since the last share-count discontinuity.

    A split changes the unit, not the quantity. Everything before it counts a
    different security, so it is dropped rather than compared -- the same
    treatment a reporting-gap gets, for the same reason.
    """
    cut = 0
    for index in range(1, len(run)):
        before = values[run[index - 1]][0]
        after = values[run[index]][0]
        if before and not SPLIT_RATIO_LOW <= after / before <= SPLIT_RATIO_HIGH:
            cut = index
    return run[cut:]


def ratio_series(numerator: list[Observation], denominator: list[Observation]) -> list[Observation]:
    """A margin, on the period ends both sides actually share.

    An inner join rather than an alignment: a quarter where revenue is known
    and gross profit is not produces no margin, instead of a margin against
    whichever revenue happened to be nearest.
    """
    denom = {o.period_end: o for o in denominator if o.value}
    return [
        Observation(
            period_end=o.period_end,
            value=o.value / denom[o.period_end].value,
            filed=max(filter(None, (o.filed, denom[o.period_end].filed)), default=None),
            concept=o.concept,
            unit="ratio",
        )
        for o in numerator
        if o.period_end in denom
    ]


def difference_series(
    left: list[Observation], right: list[Observation], *, unit: str
) -> list[Observation]:
    """``left - right`` on shared period ends, for cash flow and net debt."""
    other = {o.period_end: o for o in right}
    return [
        Observation(
            period_end=o.period_end,
            value=o.value - other[o.period_end].value,
            filed=max(filter(None, (o.filed, other[o.period_end].filed)), default=None),
            concept=o.concept,
            unit=unit,
        )
        for o in left
        if o.period_end in other
    ]


def one_unit(observations: list[Observation]) -> str | None:
    """The unit every observation shares, or ``None`` if they disagree.

    A disagreement is a reporting-currency change, and the older segment is a
    different quantity rather than an earlier value -- there is no conversion
    anywhere in this system.
    """
    units = {o.unit for o in observations if o.unit}
    return next(iter(units)) if len(units) == 1 else None


def one_concept(observations: list[Observation]) -> str | None:
    concepts = {o.concept for o in observations if o.concept}
    return next(iter(concepts)) if len(concepts) == 1 else None


def basis_for(annual: bool) -> SeriesBasis:
    return SeriesBasis.ANNUAL if annual else SeriesBasis.TTM
