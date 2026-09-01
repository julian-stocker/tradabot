# Research synthesis — design

A contract for interpreting Tradabot's verified evidence without letting the
interpreter become the source of truth. **No model is implemented, no provider
is configured and no API is called.** This document describes what is built and
what is deliberately not.

## The shape

```
owning services → EvidencePacketBuilder → EvidencePacket
                                              ↓
                      (optional provider, not implemented)
                                              ↓
                                   candidate ResearchSynthesis
                                              ↓
                                     SynthesisValidator
                                              ↓
                              ValidatedResearchSynthesis → presentation
```

The deterministic `ResearchBriefBuilder` occupies the same slot as a provider
and returns the same type, so the system works with no model at all.

## Evidence, and what it is not

Nine evidence classes, kept apart: `PRIMARY_SOURCE_FACT`,
`CANONICAL_FINANCIAL_FACT`, `DERIVED_METRIC`, `HISTORICAL_TRAJECTORY`,
`PEER_CONTEXT`, `MARKET_CONTEXT`, `CURRENT_DEVELOPMENT`, `SOURCE_LIMITATION`,
`REFUSAL`.

There is no `INTERPRETATION` class. Interpretation is what a synthesis
*produces*; it carries a claim type and evidence references and lives in the
output, never in the packet. The packet is immutable and built from owning
services, so no model output can be written back as evidence.

## Absence carries a reason

`NOT_AVAILABLE`, `NOT_APPLICABLE`, `REFUSED`, `STALE`, `INSUFFICIENT_HISTORY`,
`SOURCE_LIMITATION`, `NO_CURRENT_EVENTS`, `NO_COVERAGE`,
`SECTOR_MODEL_REQUIRED`, `CURRENCY_BOUNDARY`, `NO_MARKET_DATA`. Never a bare
null: *"operating margin is missing"* and *"operating margin is not a
comparable quantity for a bank"* lead a reader to opposite conclusions.

## Contradictions are surfaced, never decided

An `EvidenceConflict` names two items, a type and a status. `UNRESOLVED` is a
first-class outcome, and a synthesis that cites both sides of one and states
which is correct is rejected. Precedence — primary filing evidence above
canonical facts above derived metrics above peer above market — explains why one
number is presented first. It never licenses discarding the other.

## Freshness is plural

Fundamentals, market, research ingestion, developments and peer context each
carry their own date. A single "last updated" would take its value from the
fastest and imply it of the rest.

## What a synthesis may say

Five claim types and no others: `FACT_SUMMARY`, `INTERPRETATION`, `TENSION`,
`UNCERTAINTY`, `MONITORING_QUESTION`. Temporal scope is `PAST` or `CURRENT`;
there is no `FUTURE`.

**Forbidden claims are absent from the schema rather than discouraged in a
prompt.** There is no `PREDICTION` claim type, no `price_target` field and no
free-text top level, so a recommendation has nowhere to be stored. Word-bounded
vocabulary and semantic-pattern gates are a backstop for one smuggled into prose.

Fact against interpretation:

| | |
|---|---|
| `FACT_SUMMARY` | "Operating margin rose from 21.4% to 29.3% over three years." |
| `INTERPRETATION` | "Profitability improved on this measure over that period." |
| rejected | "Profitability should keep improving." |
| rejected | "This is an attractive business." |
| rejected | "Margins historically expand after this kind of filing." |

Every research event carries `historical_evidence = NOT_ESTABLISHED`. A
synthesis may say a development *bears on* an observable condition; it may not
say what such developments have led to.

`MONITORING_QUESTION` asks about a company's own reported figures — *"whether
operating margin in the next filed quarter stays above the midpoint of its
recorded range"* — never a price level and never a trigger.

## Evidence links

Every claim must cite identifiers present in the packet it was given. A
`TENSION` needs two distinct references, because a tension asserts that two
things disagree and one citing a single item is an opinion wearing a structural
label. A cited identifier absent from the packet is rejected as invented.

## Confidence

`SynthesisConfidence` measures how complete the *evidence* was — coverage,
conflicts, staleness, refusals. It is not a probability about the shares, and
nothing maps it to a return.

## The instruction boundary

A request has three parts that never merge: a fixed `SYSTEM_CONTRACT`, the
evidence as structured JSON, and the task. Filing text is written by the party
being reported on and travels as the value of a `text` field. A document
containing *"ignore previous instructions"* arrives as a quoted string; a
synthesis that acted on it is rejected as `PROMPT_INJECTION_FOLLOWED`.

## Failure

Timeout, rate limit, outage, invalid JSON, schema violation, token overflow and
budget exhaustion all end the same way: no synthesis, and the deterministic
brief stands. Malformed output is never partially trusted or repaired.

## Reproducibility and caching

Every synthesis records packet hash and version, schema version, template
version, provider, model, temperature and response hash. A cache key is
`(company, listing, as_of, packet_hash, schema_version, model)` — so a changed
packet can never serve a stale synthesis.

## Boundaries

- A model never chooses screener candidates. Discovery stays deterministic; a
  synthesis interprets a company already selected.
- No portfolio advice, no position sizing, no portfolio action.
- No web browsing, no news feed, no new data source.
- No execution path of any kind.
