# Monitoring

## What it decides

One question: **is anything worth telling someone about?** — and, far more
often, being able to answer no.

It observes the Advisor, Portfolio Fit, market data, the SEC fact store and
read-only account snapshots, compares each against the previous run, and returns
a ranked list of material changes.

It has no transport. Delivering a message is a separate concern with its own
credentials and failure modes, and mixing it in would make the materiality rules
untestable without a network.

It expresses no action. Every event describes a transition that occurred; none
recommends a response, because no validated predictive evidence supports one. A
test asserts that vocabulary never appears, and that the package imports no
broker, no vendor SDK and no database session.

## Running it

```
tradabot monitor run                          # prices, sectors, data health
tradabot monitor run --companies --accounts   # add fundamentals and paper accounts
tradabot monitor run --evidence --limit 10    # show the measurements behind each
tradabot monitor digest --days 7              # what mattered this week
```

`--companies` runs the Advisor once per watched symbol, which is the expensive
part (~60s for 52 symbols) and only changes on filing days, so it is off by
default. `--accounts` reads the paper accounts read-only.

## Events

Every event carries a timestamp, subject, previous state, current state,
materiality, evidence, confidence, provenance, scope (with the account, for
portfolio events) and a deduplication key.

| scope | kinds |
|---|---|
| market | `MARKET_REGIME_CHANGE` |
| sector | `SECTOR_MOVE` |
| company | `UNUSUAL_VOLUME`, `UNUSUAL_VOLATILITY`, `RELATIVE_STRENGTH_CHANGE`, `NEW_SEC_FILING`, `FUNDAMENTAL_CHANGE`, `VALUATION_STATE_CHANGE`, `COMPANY_CONFIDENCE_CHANGE` |
| portfolio | `POSITION_ADDED`, `POSITION_REMOVED`, `PORTFOLIO_WEIGHT_CHANGE`, `PORTFOLIO_CONCENTRATION_CHANGE`, `SECTOR_CONCENTRATION_CHANGE`, `CORRELATION_CLUSTER_CHANGE`, `CASH_LEVEL_CHANGE` |
| system | `DATA_HEALTH_CHANGE` |

## How silence is produced

Four stages, in order:

**Materiality.** A change below its declared threshold is `ROUTINE`: detected,
counted, never reported. The reporting floor is `NOTABLE`.

**Deduplication within the run.** The same key twice in one pass is one event.

**Cooldown across runs.** A measure hovering at its threshold crosses back and
forth, producing a genuinely new transition each time. Cooldowns are per kind —
a regime is quiet for a week, a filing never repeats because its key is the
accession.

**Ranking.** Materiality first, then confidence, then magnitude, with a stable
tiebreak. `--limit` truncates the display, never the record.

The first run of a fresh install reports **nothing**. It records a baseline; a
system that narrated the entire current state of the world on startup would be
saying nothing that *changed*.

## Where the thresholds come from

Three sources, and one that is deliberately excluded.

**Borrowed.** Weight, sector and correlation thresholds are imported from
`app.portfolio_fit`, which measured them against the real distribution of equity
pair correlations. A second copy would drift silently.

**Distribution-anchored.** Volume and volatility ratios were declared first, then
measured across 120 sessions × 52 symbols. The declared 2.5× volume threshold
fires on 1.8% of observations and 1.6× volatility on 3.0% — both near the 98th
percentile, so "unusual" means "rare", measured.

**Structural.** A filing either appeared or it did not.

**Never fitted to an outcome.** No threshold is tuned against a forward return or
a hit rate. That would be alpha research wearing a monitoring costume, and Phase
12.25 established that no such relationship in this data survives out of sample.
These thresholds answer "is this rare or large?", never "is this good?".

## Expected volume

Measured by replaying 120 sessions over 52 watched symbols:

| | mean/session | median | max | quiet |
|---|---|---|---|---|
| price-only sessions (96) | 1.9 | 2 | 7 | 20% |
| company-pass sessions (24) | 15.5 | 11.5 | 53 | — |

The distribution is bimodal, and correctly so. Ordinary days are quiet. Earnings
season is not: the busiest session carried 14 new filings and 31 trailing-figure
moves across the watchlist, all of them real. That is what the ranked `--limit`
and the weekly digest are for.

Four noise controls were added *after* measuring, each fixing a defect the
replay exposed:

- market capitalisation and price-to-sales excluded from `FUNDAMENTAL_CHANGE` —
  both move with the share price, so a 10% price move was being reported as a
  change in the business;
- the valuation band must hold for two consecutive company passes, because a
  percentile band flips on ordinary movement near a boundary (172 → 77 events);
- deduplication keys no longer include the destination band, so oscillating
  between two states stops producing a fresh key each time;
- the unusual-volume cooldown moved from 24h to 72h.

## State

The baseline lives in `data/monitor_state/`, one JSON file per scope, written
atomically. Reported events are appended to `data/monitor_events/`, partitioned
by month, which is what lets a weekly digest read a range.

Neither is in the trading database. Giving this layer a write path into the
database that holds instruments, candles and paper decisions would mean the
read-only guarantee rested on nobody misusing an open session; keeping its
memory separate means there is no such path to misuse. The cost is that
monitoring state is not transactional with trading state — losing it costs one
quiet run while the baseline is relearned.

A damaged baseline is discarded rather than half-read: trusting it would report
every subject as changed at once, which is the worst false alarm available.

## Next

A transport. The engine decides what to say; nothing yet says it.
