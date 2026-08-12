# Market data

How real market data enters tradabot, what is checked on the way in, and what the
resulting numbers do and do not mean.

> **The distinction this phase turns on.** Real data proves **ingestion
> correctness**. It does not prove **predictive edge**. A profitable simulation on
> real prices is evidence that the plumbing works, not that the strategy does.
> Validation is phase 4.

---

## The path

```
provider  ->  normalisation  ->  validation  ->  database  ->  features  ->  signal
   |               |                  |              |
   |               |                  |              +-- provenance recorded
   |               |                  +-- rejects, gaps, duplicates reported
   |               +-- Alpaca DTOs become domain objects; Decimal, UTC
   +-- the ONLY module that knows the vendor exists
```

Exactly one module imports the Alpaca SDK: `app/market_data/providers/alpaca.py`.
Everything downstream sees `CandleData`, `Quote` and `CorporateAction`. Swapping
providers is a registry change, not a refactor.

## Providers

| Provider | Credentials | Network | Purpose |
|---|---|---|---|
| `mock` | none | never | Deterministic testing. **Mandatory, never removed.** |
| `alpaca` | required | yes | Real US equity data |

Selected with `TRADABOT_MARKET_DATA_PROVIDER`. The default is `mock`, so a fresh
clone runs, tests and demos with no account anywhere.

The mock provider is not a leftover. Every test in the repository depends on the
same seed producing the same candles; without it the suite would be a slow,
flaky, rate-limited integration test of somebody else's uptime.

## Commands

```bash
# What is configured, and how fresh is what we hold?
tradabot market-data status

# An explicit historical window
tradabot market-data import NVDA --start 2024-01-01 --end 2024-06-30 --timeframe 1d

# Bring symbols up to date from their newest stored bar (watchlist if omitted)
tradabot market-data sync NVDA

# One latest quote
tradabot market-data quote NVDA

# Replay the imported bars through our own paper broker
tradabot simulate --symbol NVDA --from 2024-01-01 --to 2024-06-30
```

Makefile equivalents: `make market-data-status`, `make market-data-import s=NVDA
from=... to=...`, `make market-data-sync`, `make quote s=NVDA`, `make simulate
s=NVDA from=... to=...`.

### Import vs sync

`import` takes an explicit window and is what you want for a backfill. `sync`
starts from the newest bar already stored and requests only what is missing,
with a few bars of deliberate overlap — providers revise recently published bars
(late prints, consolidated-tape corrections), and the upsert makes re-fetching
them free.

Both are idempotent. Re-running either updates rather than duplicates, and the
report separates `received` from `inserted` so an overlap does not read as new
data.

## Instruments are not invented

A candle request against an unknown symbol **fails** rather than creating an
instrument row. Instrument metadata is a deliberate act (`ensure_instruments`),
so a typo produces an error rather than a phantom ticker with a plausible-looking
price history.

For Alpaca, the universe *is* the configured watchlist — see
[providers/alpaca.md](providers/alpaca.md#the-instrument-universe). Importing a
symbol that is not on it requires adding it to
`TRADABOT_MARKET_DATA__WATCHLIST`.

## Time

Everything stored is UTC and timezone-aware. A naive timestamp from a provider is
**rejected**, not assumed to be UTC: guessing a zone silently shifts a bar by
hours and the result looks exactly like real data.

Windows are half-open, `[start, end)`. Alpaca's `end` is inclusive, so the
boundary bar is dropped during normalisation — otherwise consecutive requests
would each claim the same bar.

## Sessions and calendars

`app/market_data/calendars.py` wraps `exchange-calendars`, and it is
**provider-independent** by design: which days the market is open is a fact about
the venue, not about who sells you the data.

It answers the questions that a `weekday() < 5` check gets wrong:

- Good Friday is a market holiday and not a federal one.
- 3 July 2024 was a trading day that closed at 13:00 ET.
- Five trading days after Friday 28 June 2024 is Monday 8 July, because of a
  weekend and Independence Day.

This matters in two places beyond ingestion: **holding periods** count trading
days, and the **daily-loss budget** resets on a new *session*, not at UTC
midnight. A US session runs past 20:00 UTC, so a midnight reset would split one
trading day across two budgets and let a portfolio lose its daily limit twice.

## Quotes

A quote is used for cost modelling and for marking positions at the bid. Two
things about it are configurable because pretending otherwise would be a silent
assumption:

`TRADABOT_MARKET_DATA__MAX_QUOTE_AGE_SECONDS`
: How old a quote may be before it is refused. Executing against a quote from an
  hour ago is executing against fiction.

`TRADABOT_MARKET_DATA__TREAT_PROVIDER_QUOTE_AS_EXECUTABLE`
: Whether a market-data quote may stand in for a broker execution quote. It is an
  approximation. A consolidated or IEX top-of-book is not the price a retail
  broker fills you at, and the difference is a cost the simulation would
  otherwise not see.

**Historical bars carry no quote.** Alpaca's bar endpoint returns trades, not the
book, and tradabot does not yet store historical quotes. A replay therefore uses
the *configured* spread assumption symmetrically around each price — the same
number the signal's cost model used, so scoring and execution stay consistent.
It is an assumption, not a measurement: a real spread widens exactly when it
matters most, and this one does not.

## Data quality

See [data-quality.md](data-quality.md). In short: bad records are **rejected and
reported**, never silently repaired, and a gap in the data is distinguished from
a gap in the market.

## Provenance

Every stored candle records `provider` and `ingested_at`; every instrument records
`provider`. Without it, a suspicious bar has no one to ask about it, and a
mixed-source table cannot be untangled after the fact.

## Corporate actions

Splits are stored, and applied in two independent places:

1. **Price series** — adjustment is computed *on read* and never stored
   ([data-adjustments.md](data-adjustments.md)).
2. **Open positions** — quantity and prices are rescaled so economic value is
   preserved ([paper-trading.md](paper-trading.md)).

Both are needed. Adjusting the series but not the position makes a 2-for-1 split
look like a 50% loss.

Position adjustment happens in two places, because one is not enough: at
**import** (a newly discovered split reaches positions that already exist) and
during a **replay** (a split inside the window reaches positions the replay itself
opened, which did not exist when the import ran). The service is idempotent -- it
records what each position has been adjusted through -- so running it in both
places cannot halve a holding twice.

Cash dividends are stored but **not** credited as cash. Modelling dividend income
needs payment dates, withholding tax and currency handling; a half-implementation
would quietly overstate returns.

## Health

`GET /health/market-data` reports provider configuration and data freshness, and
returns 503 when data is stale or a probe fails.

It is **credential-free**: no key, no prefix, no length — nothing from which a
secret could be reconstructed or confirmed. The live probe is opt-in
(`?probe=true`) because a health check that always hits a rate-limited API is a
reliable way to exhaust your own quota during an incident.

## Backfill windows

Sized per timeframe rather than per calendar span: 20 days of 5-minute bars, 45
of 15-minute, 180 of hourly, 400 of daily. Warming up a 50-period EMA needs about
sixty bars either way, and pulling 400 days of 5-minute data is 30,000 rows to
compute an identical answer.

Candle upserts are chunked to stay below SQLite's bound-parameter ceiling; a
single intraday backfill otherwise exceeds it and the whole insert fails.

## Scheduling

`MarketDataImportService.sync_watchlist` is a plain awaitable with no scheduler
attached. A cron job, a systemd timer or a future in-process scheduler all call
the same method. That boundary is deliberate: scheduling policy is deployment
configuration, not business logic.

## Events

Ingestion emits domain events (`MarketDataSyncCompleted`, `...Failed`,
`StaleMarketDataDetected`, `ProviderDisconnected`) through an `EventPublisher`
that discards them by default. Phase 3.5 attaches a Discord transport by wiring
one up, not by adding network calls to the provider.

Event payloads are redacted at construction, so no transport can publish an
unredacted one — including one written by someone who never read that module.

---

## Historical depth and the `limit` trap (phase 5.5)

Two provider behaviours worth knowing before requesting years of data. Both were
measured, not read from documentation.

### Depth is a rolling ~6-year window

Every symbol and every timeframe on this account returns data from **2020-07-27**
and no earlier — identical to the day, exactly 2,206 days before the probe. It is
an account-level entitlement, and the floor **advances daily**: bars ageing past
~6 years become permanently unavailable.

Requests for earlier windows succeed and return *nothing*. There is no error to
detect, so a backfill that does not probe first will record empty responses as
gaps and retry them forever.

### `limit` truncates by dropping symbols

Alpaca's `limit` applies to the whole response and is honoured by returning
**fewer symbols**, not shorter series. One 5-minute request for 52 symbols over
June 2026:

| `limit` | Symbols returned | Bars |
|---|---|---|
| `10_000` | **6 of 52** | 10,000 |
| `None` | 52 of 52 | 86,592 |

Silently. tradabot therefore sends **no limit** on bar requests and bounds the
response by the date window instead. `AlpacaSettings.max_bars_per_request` is
retained for reference but deliberately not sent.

Complete responses are larger and slower — a 90-day hourly window for 52 symbols
takes ~46 s — so long backfills need
`TRADABOT_ALPACA__REQUEST_TIMEOUT_SECONDS=120`.

See [historical-expansion.md](historical-expansion.md) for chunking, resume and
gap classification.
