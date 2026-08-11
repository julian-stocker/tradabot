# Alpaca

The only real market-data provider tradabot implements. Confined to one module,
`app/market_data/providers/alpaca.py`, which is the sole place in the codebase
that knows this vendor exists.

> **Market data only.** tradabot authenticates against Alpaca's *data* API and
> never its trading API. It places no orders — not live, not paper. Alpaca's own
> paper-trading engine is deliberately unused: it would apply their fill model,
> their costs and their assumptions, which is exactly the machinery tradabot
> exists to make explicit.

---

## The SDK

`alpaca-py` (the official SDK). `alpaca-trade-api-python` is deprecated and
archived; do not add it.

Its clients are **synchronous**. Rather than pretend otherwise, every call is run
off the event loop with `asyncio.to_thread` and bounded by
`asyncio.wait_for`. Wrapping a blocking client in an `async def` without moving
it off the loop would stall every other request in the process.

The SDK is imported **lazily**, inside the methods that need it. The module must
be importable — and the API must start — on a machine with no credentials and no
SDK installed.

## Credentials

Two environment variables, and nothing else:

```bash
TRADABOT_ALPACA__API_KEY=
TRADABOT_ALPACA__API_SECRET=
```

Create them at <https://app.alpaca.markets>. **Paper-account keys are
sufficient** — tradabot only reads market data, so a key with trading permission
grants more access than the job needs.

Held as `SecretStr`, so they do not appear in a repr, a `model_dump()` or a log
line. They are never interpolated into an exception message, never returned by an
endpoint, and never written to `.env.example`.

If they are absent, the provider raises a `ConfigurationError` naming the
variables and pointing at `mock` — not a stack trace from inside the SDK.

## Feeds

`TRADABOT_ALPACA__FEED` selects the data source, and the choice changes what the
numbers mean:

| Feed | Cost | Coverage |
|---|---|---|
| `iex` (default) | free | **One venue.** Roughly 2–3% of consolidated volume. |
| `sip` | paid | Full consolidated tape. |
| `delayed_sip` | free | Consolidated tape, 15 minutes late. |

The default is `iex` because it is free, but its limitation is real and worth
stating plainly: IEX bars are one exchange's view. Volume is a fraction of the
true figure, and the high/low of a bar can miss prints that happened elsewhere.
Any volume-based signal component computed on IEX data is measuring IEX, not the
market.

`delayed_sip` is the better free choice for *historical research*, and `iex` the
better one for anything that needs to be current. Neither is suitable for
inferring microstructure.

## Historical bars

Requested with `Adjustment.RAW`. tradabot stores raw prices and computes
split-adjusted and total-return series **on read**, from its own corporate-action
table — see [../data-adjustments.md](../data-adjustments.md). Storing a
provider's pre-adjusted prices would mean the stored history silently changes
meaning every time a new split occurs.

Windows are half-open `[start, end)`. Alpaca's `end` is inclusive, so a bar
landing exactly on the boundary is dropped during normalisation; otherwise two
consecutive requests would each return it.

Pagination is handled by the SDK. `max_bars_per_request` caps a single request at
Alpaca's own limit of 10,000.

## Quotes

`get_latest_quote` returns top-of-book. A crossed book (ask below bid) or a
non-positive price is **refused**, not stored: a nonsensical quote reaching the
cost model corrupts every figure downstream of it.

## Corporate actions

Alpaca's model maps cleanly onto the phase 2 domain, so nothing was rebuilt:

| Alpaca | tradabot | Note |
|---|---|---|
| `old_rate` | `from_shares` | Exact integers, never a float ratio |
| `new_rate` | `to_shares` | |
| `ex_date` | `effective_at` | |
| `rate` (dividends) | `cash_amount` | Stored; not credited as cash |

Keeping the ratio as an exact pair means a 3-for-2 stays 3:2 and does not
compound rounding across successive actions.

A provider that fails to return corporate actions does **not** fail the candle
import — but the absence is logged, because "no actions" and "no action data" are
indistinguishable downstream and the second one silently turns a split-adjusted
series into a raw one.

## The instrument universe

Alpaca's asset catalogue lives behind the **trading** API, which tradabot
deliberately does not authenticate against. So the configured watchlist *is* the
universe:

```bash
TRADABOT_MARKET_DATA__WATCHLIST=AAPL,MSFT,NVDA,AMD,AMZN,META,GOOGL,TSLA
```

Importing a symbol that is not on the watchlist logs a warning naming it and
imports nothing for it. The fix is to add it to the watchlist — not for the
import to conjure an instrument row, which would break the rule that a candle
request never creates one.

Instrument metadata from this provider is minimal by design: name and listing
dates would need a reference source, and inventing them is worse than leaving
them empty.

## Retries and rate limits

```
retryable      429, 500, 502, 503, 504
terminal       401, 403 (credentials), 404 (no data), 422 (bad request), other 4xx
```

Retrying a 401 four times only delays telling the operator their key is wrong, so
authentication failures are raised immediately.

Backoff is **exponential with full jitter**, capped by `backoff_max_seconds`.
Without jitter, several symbols rate-limited at the same moment retry in lockstep
and rate-limit each other again. A `Retry-After` header, when present, overrides
the computed delay — the provider knows better than our curve when it will accept
traffic, and ignoring the header is how a client gets itself banned.

The retry count is **bounded** (`max_retries`, default 4, hard-capped at 10). An
unbounded retry loop turns an outage into a hang.

Exhausting the budget raises. It never returns an empty list: silence would be
indistinguishable from "the market was closed", and a caller would store it as
data.

| Setting | Default | Purpose |
|---|---|---|
| `TRADABOT_ALPACA__REQUEST_TIMEOUT_SECONDS` | 30.0 | Per-attempt ceiling |
| `TRADABOT_ALPACA__MAX_RETRIES` | 4 | Attempts = this + 1 |
| `TRADABOT_ALPACA__BACKOFF_BASE_SECONDS` | 0.5 | First delay |
| `TRADABOT_ALPACA__BACKOFF_MAX_SECONDS` | 30.0 | Delay ceiling |
| `TRADABOT_ALPACA__MAX_BARS_PER_REQUEST` | 10000 | Alpaca's own limit |

## Error messages

Every provider error passes through `app/core/redaction.py` before it reaches a
log line, an event payload or an HTTP response. SDKs sometimes echo request
context into exception strings, and an error message is the easiest way for a key
to reach somewhere permanent.

That is **defence in depth, not the primary control**. The primary control is not
putting secrets into strings at all. Redaction is pattern-based and therefore
incomplete: never rely on it to make an otherwise-unsafe value safe to publish.

## Testing

Unit tests stub the SDK's *shape* — attribute access on bar objects, a `data`
mapping keyed by symbol — rather than importing it. They run with no credentials,
no network and no SDK, and they fail loudly if our mapping drifts from what this
document claims.

The live smoke test is opt-in and skipped by default:

```bash
TRADABOT_RUN_EXTERNAL_TESTS=1 pytest tests/external -v   # or: make smoke-real-data
```

It asserts on structure and consistency, never on values. A test that asserted
NVDA's close would fail every day for the right reason and teach nothing.
