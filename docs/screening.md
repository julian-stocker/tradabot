# Screening

`tradabot screen` answers one question: **which covered companies satisfy these
stated conditions, as of a given date?**

It does not answer which companies to buy. There is no score, no ranking by
desirability, and no default "best first" ordering — results are alphabetical
unless you name a metric to sort by, and that is a data ordering rather than a
judgement.

```
tradabot screen \
  --where revenue_cagr_3y:gte:0.08 \
  --where fcf_margin:gte:0.15 \
  --where share_count_change_5y:lte:0
```

`tradabot screen --list-metrics` prints every screenable dimension with its
unit, cost tier and whether financial-sector companies may be tested on it.

## What a match means

A match states that a company's own filings satisfy the conditions asked for.
Nothing more. Two companies that both match are not ranked against each other,
and a company that matches is not thereby a better holding than one that does
not.

Every match is printed with the figure that produced it and the threshold it
was compared against, so the result is auditable rather than a list of tickers.

## What NOT_EVALUABLE means

A missing metric is **not** a failure. A company with no reported operating
margin has not failed `operating_margin >= 20%` — there was nothing to compare.
Those companies are counted separately, with the reason:

```
universe 989  ·  evaluated 674  ·  matched 81  ·  not evaluable 315
not evaluable because:
    177  SECTOR_MODEL_REQUIRED
    124  UNAVAILABLE
      8  INSUFFICIENT_HISTORY
      6  WINDOW_UNAVAILABLE
```

Reporting "81 of 989" without that second line would read as a 8% hit rate when
it is really 12% of what could be assessed. Both numbers are always shown.

A company that *fails* a stated condition is `NO_MATCH`, even if some other
criterion could not be tested. Saying "we could not assess this company" about
one already known to fall below a threshold is the less honest statement, and
the rule is order-independent — which matters because criteria are evaluated
cheapest-first.

Reasons are specific, never a generic "missing data":

| reason | meaning |
|---|---|
| `SECTOR_MODEL_REQUIRED` | a financial issuer, where this line item is not the quantity of the same name |
| `UNAVAILABLE` | the company never reported it |
| `INSUFFICIENT_HISTORY` | too few contiguous observations for a window |
| `WINDOW_UNAVAILABLE` | the metric exists but not over the window asked for |
| `TAXONOMY_DISCONTINUITY` | the underlying XBRL concept changed |
| `CURRENCY_CHANGE` | the reporting currency changed within the series |
| `NO_MARKET_DATA` | this listing has no price series of its own |
| `VALUATION_REFUSED` | price and filings are in different currencies |
| `NO_RESEARCH_COVERAGE` | the research store holds nothing for this company |
| `NOT_APPLICABLE` | a fund, which has no company economics |

## Point in time

`--as-of` screens the past. A screen dated 2024-06-30 uses only filings that
were public by that date: no later restatement leaks backwards, and a fiscal
period that had ended but not yet been filed does not count. Screen membership
therefore changes between dates for reasons you can verify — Analog Devices
fails `operating_margin >= 30%` as of 2022 at 20.5% and matches as of 2026 at
32.5%.

## Sector restrictions

A bank's revenue, operating margin and free cash flow are not the industrial
quantities of the same name. A naive cross-sector margin screen over this data
returns REITs and insurers at 424%, 708% and 1,185%, which is why every metric
declares whether financial-sector companies may be tested on it, and the
declaration is enforced.

**177 of 989 registrants** are financial. They are refused the industrial
metrics and keep the ones that mean the same thing for them: share count, price
to earnings, and market position. Price to sales is refused — Bank of America
yields 3.58x against a "sales" figure that is not comparable.

## Company and listing

Screening is by **company**. A cross-listed issuer appears once: SAP.DE and
SAP.US are one registrant with one set of filings.

Market and valuation criteria are **listing**-specific, and a company is
represented by the listing that has its own price series. No listing ever
borrows another's prices — a foreign line without local market data is
`NO_MARKET_DATA` for those criteria and participates normally in company-level
ones.

Valuation across a currency boundary is refused rather than converted. SAP
reports in euros and its US line trades in dollars, so its P/E is
`VALUATION_REFUSED`. Tradabot performs no FX.

## Supported metrics

Four tiers, ordered by measured cost per company across the universe, and
evaluated cheapest-first so a cheap condition narrows the field before an
expensive one runs:

| tier | cost | metrics |
|---|---|---|
| registry | free | `sic` |
| history | 4 ms | current level, 1y/3y/5y change and own-history percentile for revenue, gross margin, operating margin, FCF margin and share count |
| developments | 9 ms | `has_current_development`, `development_kind`, `development_materiality` |
| advisor | 45 ms | `pe_ttm`, `ps_ttm`, `p_fcf`, `relative_strength_252d`, `distance_from_ma200`, `drawdown_from_252d_high` |

Operators are `gte`, `lte`, `gt`, `lt` for numbers and `eq`, `neq` for
classifications. Criteria combine with AND.

**Peer percentiles are not supported.** They are safe and validated, and one
company's peer position requires an Advisor report for every member of its
group — measured at 613 ms per company, ten minutes across the universe.
Making that interactive means materialising a cross-sectional table, which is a
larger decision than a screening layer should take on its own.

## Coverage

Of 989 registrants, the share for which each metric can be evaluated:

| metric | evaluable |
|---|---|
| share count | 85% |
| revenue | 77% |
| FCF margin | 70% |
| operating margin | 69% |
| gross margin | 48% |

The remainder is 177 financial registrants plus companies that never reported
the line item or lack enough contiguous history for a window.
