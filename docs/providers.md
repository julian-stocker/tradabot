# Providers and instrument identity

## Today: one provider, US equities only

| | |
|---|---|
| Provider | Alpaca |
| Feed | IEX |
| Coverage | US equities |
| History | **rolling ~6 years** (2020-07-27 today), advancing daily |
| Currency | USD |
| Exchanges | XNYS (32), XNAS (20) |

## Instrument identity

Provider-neutral by design. A MIC and a company name mean the same thing for a
Xetra listing as for a NYSE one, so a future European or Japanese provider fills
the same fields without a schema change.

| Field | Populated | Source |
|---|---|---|
| `symbol` | 52/52 | watchlist |
| `name` | **52/52** | Alpaca asset catalogue |
| `exchange` (MIC) | **52/52** | Alpaca asset catalogue |
| `currency` | 52/52 | USD (single-market assumption) |
| `provider` | 52/52 | `alpaca` |
| `provider_symbol` | 52/52 | Alpaca ticker |
| `asset_type` | 52/52 | STOCK |
| `isin` | **0/52** | not exposed by Alpaca |
| `listed_at` / `delisted_at` | **0/52** | not exposed by Alpaca |

### The XNAS defect (fixed)

Every instrument was seeded `exchange = <configured default>` and `name = symbol`,
because `get_instruments()` fabricates metadata — the asset catalogue lives behind
the trading API, which the market-data path deliberately does not use.

The result: **32 of 52 instruments claimed to be on XNAS while listed on NYSE**,
and no company name existed anywhere. `exchange` feeds calendar selection, which
was harmless only because both US venues share sessions — and stops being harmless
the moment a non-US listing exists.

Fixed by `tradabot scanner refresh-identity`, which reads Alpaca's asset endpoint
and writes only what it reports:

```
52/52 resolved; 52 names, 32 exchanges updated
  XNAS->XNYS: 32 -- ABBV, BA, BAC, BRK.B, CAT, COP, CRM, CVX
```

**Read-only.** It calls `GET /v2/assets/{symbol}` and nothing else; no order type
is imported and none is reachable. tradabot still submits no orders anywhere.
Seeding remains credential-light — enrichment is a separate, opt-in step.

`listed_at`, `delisted_at` and ISIN are deliberately **left null**: Alpaca does
not carry them, and a guessed listing date is worse than a missing one. This is
why backtests are still not survivorship-bias-free.

## Why international listings cannot be monitored

Kioxia (285A.T), SÜSS MicroTec (SMHN.DE) and any other non-US listing are blocked
by **provider coverage**, not by the schema:

1. **Alpaca serves US equities.** A request for `285A.T` returns empty — no error.
   The backfill would record that as a gap and retry it forever.
2. **Calendars are selected from `exchange`.** Now that MICs are correct this is
   one step from working, but `get_trading_calendar` is still passed a US venue
   for every instrument because every instrument *is* one.
3. **Currency is assumed USD.** Cost models, portfolio equity and P/L are all
   single-currency; a EUR or JPY position needs an FX boundary that does not exist.

### What phase 5.7+ would need

| Requirement | Why |
|---|---|
| A provider covering XETR / XTKS | Alpaca does not |
| ISIN-based identity | the only stable cross-venue key; ticker collides (`SMHN.DE` vs `SMHN`) |
| Per-instrument calendars | Xetra and Tokyo have different sessions, holidays and half-days |
| FX rates and a base currency | equity and P/L across currencies |
| Per-venue cost models | fee structures differ by an order of magnitude |
| Per-venue session policy | the IEX extended-hours rule is US-specific |

Candidate providers were **not** evaluated in this phase, and no integration was
started.

## Design constraints already satisfied

- `AssetMetadata` and the `AssetCatalogue` protocol are provider-neutral —
  nothing in them names Alpaca.
- `exchange` stores an **ISO 10383 MIC**, not a vendor string, so `XETR`/`XTKS`
  slot in directly.
- `_MIC_BY_ALPACA_EXCHANGE` maps a vendor's spelling to the MIC at the boundary,
  which is where vendor-specific vocabulary belongs.
- Unrecognised exchanges map to `None` rather than a default — defaulting is
  exactly how everything came to claim XNAS.
