# Provider connections

How tradabot records external accounts, where credentials actually live, and what
a future per-user Alpaca connection would look like.

> **Nothing in the database is a secret, and nothing ever will be.**
> `external_account_connections.credential_reference` is a **pointer**. A raw
> `api_secret` column would be one database backup away from a leak — in a file
> that gets copied to laptops, synced to cloud storage and attached to issues.

---

## Current arrangement

```
ExternalAccountConnection
  owner                  local-user
  provider               ALPACA
  purpose                MARKET_DATA
  environment            PAPER
  connection_status      CONFIGURED
  credential_reference   env:TRADABOT_ALPACA__API_KEY+API_SECRET
```

The secret store is **environment variables** (`.env`, git-ignored). The row
above says *where* the credential is, not what it is.

### The table does not supply credentials

`app/market_data/registry.py` builds the provider from settings and **never reads
this table**. If it did, a database row could change which credentials the system
authenticates with — a far larger attack surface than a configuration file, and a
much harder one to audit. A test asserts the registry contains no reference to
the connection model.

The table answers "what is connected, by whom, for what purpose". That is the
question a second user makes hard, and it is worth recording before then.

## Purpose is separate from provider

| Purpose | Implemented | Risk |
|---|---|---|
| `MARKET_DATA` | ✅ | Read-only |
| `PAPER_TRADING` | ❌ | Would place orders on a broker's simulator |
| `LIVE_TRADING` | ❌ | Real money. A permanent non-goal for automation. |

Recorded distinctly so a connection cannot silently acquire trading scope because
it happens to be with the same vendor. tradabot authenticates against Alpaca's
**data** API only; no trading endpoint is configured anywhere.

## Environment variables

```bash
TRADABOT_ALPACA__API_KEY=
TRADABOT_ALPACA__API_SECRET=      # or TRADABOT_ALPACA__SECRET_KEY
```

`SECRET_KEY` is accepted because Alpaca's dashboard calls the pair "API Key ID"
and "Secret Key". The mismatch failed in the worst way — key read, secret
silently ignored, `is_configured=False` with both variables plainly present.

## Future: per-user Alpaca via OAuth

**Not implemented. Do not read this as available.**

```
Discord user
  → /connect alpaca
  → tradabot returns a one-time authorization URL
  → user authorizes in their own browser, on Alpaca's domain
  → Alpaca redirects with a code
  → tradabot exchanges it for a token
  → token stored in a secret store; a REFERENCE stored in the database
  → connection linked to that TradabotUser
```

Two properties that matter:

**Users never paste an API secret into Discord.** A message is stored on
Discord's servers, visible to anyone with channel history, and unrevocable by
tradabot. Any flow that asks for one is wrong, no matter how convenient.

**This is separate from the global market-data connection.** A user connecting
their own account gets a `PAPER_TRADING` connection; market data continues to
come from the single global one. Conflating them would mean a user disconnecting
their account broke everyone's data feed.

## Future: a real secret store

For a single local user, `.env` is appropriate: it is one file, owned by one
person, on one machine, already git-ignored.

It stops being appropriate the moment there is a second user, because their
credential would sit in a file the first user can read. At that point
`credential_reference` points at something else — macOS Keychain, `age`-encrypted
files, or a managed KMS — behind a `SecretStore` interface. The database schema
does not change, which is the point of storing a reference from the start.

**Not built now.** Building a secret-management system for one local user would
be work with no beneficiary, and the interface it would sit behind does not exist
yet either.

## Related

- [multi-user-roadmap.md](multi-user-roadmap.md)
- [providers/alpaca.md](providers/alpaca.md)
- [discord.md](discord.md#security)
