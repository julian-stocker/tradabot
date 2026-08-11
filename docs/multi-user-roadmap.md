# Multi-user roadmap

**Nothing here is implemented.** This documents the boundary Phase 4.1 prepared
and what would build on it — so that adding a second person later is a migration
of data rather than a redesign.

---

## Current state

```
TradabotUser "local-user"          ← one row, created automatically
├── paper-100    → #paper-100
├── paper-1000   → #paper-1000
├── paper-10000  → #paper-10000
└── 9 generic phase-3 profiles (no notification channel)

ExternalAccountConnection
└── ALPACA / MARKET_DATA / PAPER   ← global, credential_reference only
```

One owner. One global Alpaca market-data connection. Three personal portfolios,
each with its own Discord channel. **No authentication of any kind.**

## What exists, and what does not

| | Status |
|---|---|
| `tradabot_users` table | ✅ exists, one row |
| `simulation_profiles.owner_id` | ✅ exists, nullable |
| `external_account_connections` | ✅ exists, records the global connection |
| Login / sessions / passwords | ❌ not implemented |
| Per-user watchlists | ❌ not implemented — the watchlist is global |
| Per-user API scoping | ❌ not implemented — endpoints return everything |
| Discord bot | ❌ webhooks only, outgoing |
| Alpaca OAuth | ❌ not implemented |

**Do not read the tables as multi-user support.** They are the boundary that
makes it cheap later. The API has no notion of a caller.

## Why the columns exist now

`simulation_profiles` accumulates live state — cash, positions, realised P&L.
Adding an ownership column to a table in that condition means migrating live
financial records; adding it while the table holds configuration is free. Both
new columns are nullable and default to the local owner, so nothing changed
behaviour.

## What a second user would need

Roughly in order:

1. **Identity.** `external_identity_type` already anticipates `DISCORD`, so a
   Discord user id becomes an owner row without a schema change.
2. **Per-user watchlists.** The watchlist is currently global — one table with no
   owner. This is the largest piece and the reason it is not "just add a user".
3. **Scoping in the API and CLI.** Every read currently returns everything.
4. **Fan-out by owner.** The scanner evaluates globally and fans out to
   portfolios; it would fan out to *each owner's* portfolios. `PaperBroker` needs
   no change — it already operates per profile.
5. **Authorization.** Who may see whose portfolio. Deliberately unaddressed.

The target shape:

```
Julian                     Brother
├── paper-100              ├── paper-250
├── paper-1000             └── paper-1000
└── paper-10000
```

Note both users having a `paper-1000`: portfolio *names* would need to be unique
per owner rather than globally, which is a constraint change worth knowing about
before it bites.

## What stays global

**Market data.** One Alpaca connection, one scan, one signal, fanned out. Two
users watching NVDA must not produce two API calls — that is the scaling property
the architecture protects, and it is asserted by a test.

Analysis is a fact about the market, not about a user. Only *decisions* are
per-portfolio.

## Related

- [provider-connections.md](provider-connections.md) — credentials and OAuth
- [notifications.md](notifications.md) — portfolio routing
- [simulation-design.md](simulation-design.md) — portfolios and profiles
