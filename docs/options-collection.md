# Option surface collection, and the EDGAR direction result (phase 10.1)

Two independent outcomes:

- **`IV_COLLECTION_HEALTHY`** — the collector is live as the seventh scheduled job.
- **`NO_STABLE_DIRECTIONAL_INFORMATION`** — the EDGAR EPS-growth hypothesis does
  not survive a 5.5× larger sample. It inverts.

## Why option snapshots are collected at all

Every other dataset here can be reconstructed on demand. Option chains cannot:
Alpaca serves snapshots of *now*, the chain request has no historical "as of"
parameter, and even the paid OPRA feed only reaches back to February 2024. A
surface not captured today is gone.

That is the entire argument for running this before there is any evidence that
implied volatility predicts anything.

## What the free feed actually returns

Measured, not assumed — `indicative` feed, no OPRA agreement, no subscription:

| Field | Coverage |
|---|---|
| `latest_quote` (bid/ask/size) | 100% |
| `implied_volatility` | **78.5%** (10,996 of 14,001 for SPY) |
| `greeks` (δ/γ/ν/θ) | **78.5%** |
| `latest_trade` | 80.3% |
| `open_interest` | **0%** — absent on this feed |

Greeks are internally consistent: SPY ATM call/put deltas +0.544/−0.457, vega
matched to three decimals across the pair, visible term structure and skew.
NVDA ATM IV 0.298 against KO's 0.176.

The indicative feed is Alpaca's *derived approximation* of quotes, not the
consolidated OPRA best bid/offer, so every stored row records `feed`. Mixing the
two silently would put an unexplained step change in the middle of a series.

## Cadence

**One capture per regular session, in a 19:30–21:00 UTC window** (15:30–17:00 ET).

Late enough that the session's information is in the surface; early enough to
avoid the closing auction, where quotes widen and an indicative feed is least
representative. The window is 90 minutes wide rather than an instant so a slow
provider, a late run or a machine that woke at 20:15 still captures the day.

The job runs every 20 minutes and guards itself on three things: the session is
REGULAR, the moment is inside the window, and today is not already captured.
**Idempotency is keyed on the trading date**, not the timestamp — which is what
makes a wide window safe and a retry convergent.

## The canonical slice

Storing every contract would be operationally absurd. Measured across the
52-stock universe:

| Representation | Contracts/symbol/day | 52 sym / 1y | 52 / 5y | 200 / 5y |
|---|---|---|---|---|
| Full chain | 2,598 | 6.8 GB | 34 GB | **131 GB** |
| **Canonical** (±10% moneyness, ≤60 DTE, IV present) | **283** | **742 MB** | 3.7 GB | 14 GB |
| Derived summary only | 1 row | 3.3 MB | 16 MB | 63 MB |

Both grains are kept: the derived summary (ATM IV, 30-day IV, 25-delta skew,
term slope, expected move) so a future phase has a compact series, and the
canonical slice so it can recompute skew a different way instead of inheriting
today's definition.

Index ETFs carry ~3,200 canonical contracts each — roughly 11× a single stock —
so adding benchmarks to collection would triple storage.

### Nothing is fabricated

Every derived field is `None` when its inputs are absent:

- 30-day IV interpolates **only when 30 days is bracketed** by two expiries,
  never extrapolated from one side;
- skew requires **both** wings;
- a one-sided market has **no mid** — substituting the live side would create a
  price that never existed;
- an implied volatility outside 0.1%–500% is **rejected, not clamped**. Clamping
  would launder a broken quote into a plausible number.

Eight defect classes are counted on every capture (missing IV, missing greeks,
missing quote, one-sided, impossible IV, duplicates, bad strike/expiry) and
recorded rather than repaired.

## First run

Market was shut, so **no snapshot was fabricated**. A dry run proved the pipeline:

| | |
|---|---|
| Symbols | 52/52 |
| Contracts scanned | 135,070 |
| Canonical kept | 14,715 |
| IV missing | 36.6% |
| One-sided / impossible / duplicate / bad strike | 0 / 0 / 0 / 0 |
| Runtime | 78.4s |
| Written | **nothing** |

The staleness guard applies to the persisting path only. A dry run writes
nothing, so refusing there would make the pipeline unverifiable outside market
hours — which is exactly when an operator checks it.

```bash
tradabot options capture --dry-run   # fetch and derive, write nothing
tradabot options capture             # the scheduled path; guards itself
```

## The provider floor is 2020-07-27, and it is a wall

Phase 10 recommended extending price history to ~2016 to enlarge the EDGAR
sample. **That is not possible on this account.** Probed directly:

| Year probed | D1 bars returned | H1 bars |
|---|---|---|
| 2015–2019 | **0** | **0** |
| 2020 Jan–Jun | **0** | **0** |
| 2020 Jul onwards | present | present |
| 2021, 2022, 2024 | present | present |

First available bar, measured: **AAPL D1 2020-07-27, H1 2020-07-27 13:00**.

Our stored history starts on exactly that date because it *is* the provider
floor on the free IEX feed — not a project choice, as phase 10 assumed. The
advertised "7+ years" refers to the SIP feed on Algo Trader Plus ($99/mo).

**No backfill was performed. Zero candles added.**

## EDGAR: the sample grew 5.5×, and the hypothesis inverted

The real constraint was not only price history — phase 10's POC used 10 symbols
when 52 were available. Across the universe EDGAR holds **2,756 events**
(2009–2026, ~150–180/year), of which **1,036** fall inside the reachable price
window and **916** join causally.

Same frozen hypothesis, unchanged thresholds: as-first-reported diluted EPS YoY
growth > +20% versus < −20%, entering at the first bar strictly after SEC
acceptance.

| Horizon | STRONG_GROWTH | MID | DECLINE | Spread (S−D) | Phase 10 (n=167) |
|---|---|---|---|---|---|
| 1d | 53.2% | 55.4% | 53.3% | **−0.1pp** | +11.1pp |
| 5d | 59.4% | 59.4% | 55.0% | **+4.4pp** | +8.1pp |
| 10d | 55.8% | 62.8% | 60.3% | **−4.5pp** | +9.3pp |
| 20d | 55.5% | 60.7% | 61.6% | **−6.0pp** | +0.1pp |

The sign **inverts** at 10d and 20d. Year stability reverses at every horizon
across all six years. The phase-10 result was noise, exactly as its
`INSUFFICIENT` label said.

### The matched baseline finds something else

Each event compared against its own symbol's non-event bars in the same calendar
year — matching removes both universe composition and bull-market drift:

| Horizon | STRONG_GROWTH | MID | DECLINE |
|---|---|---|---|
| 5d | +4.8pp | +6.4pp | +3.0pp |
| 10d | −0.3pp | +9.0pp | +8.0pp |
| 20d | −1.4pp | +6.9pp | **+8.8pp** |

**All three buckets lift at 5d**, and DECLINE beats STRONG_GROWTH at 10d and
20d. So the *filing event* carries something, and EPS growth direction does not
order it. That is an event effect, not a directional one — and it is the
opposite of the hypothesis.

### Survivorship / CIK continuity

Three of 52 symbols have discontinuities: **BRK.B** and **V** return zero
quarterly `EarningsPerShareDiluted` observations under their current CIK, and
**XOM** returns one (CIK 2115436, a post-reorganisation identifier). These are
tagging and corporate-structure artefacts, not missing filings, and they bias the
sample toward companies with continuous XBRL history.

## Status

- **Options collection: `IV_COLLECTION_HEALTHY`.** Seventh job loaded, 0 days
  accumulated, first capture due in the next session's window. At ~250 sessions
  per year, a minimally testable IV series is roughly 6–12 months away.
- **EDGAR EPS growth: `NO_STABLE_DIRECTIONAL_INFORMATION`.** Not
  `REGIME_DEPENDENT` — a regime-dependent effect keeps a sign within a regime,
  and this one inverts between horizons on the same rows.
- Volatility interaction was **not tested**: it is gated on stable directional
  information, and that gate was not met.
