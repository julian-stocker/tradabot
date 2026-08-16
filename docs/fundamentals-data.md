# SEC fundamentals: data ownership and rebuild

## `data/sec_facts.parquet` is production data

It is not a research artifact, not an export, and not a cache of a research run.
The Advisor reads it on every request and fetches nothing on demand, so if this
file is absent the Advisor has no fundamentals at all.

This distinction was learned the hard way. The store was originally produced by
a Phase 12.22 research script writing into `reports/phase12_22/`. When that
directory was later deleted the Advisor lost its data source, and the only
surviving copy was in a session-scoped temporary directory that would have been
cleaned within days.

Consequences of that rule:

- Nothing under `reports/` may be an input to a production path.
- The store lives under `data/`, alongside the other durable local state.
- It must be rebuildable from scratch, from a source that is not this machine.

## Rebuilding it

```
tradabot fundamentals sync
```

Reads every instrument that has daily price history in the local database,
fetches each company's XBRL facts from SEC EDGAR, and writes the store. No
credentials are involved: EDGAR is public and free, and asks only for a
descriptive `User-Agent`, which you can override with `TRADABOT_SEC_USER_AGENT`.

A full rebuild of roughly a thousand symbols takes about twenty minutes, bounded
by SEC's ten-requests-per-second courtesy limit rather than by local compute.

The sync is:

- **idempotent** — the same inputs produce a byte-identical file, because rows
  are sorted by a stable key before writing;
- **resumable** — each symbol's filtered payload is cached under `data/sec_cache/`
  as it arrives, so an interrupted run continues instead of re-downloading;
- **fail-soft** — one company's outage is recorded and skipped rather than
  aborting the run.

Useful flags: `--symbols AAPL,MSFT` to sync a subset, `--force` to ignore the
cache, `--store` and `--cache` to redirect either path.

## Checking it

```
tradabot fundamentals status
```

Reports one of four states, kept distinct because each calls for a different
response:

| state | meaning | what to do |
|---|---|---|
| `DATA_NOT_SYNCED` | no file on disk | run a sync |
| `DATA_CORRUPT` | present but unreadable, empty, or missing required columns | investigate, then re-sync |
| `DATA_STALE` | readable, but the newest filing is over 30 days old | run a sync |
| `READY` | usable | nothing |

Status is a local file operation and never reaches the network. An Advisor
request that quietly fetched when its data looked old would turn a 30-millisecond
local call into an unbounded one, at exactly the moment someone was waiting.

## What is stored

One row per XBRL fact, carrying the concept, taxonomy, unit, period, form,
filing date, acceptance timestamp, accession and value.

Two fields deserve a note:

**`filed`** is the point-in-time key. A fact is visible to a query only from the
filing that published it, so a restatement never rewrites what was knowable
earlier.

**`accepted`** is the acceptance timestamp from the submissions API — the moment
the document actually became public, which can fall after the close of the
`filed` session. It is recorded as provenance. It is deliberately *not* the
visibility key: changing that would alter Advisor behaviour that has been
validated against `filed`, and an ingestion change is not the place to do it.

## Known coverage gaps

- **ETFs** (`SPY`, `QQQ`, the sector funds) file no company fundamentals. They
  are correctly absent, and the Advisor reports their fundamentals as
  unavailable while still describing their market position from prices.
- **`AEP`** is missing from both of SEC's ticker-to-CIK files, so it cannot be
  resolved. This is an EDGAR-side gap, not a local one.
- A handful of foreign issuers return no `companyfacts` document.

Every one of these is reported by the sync as `unmapped` or `UNAVAILABLE`. None
is silently filled in.
