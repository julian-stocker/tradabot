# Real market and sector context (phase 9A)

**Final classification: `NO_ADDITIONAL_INFORMATION`.**

Real ETF context does not add stable directional information beyond the
equal-weight proxy it replaces. Signal-v2 is not started. The one finding that
*did* survive is descriptive, not directional: volatility is sector-structured.

## What this phase was for

Phase 6 measured market context against a reference built out of the universe it
was measuring — an equal-weight mean of the same 52 watchlist symbols, and a
"sector" that was the first watchlist tag. That family produced the largest
separation in the entire study (5.5pp, `REGIME_DEPENDENT`) and also the least
interpretable one, because a stock is *definitionally* correlated with the
average of 51 others taken at the same instant.

## Part B — Alpaca availability

All fifteen candidates are available, tradable and complete through the existing
account. **No second provider is needed.**

| Symbol | Role | Exchange | D1 bars | H1 bars | From | Gaps |
|---|---|---|---|---|---|---|
| SPY | market | ARCX | 1,519 | 12,718 | 2020-07-27 | none |
| QQQ | market | XNAS | 1,519 | 13,005 | 2020-07-27 | none |
| XLK / XLF / XLE / XLV / XLI / XLY / XLP / XLC | sector | ARCX | 1,519 | ~10,600–10,900 | 2020-07-27 | none |
| XLU / XLRE / XLB | sector | ARCX | 1,519 | ~10,620 | 2020-07-27 | none |
| SMH | semis | XNAS | 1,519 | 10,893 | 2020-07-27 | none |
| SOXX | semis | XNAS | 1,519 | 10,904 | 2020-07-27 | none |

Every symbol reaches the provider floor of 2020-07-27, matching the stock
universe exactly. Alpaca's asset API does not report a currency field; all
fifteen are USD-listed.

**XLU, XLRE and XLB are excluded.** They are available and complete — no stock in
the 52-symbol universe maps to them. Registering an unmapped sector fund adds a
series nothing joins to.

### SMH vs SOXX

| | D1 bars | H1 bars | Sessions missing vs SPY | Thin H1 sessions (<6 bars) | Splits |
|---|---|---|---|---|---|
| **SMH** | 1,519 | 10,893 | 0 | **13** | 2023-05-05 (2:1) |
| SOXX | 1,519 | 10,904 | 0 | **45** | 2024-03-07 (3:1) |

Equivalent on session coverage; **SMH wins on intraday continuity** — 13 thin
hourly sessions against SOXX's 45. Thin sessions are what turn into NULL context
joins, so SMH is canonical. SOXX is retained as a registered `ALTERNATE` so the
choice can be revisited against stored data rather than re-downloaded.

## Part C — context-universe architecture

One source of truth: `app/market_data/benchmarks.py`. `BENCHMARKS` is the
CONTEXT_UNIVERSE; the TRADE_UNIVERSE is the enabled watchlist. They are disjoint
by construction, not by care.

| Requirement | How it holds |
|---|---|
| Scanner emits no signals for ETFs | Scan set is `WatchlistEntry.enabled.is_(True)`; ETFs are never watchlisted |
| Paper portfolios cannot trade them | Paper trades scanner decisions, which come from the same enabled watchlist |
| Opportunity counts exclude them | `WatchlistRepository.count(enabled_only=True)` is unchanged — verified by test |
| Research can use them | `is_benchmark()` splits the featured frame into universe and reference |
| Volatility engine may inspect them | `VolatilityService.for_symbols` takes any stored symbol; no change to volatility-v1 |
| #market-trends may display them | `app/notifications/market_context.py`, built and tested, **not enabled** |
| Reuses the existing pipeline | Same `history` / `market-data sync` / `MarketDataImportService` |

`WatchlistRepository.add` enables unconditionally, so watchlisting SPY is the one
way an ETF could leak. `watchlisted_benchmarks()` detects it, the CLI exits
non-zero on it, and an integration test forces the leak to prove it is caught.

```bash
tradabot market-data benchmarks             # coverage + the leak check
tradabot market-data benchmarks --register  # writes `instruments` only
```

**No schema migration.** Context instruments are ordinary `instruments` rows with
`asset_type = ETF`; `alembic check` reports no new operations, head stays 0011.

## Part D — sector mapping

| Watchlist tag | Benchmark | Parent | Stocks |
|---|---|---|---|
| technology | XLK | — | AAPL MSFT GOOGL ORCL CRM ADBE |
| semiconductors | **SMH** | **XLK** | NVDA AMD INTC AVGO QCOM TXN MU |
| communication | XLC | — | META NFLX DIS T VZ |
| consumer-discretionary | XLY | — | AMZN TSLA HD MCD NKE SBUX |
| financials | XLF | — | JPM BAC GS MS V MA BRK.B |
| healthcare | XLV | — | UNH JNJ LLY PFE ABBV MRK |
| industrials | XLI | — | CAT BA HON GE UPS LMT |
| energy | XLE | — | XOM CVX COP SLB |
| consumer-staples | XLP | — | PG KO PEP WMT COST |

Mapping is keyed on the **stored watchlist tags**, not GICS. The tags file GOOGL
under `technology` rather than communication services; mapping to a textbook
taxonomy would produce sector returns that do not describe the groups the
research code actually forms. An unrecognised tag raises rather than silently
returning no benchmark.

Semiconductors are hierarchical, as the brief asks: SMH is the sector reference
and declares XLK as `parent`, so a chipmaker carries both. Every other sector
returns `None` for parent — "no parent" and "parent is the whole market" are
different statements.

## Part E — backfill

679,825 bars across 12 instruments, **193 MB** of database growth (3.316 → 3.505
GiB). Zero failures, zero rejected bars, zero gaps reported by the importer.

| Timeframe | From | Bars |
|---|---|---|
| 1d | 2020-07-27 | 18,240 |
| 1h | 2020-07-27 | 133,064 |
| 15m | 2024-08-01 | 164,608 |
| 5m | 2025-02-03 | 363,913 |

Windows mirror the measured stock coverage exactly. The 15m and 5m history is
*real provider data over the windows the stocks already have* — nothing is
fabricated, and no bar was requested outside a window an observation could
reference.

**Freshness is manual.** `scanner sync` iterates the enabled watchlist, so the
six scheduled jobs do not touch these:

```bash
tradabot market-data sync SPY,QQQ,XLK,SMH,XLC,XLY,XLP,XLF,XLV,XLI,XLE,SOXX
```

## The split defect this phase uncovered

Bars are stored **RAW** on purpose — the provider is asked for unadjusted prices
because tradabot adjusts on read. But the research loader read `candles`
directly and never applied the adjustment, and the corporate-action table held
two dividends and **no splits at all**: the two paths that built this database
(`history`, `scanner sync`) never fetched them, and the provider call that would
have was unbounded, so Alpaca's roughly-current-month default returned one
action across 62 instruments and looked like a successful sync.

Sixteen splits are now stored. Effect on the hourly research series:

| Feature | Raw range | Adjusted range |
|---|---|---|
| `ret_1d_pct` | −95.1 … +686.5 | −37.2 … +28.9 |
| `ret_5d_pct` | −95.3 … +709.1 | −42.5 … +39.7 |
| `rel_strength_market_pct` | −94.8 … +672.9 | −36.7 … +28.2 |
| `atr_pct` | 0.19 … **132.2** | 0.19 … **5.5** |

The +686% is GE's 1-for-8 reverse split; the −95% is AMZN's and GOOGL's 20-for-1.
Three of the sector funds split too — XLE, XLK and XLY all 2-for-1 on 2025-12-05,
and SMH 2-for-1 on 2023-05-05 — inside the windows they exist to provide context
for.

### Splits are applied only where the prices corroborate them

A stored action the prices do not show is not harmless: adjusting for it
**creates** a discontinuity. Two real cases —

- **HON 2026-06-29**, reported as a 1-for-2 reverse split, observed ratio 1.02.
  Nothing happened to the price. Skipped on every timeframe.
- **NVDA 2024-06-10**, a genuine 10-for-1, but NVDA's *daily* series is missing
  2024-01 through 2025-06, so on 1d the adjacent bars straddle an 18-month hole
  and show 3.10. On 1h it shows 10.00 and is applied.

The test is scale-free: the declared ratio must explain the observed jump better
than "no split" does, and land within a factor of 1.35. A percentage threshold
would have to be loose enough for TSLA's 5-for-1 on a 12%-move day, which is
loose enough to admit HON.

### Is back-adjustment causal?

Rescaling past prices when a *future* split occurs looks like leakage. It is not,
because every feature here is scale-invariant: returns, percentage distances from
moving averages, ATR as a percentage of price, volume against its own rolling
mean. Multiplying a trailing window by a constant leaves all of them unchanged;
the factor only varies *across* a split boundary, where it removes a
discontinuity that was never a price move.

`test_a_future_split_does_not_move_any_earlier_feature` proves this rather than
asserting it, and the polars implementation is pinned to the `Decimal` one in
`app/corporate_actions/adjust.py` by a randomised property test.

## Part F — context features

All causal, all joined on the bar the observation actually saw.

`index_ret_1d_pct` `index_ret_5d_pct` `index_px_vs_ema50_pct` `nasdaq_ret_1d_pct`
`nasdaq_ret_5d_pct` `sector_etf_ret_1d_pct` `sector_etf_ret_5d_pct`
`parent_etf_ret_1d_pct` `relative_strength_market_1d` `relative_strength_market_5d`
`relative_strength_nasdaq_1d` `relative_strength_sector_1d`
`relative_strength_sector_5d` `sector_relative_to_market` `market_trend_state`
`sector_trend_state` `index_above_ema50` `sector_above_ema50`

Additive, never replacing: proxy columns are untouched, so one row carries both
readings and the comparison in part B is possible at all. Reference instruments
are held out of the proxy cross-section — an equal-weight "market" containing SPY
would let the benchmark vote on itself.

## Part G — frozen hypotheses

Fixed before any outcome was inspected. "Stock strong" = top quintile of
`relative_strength_market_1d` **within each timestamp**; "sector strong" = sector
fund's 1-day return above SPY's; "market strong" = SPY above its own EMA50.

Production-faithful, 1d:

| State | Episodes | Positive | CI |
|---|---|---|---|
| stock_strong / sector_strong / market_strong | 3,613 | 52.7% | [51.1, 54.3] |
| stock_strong / sector_strong / market_weak | 2,321 | 51.7% | [49.7, 53.7] |
| stock_strong / sector_weak / market_strong | 1,729 | 51.5% | [49.1, 53.7] |
| stock_strong / sector_weak / market_weak | 1,200 | 51.0% | [48.2, 53.8] |

The ordering is monotone in the intuitive direction and the total spread is
**1.7pp**, against a 5pp floor, with every interval overlapping every other.

Relative-strength rank buckets, same stream and horizon: top 10% 53.0%, top 20%
53.0%, middle 60% 52.2%, bottom 20% 50.7% — a 2.3pp spread. Concentrating from
the top 20% to the top 10% adds exactly nothing.

## Part H — horizons

1d, 3d, 5d, 20d × two streams × 14 features = **112 analyses. 111 are
`NO_INFORMATION`.**

The single exception is `parent_etf_ret_1d_pct` (XLK behind the chipmakers) at
production 3d: +7.5pp, `PROMISING_BUT_UNSTABLE`, on 4,155 episodes. Across its
eight cells it reads −2.2, **+7.5**, +1.9, +3.2, −1.0, +1.3, −0.4, −1.0 — the sign
flips between adjacent horizons on the same stream, on the smallest subsample in
the study. That is one cell of noise, not a finding.

**`10d` was not evaluated.** It is not a member of the `Horizon` enum, so no
outcome labels exist for it; producing them would need an enum addition and
regeneration of ~427k outcome rows. Flagged rather than silently substituted.

## Part I — walk-forward stability (mandatory)

Chronological, never shuffled. Spreads in positive-rate points, by year:

**Coarse historical, 1d**

| Feature | 2020 | 2021 | 2022 | 2023 | 2024 | Verdict |
|---|---|---|---|---|---|---|
| `relative_strength_market_1d` | −1.0 | −1.9 | −0.3 | +0.5 | +3.8 | unstable |
| `relative_strength_sector_1d` | −0.0 | −0.3 | −1.3 | +0.7 | +1.2 | unstable |
| `sector_relative_to_market` | −0.2 | −1.4 | +0.6 | +0.1 | +0.5 | unstable |
| `rel_strength_market_pct` (proxy) | +0.2 | −0.8 | −1.2 | +0.9 | +1.9 | unstable |
| `proxy_breadth_stacked` | −3.3 | **−9.8** | **+4.6** | +3.1 | +0.6 | unstable |

**Production-faithful, 1d**: every feature unstable; `proxy_breadth_stacked`
2025 −7.6 → 2026 −1.9.

Every feature reverses sign across years. `proxy_breadth_stacked` — the source of
phase 6's headline 5.5pp — swings from −9.8 to +4.6.

## Part J — redundancy

The decisive result. Production-faithful, |r| ≥ 0.80:

| r | Pair |
|---|---|
| **+0.978** | `rel_strength_market_pct` (proxy) ↔ `relative_strength_market_1d` (real) |
| **+0.974** | `relative_strength_market_1d` ↔ `relative_strength_nasdaq_1d` |
| +0.938 | `index_ret_1d_pct` ↔ `nasdaq_ret_1d_pct` |
| +0.918 | `ret_5d_pct` ↔ `relative_strength_market_5d` |
| +0.912 | `rel_strength_sector_pct` ↔ `relative_strength_sector_1d` |
| **+0.906** | `ret_1d_pct` ↔ `relative_strength_market_1d` |
| +0.893 | `proxy_ret_1d_pct` ↔ `index_ret_1d_pct` |

Three readings, and they compound:

1. The real feature **is** the proxy feature, at r = 0.978. Replacing one with the
   other changes almost nothing by construction.
2. QQQ is redundant with SPY for this purpose (r = 0.974 on relative strength).
   Both are kept because the redundancy is itself the finding.
3. Relative strength is 0.906-correlated with the stock's own 1-day return —
   it is largely the stock restating itself, exactly as phase 6 found at +0.950.

## Part B result — proxy vs real, head to head

**32 pairings across both streams and all four horizons. Zero verdict
disagreements.** Correlations 0.86–0.98. The proxy was a fair stand-in; it was
measuring something that carries no outcome information.

## Part K — volatility context (descriptive; volatility-v1 unchanged)

ATR% correlation over 13,560 stored hourly bars:

| | Mean correlation |
|---|---|
| Stock ↔ **its own sector ETF** | **0.754** |
| Stock ↔ SPY | 0.662 |

Sector is the closer reference for **33 of 52** stocks; the market for **2**;
17 tied. Volatility is more sector-driven than market-driven, and both dominate
stock-specific behaviour.

Strongest sector clustering is energy — XOM 0.951, COP 0.952, CVX 0.940 against
XLE, versus ~0.70 against SPY. The most stock-specific name is INTC (0.513 vs
SMH, 0.259 vs SPY).

Benchmark ATR% cross-correlation shows SPY↔QQQ at 0.96 — near-redundant for
volatility too — while SMH sits at 0.69 against SPY and XLE at 0.75. The
semiconductor and energy complexes have volatility regimes of their own.

A same-instant regime snapshot is *not* a useful decomposition here: the market
is currently uniformly LOW_VOL (all 12 references, percentiles 0.00–0.07), so
88% regime agreement is trivially true. The historical correlation above is the
answer; the snapshot is not.

## Part L — #market-trends readiness

`app/notifications/market_context.py` renders a compact block appended to the
existing trends payload — one section, three statements of fact: what the market
reference did, what the sector reference did, and the arithmetic difference.

**Built and tested; deliberately not enabled.** Directional context classified
`NO_ADDITIONAL_INFORMATION`, so nothing here may imply an action. Enabling it
changes what the six production jobs emit, which is a decision to take
explicitly rather than as a side effect of a research phase.

The recommendation-language guard runs over every rendered line, on both rising
and falling markets, and the block returns `None` rather than an empty header
when there is nothing factual to say.

## Part N — provider sufficiency

**Alpaca is sufficient. Do not add a provider.** All fifteen candidates resolved
with full history to the project floor, zero gaps, zero failures across 679,825
backfilled bars. The one provider-side defect found was on our side of the call:
the corporate-actions request was unbounded and silently returned ~a month.

## Part M — final classification

**`NO_ADDITIONAL_INFORMATION`.**

- 111 of 112 horizon × stream × feature analyses: `NO_INFORMATION`
- The single exception flips sign between adjacent horizons on 4,155 episodes
- Pre-registered 2×2: 1.7pp spread, all intervals overlapping
- Rank buckets: 2.3pp, no gain from concentrating
- Walk-forward: every feature reverses sign across years
- Redundancy: the new feature is r = 0.978 with the proxy it replaces

Signal-v2 is **not** started. No context feature is promoted to production.

## Open items this phase surfaced but did not fix

1. **`app/market_data/volatility_service.py` reads raw candles.** The production
   volatility model uses a 252-bar trailing window with no split adjustment, so
   for ~36 sessions after any future split its estimate for that symbol would be
   badly inflated. No split currently sits inside a live window, so present
   estimates are unaffected. The phase-8 calibration table in
   [volatility.md](volatility.md) *was* computed over windows containing splits.
2. **NVDA is missing 1d bars from 2024-01 to 2025-06** (1,142 rows against 1,520
   for its peers). The hourly series is complete, so research is unaffected.
3. **`10d` outcomes do not exist** and would require an enum change plus outcome
   regeneration.
