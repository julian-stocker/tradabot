# Data quality

What is checked when market data arrives, what happens to records that fail, and
why the answer is never "fix it quietly".

---

## The rule

**Reject and report. Never silently repair.**

A bar whose high is below its open is corrupt. There is no way to know which
field is wrong, and every repair is a guess that produces data indistinguishable
from the real thing. Downstream, a repaired bar is worse than a missing one: a
gap is visible, an invention is not.

So every check either accepts a record or rejects it with a reason, and the
rejection is counted in a report the caller receives — not buried in a log file
to be discovered afterwards.

## What is checked

### Structural (at normalisation)

`CandleData` and `Quote` enforce these as construction invariants, so an invalid
one cannot exist as an object:

```
low <= open  <= high
low <= close <= high
volume >= 0
all prices > 0
bid <= ask                (a crossed quote is refused)
timestamp is timezone-aware
```

A naive timestamp is rejected rather than assumed to be UTC. Guessing a zone
shifts a bar by hours and the result looks exactly like real data — the most
expensive kind of wrong.

### Series-level (after storage)

`check_series` inspects a stored range for what a per-record check cannot see:

- duplicate timestamps
- non-monotonic ordering
- **calendar-aware gaps**

### Duplicates

Deduplicated at normalisation, keyed on timestamp, and counted separately from
rejections. A duplicate is usually a paging artefact rather than bad data, and
conflating the two trains the reader to ignore the report.

The database enforces uniqueness on `(instrument, timeframe, timestamp)`
regardless, which is what makes re-import idempotent.

## Gaps

> **A gap in the data is not the same as a gap in the market.**

This is the distinction the whole module exists for. A naive detector flags every
weekend, every holiday and every early close, produces hundreds of false
positives, and teaches its reader to skip the section.

So gaps are computed against the **exchange calendar**:

| Situation | Gap? |
|---|---|
| Friday bar, Monday bar | No — the market was shut |
| Wednesday 3 July, Friday 5 July | No — Independence Day |
| Monday bar, Wednesday bar | **Yes** — Tuesday was a trading day |
| Missing 09:35 bar in a session | **Yes** (intraday, within a session) |

Intraday gaps are only checked *within* a single session. Across a session
boundary the expected bar count depends on half-days, early closes and
pre/post-market inclusion; guessing it would generate exactly the false positives
this design avoids.

## Expected bar count

`expected_bar_count` returns the number of sessions for daily and weekly
timeframes, and **`None` for intraday**.

Returning `None` rather than a plausible estimate is deliberate. A wrong
expectation is worse than an admitted unknown: it makes a correct import look
broken, and after the third false alarm nobody checks the number again.

## Quote staleness

A quote older than `TRADABOT_MARKET_DATA__MAX_QUOTE_AGE_SECONDS` (default 900) is
stale and refused for execution.

A quote from the **future** is also stale. It means a clock or wiring problem,
and treating it as fresh would be trusting the one input that is provably wrong.

Note that staleness is a property of the quote, not of the market: outside
trading hours the last quote is legitimately hours old. That is why the health
endpoint reports age and lets the operator judge, rather than declaring the
market broken every evening.

## The report

`ImportReport` separates numbers that are routinely confused:

| Field | Meaning |
|---|---|
| `expected_bars` | From the calendar. `None` when unknowable. |
| `received_bars` | What the provider returned |
| `inserted_bars` | Newly written |
| `existing_bars` | Already held for that window |
| `rejected_bars` | Failed validation |
| `gaps` | Calendar-aware missing stretches |
| `corporate_actions` | Actions stored |
| `positions_adjusted` | Open paper positions rescaled for a split |

`received` and `inserted` differ on every incremental sync, because syncs overlap
deliberately. Collapsing them into one "imported" count would make normal
operation look like duplication.

## Partial failure

One bad bar does not discard a good response. One bad symbol does not abort a
watchlist sync. A provider error becomes `report.error` rather than an exception,
so a nightly job reports what it managed and what it did not.

The exception is a failure that has exhausted the retry budget on a single
requested window: that raises, because returning an empty list would be
indistinguishable from "there were no bars".

## What quality checks do not tell you

They confirm the data is *internally consistent and complete*. They cannot
confirm it is *correct*: a feed that reports the wrong price consistently passes
every check here.

They also say nothing about whether the data supports a strategy. Clean data and
a profitable backtest are independent claims, and this module only makes the
first one.
