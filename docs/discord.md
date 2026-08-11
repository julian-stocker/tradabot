# Discord

The first notification backend. Confined to
`app/notifications/backends/discord.py`, the only module that knows Discord's
protocol — its payload shape, its 2000-character limit, its rate-limit headers.

> **Output only.** Discord is a monitoring surface. No trading logic depends on
> it, nothing reads from it, and there is no bot, no slash command and no
> interactive control. A Discord outage never fails a market-data sync and never
> rolls back a paper trade.

For routing, thresholds and the reliability model, see
[notifications.md](notifications.md).

---

## Channels

Four channels, one webhook each, because Discord scopes a webhook to a channel.

| Channel | Receives | Volume |
|---|---|---|
| `#market-signals` | Qualified / strengthened / invalidated signals, market overviews | Moderate; governed by thresholds and cooldown |
| `#paper-trades` | Grouped entry decisions, individual closes | One message per signal acted on |
| `#performance` | Daily report, portfolio summaries | Once a day |
| `#tradabot-system` | Sync failures, stale data, provider up/down, lifecycle, critical errors | Rare by design — transitions only |

Separate channels rather than one firehose because they are read at different
times and for different reasons: a system alert needs attention now, a daily
report does not, and mixing them means neither gets read properly.

## Setup

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, one
   per channel. Copy each URL.
2. Put them in your **`.env`**, which is git-ignored:

```bash
TRADABOT_DISCORD__ENABLED=true
TRADABOT_DISCORD__MARKET_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__TRADES_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__PERFORMANCE_WEBHOOK=https://discord.com/api/webhooks/...
TRADABOT_DISCORD__SYSTEM_WEBHOOK=https://discord.com/api/webhooks/...
```

3. Verify:

```bash
tradabot notifications test        # or: make notify-test
```

Each configured channel receives a message labelled `🧪 TRADABOT TEST` naming the
channel and the environment. Nothing secret is included.

Configuring only some channels is fine. An event whose category has no webhook is
recorded as `skipped` — not a failure, because nothing was attempted.

## Security

**A webhook URL is a bearer credential.** Anyone holding one can post to that
channel as tradabot. Treated exactly like an API key:

- held as `SecretStr` — absent from reprs, `model_dump()` and log output;
- never logged, never returned by an endpoint, never written to disk;
- `.gitignore` excludes `.env`; `.env.example` carries empty placeholders only;
- `app/core/redaction.py` masks **any** URL containing `/webhooks/`, wherever it
  appears.

That last one is not theoretical. HTTP clients routinely put the request URL into
their exception strings, and that string travels into a log line, an audit row
and an HTTP response. The whole URL is masked rather than just the token, because
the id identifies the channel and no error message needs any part of it.

Redaction is applied at three boundaries, deliberately overlapping: inside the
Discord backend, at `NotificationService` where *any* backend's error text
enters, and at the health endpoint. Defence in depth — the primary control is
still not putting secrets into strings at all, and pattern-based redaction is
necessarily incomplete.

`GET /health/notifications` reports channel **names** and counts. No URL, no
token, no fragment of one.

Messages also set `allowed_mentions: {"parse": []}`, so a monitoring channel can
never `@everyone` a room even if a symbol name or a formatter ever produced one.

## Delivery

| | |
|---|---|
| Retried | 429, 500, 502, 503, 504 |
| Not retried | 401, 403, 404 (revoked webhook), other 4xx |
| Backoff | Exponential with full jitter, capped |
| `Retry-After` | Honoured, from header or JSON body |
| Attempts | `max_retries + 1`, default 4 |
| Timeout | 10s per attempt |

Discord's 429 is authoritative: it knows when it will accept traffic again, and
ignoring the header is how a client earns a longer ban. Without jitter, several
notifications rate-limited at once would retry in lockstep and rate-limit each
other again.

A 404 means the webhook was deleted. Retrying it only delays the operator
learning that, so it fails immediately and lands in the audit table.

**Delivery never raises.** Every failure becomes a `DeliveryResult` with
`delivered=False`. This is the property the whole design rests on — an exception
escaping this module is how a Discord outage becomes a lost trade.

| Setting | Default |
|---|---|
| `TRADABOT_DISCORD__REQUEST_TIMEOUT_SECONDS` | 10.0 |
| `TRADABOT_DISCORD__MAX_RETRIES` | 3 |
| `TRADABOT_DISCORD__BACKOFF_BASE_SECONDS` | 0.5 |
| `TRADABOT_DISCORD__BACKOFF_MAX_SECONDS` | 15.0 |
| `TRADABOT_DISCORD__USERNAME` | tradabot |

## Message limits

Discord rejects content over 2000 characters. Messages are truncated to
`TRADABOT_NOTIFICATIONS__MAX_MESSAGE_CHARACTERS` (1900 by default; the margin
absorbs the wrapper) with a visible marker.

Truncation drops from the end. Formatters put symbol, score, decision and result
first, so what is lost is a long tail of explanation — and losing that is much
better than failing delivery, which loses the alert entirely.

## Auditing

Every attempt is recorded in `notification_attempts`: event type, category, key,
backend, status, attempt count, status code, redacted error, and timestamps.

**Message content is not stored** — it would duplicate the signal and trade
tables and grow without bound. Consequently a failed delivery cannot be replayed
from the audit row; see the reliability section in
[notifications.md](notifications.md#delivery-reliability--stated-precisely) for
what is and is not recoverable.

```bash
tradabot notifications status      # counts, last success, last failure
curl localhost:8000/health/notifications
```

## Testing

Every test uses `httpx.MockTransport`. **No test opens a socket to Discord**, and
the suite runs with no webhook configured. The strongest assertions are negative:
that a webhook URL appears in no error, no log line, no audit row and no HTTP
response.

## Not implemented, deliberately

Slash commands, an interactive bot, reading messages, reacting to them, or any
path by which Discord could influence trading. Discord is where tradabot *writes*
what it decided; it is never where a decision comes from.
