# Historical expansion

How stored market history is deepened, and the two provider behaviours that make
it harder than it looks.

```bash
make storage-plan FROM=2020-07-27 TO=2026-08-11    # project first
python -m app.cli history --from 2020-07-27 --to 2026-08-11 --universe active --dry-run
python -m app.cli history --from 2020-07-27 --to 2026-08-11 --universe active
```

Always run the plan first. The command refuses outright if the disk gate says
`UNSAFE`.

## Provider depth is a rolling window, not an archive

Probed directly against the configured account:

| Symbol | Timeframe | Earliest obtainable |
|---|---|---|
| NVDA / AAPL / JPM | 1d, 1h, 15m, 5m | **2020-07-27** |

Identical to the day across every symbol and every timeframe, and exactly
**2,206 days = 6.04 years** before the probe date. That is an account-level
entitlement, not per-symbol listing history.

Two consequences that shape everything else:

1. **2020-01-01 is not obtainable at any chunk size.** Requests for earlier
   windows return empty successfully — no error, just nothing. A backfill that
   did not probe would record those as gaps and retry them forever.
2. **The floor advances daily.** Data ageing past ~6 years becomes permanently
   unavailable. There is no "fetch it later": a candle downloaded today is the
   only copy that will ever exist locally, which is why raw candles are treated
   as irreplaceable in docs/storage-planning.md.

Depth was verified per timeframe rather than assumed. It happens to be uniform
here; that is a measurement, not a guarantee for another account or feed.

## The `limit` trap

Alpaca's `limit` parameter applies to the **whole response**, and it is honoured
by **dropping symbols entirely** rather than by shortening each series.

Measured — one 5-minute request, 52 symbols, June 2026:

| `limit` | Symbols returned | Bars |
|---|---|---|
| `10_000` | **6 of 52** | 10,000 |
| `None` | 52 of 52 | 86,592 |

No error, no warning, no partial-result flag. A multi-year backfill built on the
capped request would have silently stored a dataset missing 88% of the universe,
and every statistic computed from it would have been wrong in a way no validation
rule would catch — the bars that *were* stored are perfectly valid.

Bar requests therefore send **no limit**. The response is bounded by the date
window instead, which bounds it honestly. See `app/market_data/providers/alpaca.py`.

The trade-off is that complete responses are large and slow: a 90-day hourly
window for 52 symbols takes ~46 s, which exceeds the default 30 s request
timeout. Raise it for long backfills:

```bash
TRADABOT_ALPACA__REQUEST_TIMEOUT_SECONDS=120 python -m app.cli history ...
```

## Chunking

| Timeframe | Days per request | ≈ rows per request (52 symbols) |
|---|---|---|
| 5m | 14 | ~40 k |
| 15m | 45 | ~46 k |
| 1h | 90 | ~24 k |
| 1d | 365 | ~13 k |

Sized by expected **rows**, not days, so each request lands in the same range and
none of them times out. One batched request covers the whole universe — the
per-symbol alternative would be ~6,200 round trips and hours of pure latency.

Windows are always issued **oldest first**, so an interrupted run leaves a
contiguous frontier rather than islands.

## Resume

A run that fails at 90% must continue from 90%. Progress is derived from the
database, never from a checkpoint file — a checkpoint can disagree with reality
after a crash and the database cannot.

**Coverage is measured session by session.** An earlier version compared the
*oldest stored bar* against the window start, which assumes history was filled
contiguously. It was not: one exploratory 2020 chunk plus the live sync's recent
data made every window in between look covered, and a five-year hole in the
hourly series was reported as "25 chunks already complete". The hole was only
visible by counting sessions per year:

```
2020: 64 sessions      2021-2025: 0      2026: 124 sessions
```

Now `_covered()` asks whether the trading sessions inside a window actually have
data, with a 5% tolerance so a thin symbol missing one session does not cause an
infinite re-download.

## Retries and failures

Each chunk retries up to 3 times with linear backoff. After that it is recorded
as failed and **the run continues** — one bad window must not cost the other 99%.
Failed chunks are listed at the end and are individually retryable simply by
re-running the command, since resume will skip everything that succeeded.

## Coexistence with the scheduler

The production scheduler keeps running throughout. Chunks commit independently
and briefly, so the live 5-minute sync waits milliseconds for the SQLite write
lock rather than hours. With WAL and `busy_timeout=5000` this is not merely
survivable, it is unnoticeable.

There is no need to stop the scheduler for a backfill, and it should not be
stopped: the gap it would leave in recent data is a worse problem than the lock
contention it avoids.

## Provenance

Every backfilled candle carries `provider='alpaca'` and an `ingested_at` stamp,
written per bar. The `history` command **refuses to run** when the configured
provider is not Alpaca, so `MockMarketDataProvider` can never contaminate the
historical archive.

After any expansion:

```sql
SELECT COUNT(*) FROM candles WHERE provider != 'alpaca';   -- must be 0
```

## Validation

After each batch: duplicate timestamps (impossible by primary key), OHLC
invariants (enforced by CHECK constraints), negative volume (CHECK), chronological
order, and calendar-aware gap classification.

Gaps are classified before they are reported (`app/market_data/gaps.py`):

| Kind | Meaning | Actionable |
|---|---|---|
| `EXPECTED_MARKET_CLOSURE` | weekend, holiday, outside session | no |
| `SYMBOL_NOT_TRADING` | before the instrument's first known bar | no |
| `PROVIDER_MISSING` | market open, nothing returned | **yes** |
| `UNKNOWN` | partial session; worth looking at individually | **yes** |

Roughly 70% of naive "gaps" on US equities are the market being shut. Reporting
those as data problems trains the reader to ignore the report.

**Nothing is ever interpolated.** A fabricated bar is indistinguishable from a
real one once stored, and every statistic downstream inherits it.

## Staging depth by how a timeframe is used

Depth does not have to be uniform, and making it uniform is often the wrong call.
The hourly series is the backtest's primary timeframe and daily is the macro
context, so both justify the full provider window. The 15-minute and 5-minute
series are confirmation and entry context — they need to cover the *benchmark*
window at full fidelity, not the entire archive.

Allocating depth that way costs a fraction of the runtime and loses nothing that
is currently used. It is recorded here because the reasoning, not the specific
dates, is what should be reused.
