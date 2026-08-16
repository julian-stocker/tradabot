# Portfolio Fit

## What it answers

How a company sits inside a *specific* portfolio: concentration, sector
exposure, correlation to what is already held, and historical risk. It can read
a real paper account or take holdings on the command line, and it can describe
what a hypothetical addition would do to the shape of the portfolio.

## What it cannot do

**Portfolio Fit is read-only. It cannot place or cancel a trade.**

That is a structural property, not a policy. The analysis package declares a
read protocol with two members — list the slots, snapshot one slot — and there
is no member that could change anything. The vendor client capable of order
submission lives in `app.broker.paper_snapshots`, outside the package, and a
test parses `app/portfolio_fit/*.py` and asserts none of it imports `app.broker`
or `alpaca.trading`. A second test parses the adapter and asserts it names no
mutating vendor call at all.

It also expresses no recommendation. There is no buy, no sell, no target weight,
no expected return and no rotation, because no validated predictive evidence
supports any of them. A test asserts that vocabulary never appears.

## Reading a real account

```
tradabot portfolio-fit PAPER_3K
tradabot portfolio-fit PAPER_3K --candidate MSFT --amount 500
tradabot portfolio-fit PAPER_3K --candidate MSFT --amount 500 --json
```

Any `PAPER_*` name is routed to the broker reader, including one that does not
exist — `PAPER_50K` refuses by name rather than quietly describing an empty
portfolio that happens to look flat.

Three properties matter:

**Isolation.** Each slot uses its own credentials and its own client. There is
no fallback: an unconfigured slot returns `SLOT_NOT_CONFIGURED` rather than
borrowing another slot's keys, which is the one failure that would silently mix
two accounts' money. Snapshots carry a hashed account reference — enough to
prove two readings came from different accounts, never the account number.

**Capital, not buying power.** Two of the three paper accounts are margin
accounts offering four times equity. The experiment they belong to has never
used leverage, so usable capital is capped at equity and the difference is
reported as `leverage_withheld` rather than silently discarded.

**A flat account is a valid state.** It reports 100% cash with full confidence,
not "insufficient data" — nothing about an all-cash account is unknown.

Anything given inline is analysed without touching a broker at all:

```
tradabot portfolio-fit semis --cash 200 --holding NVDA:3 --holding AVGO:2 \
    --candidate AMD --amount 500
```

## Company context is borrowed, never recomputed

The report shows Advisor output — factual summary, valuation context, market
position, company-analysis confidence — beside the portfolio arithmetic, never
merged into it. "Is this a sound company?" and "does it fit this portfolio?" are
different questions, and a single blended verdict would hide when the answers
disagree.

Not one financial figure is derived in this layer. No trailing-twelve-month sum,
no margin, no valuation percentile. The Advisor owns all of it; a test asserts
those tokens never appear in `app/portfolio_fit/`, and that the package does not
import `app.advisor` — the protocol points inward and the Advisor satisfies it
from its own side.

If context cannot be produced — an unsynced fact store, a company with no
filings — the report shows `ADVISOR_CONTEXT_UNAVAILABLE` and every number is
still computed. Portfolio mathematics needs prices and weights, not fundamentals.

## Overlap states

Correlation bands are calibrated against the real distribution of equity pair
correlations, measured over 252 sessions across the clean universe — not tuned
against any example portfolio.

| state | rule |
|---|---|
| `NORMAL_OVERLAP` | ρ < 0.181 (below the 75th percentile) |
| `ELEVATED_OVERLAP` | 0.181 ≤ ρ < 0.296 |
| `HIGH_OVERLAP` | 0.296 ≤ ρ < 0.509 (90th to 99th percentile) |
| `EXTREME_OVERLAP` | ρ ≥ 0.509 (99th percentile) |

The previous 0.70 threshold sat *above* the 99th percentile of real equity
pairs, so it effectively never fired. Provenance is recorded in
`reports/phase12_34/overlap_rules.json`; the values are frozen and are not
retuned against examples.

## Cost

Everything except the broker snapshot is local. A fit calculation takes about
20 ms, company context about 130 ms per uncached symbol, a fact-store status
check about 300 ms. The broker snapshot is one network round trip, typically
one to three seconds, and is the only part that leaves the machine.
