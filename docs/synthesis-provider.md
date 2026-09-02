# Provider selection for bounded research synthesis

**Status:** Stage A complete. No model has been called. Nothing has been spent.

Phase 18.0 defined what a synthesis may see and say. This document records
which model would produce one, why that one, what it costs, and what stops it
costing more than intended. Prices live in `app/synthesis/pricing.py` with the
date they were read; this file explains the choice, not the numbers.

---

## What was checked, and where

All figures read from official documentation on **2026-09-02**.

| Source | URL |
| --- | --- |
| OpenAI pricing | `https://developers.openai.com/api/docs/pricing` |
| OpenAI model pages | `https://developers.openai.com/api/docs/models/gpt-5.6-terra` |
| OpenAI structured outputs | `https://developers.openai.com/api/docs/guides/structured-outputs` |
| OpenAI data controls | `https://developers.openai.com/api/docs/guides/your-data` |
| OpenAI deprecations | `https://developers.openai.com/api/docs/deprecations` |
| Anthropic pricing | `https://platform.claude.com/docs/en/about-claude/pricing` |
| Google Gemini pricing | `https://ai.google.dev/gemini-api/docs/pricing` |

No blog posts, no aggregators, no recalled prices.

## Candidates

The task is small and unusual: ~2,100 input tokens, a 900-token ceiling, and a
requirement that the output parse into a fixed structure every time. Ranked by
the phase's stated criteria — strict structured output first, instruction
following second, cost sixth — three OpenAI tiers and two credible alternatives
were compared.

| Provider | Model | In $/Mtok | Out $/Mtok | Structured output | Context |
| --- | --- | ---: | ---: | --- | ---: |
| OpenAI | gpt-5.6-luna | 0.20 | 1.20 | strict, constrained decoding | 1.05M |
| **OpenAI** | **gpt-5.6-terra** | **2.00** | **12.00** | **strict, constrained decoding** | **1.05M** |
| OpenAI | gpt-5.6-sol | 4.00 | 20.00 | strict, constrained decoding | 1.05M |
| Anthropic | Claude Haiku 4.5 | 1.00 | 5.00 | via tool-use schema | 200K |
| Google | Gemini 3.5 Flash-Lite | 0.30 | 2.50 | response schema | — |

**OpenAI, because of the first criterion.** Its Structured Outputs with
`strict: true` is constrained decoding: the documentation states the model
"will always generate responses that adhere to your supplied JSON Schema". The
alternatives validate or coerce a schema; this one makes a violating token
unemittable. That difference is worth more here than anywhere else, because it
is what lets `evidence_ids` be typed as an enum of the identifiers in the
packet — turning a fabricated citation from something the validator catches
into something the decoder cannot produce.

**`gpt-5.6-terra`, and not the cheapest.** The phase brief's default is to
prefer a smaller model and let the experiment justify a larger one. The reason
for departing from it is that cost does not discriminate: across the whole
21-call cohort the three OpenAI tiers differ by **$0.36**, which is 3.6% of the
monthly cap. What does discriminate is the failure this experiment exists to
detect. The question being asked is whether *a model* can read bounded evidence
without importing what it already knows about Apple; a null result from the
cheapest tier would answer "luna cannot", which is not the question. Starting
at the mid tier and testing downward yields an answer at each step. Starting at
the bottom risks spending the cohort to learn nothing.

`gpt-5.6-terra` is not the largest model available — `gpt-5.6-sol`, `gpt-5.5`
and `gpt-5.5-pro` all sit above it, the last at fifteen times the input price.

## Properties that were checked rather than assumed

**Structured output.** Strict mode supports enums, `required`,
`additionalProperties: false`, `minLength`/`maxLength`, and `maxItems`. All are
used. Refusals arrive in a dedicated `refusal` field and are detectable rather
than parsed out of prose.

**Temperature is not sent.** The model's page enumerates its supported features
— streaming, structured outputs, function calling, prompt caching, web search —
and no sampling parameter appears among them. Sending an undocumented argument
to discover whether it returns a 400 is an experiment about the API, funded by
a budget meant for an experiment about synthesis. The determinism lever used
instead is `reasoning_effort`, which *is* documented for this model
(`none`, `low`, `medium`, `high`, `xhigh`, `max`).

This is the pilot's one adapter-level limitation, and it is stated rather than
worked around: **output is not guaranteed reproducible across identical
requests.** The cache makes it reproducible in practice — an identical packet
returns the stored synthesis rather than a new call — but two cold calls with
the same input may differ. Phase 18.1 records this; it does not solve it.

**Reasoning is off.** Reasoning tokens bill as output and count against
`max_completion_tokens`. At any other setting a 900-token ceiling bounds
deliberation plus answer, so a long think returns a truncated synthesis, and
the pre-call cost estimate stops being exact. With `reasoning_effort: "none"`,
the ceiling bounds the answer and the estimate is the true worst case.

**Retention.** API data is not used to train models by default. Abuse-monitoring
logs are retained 30 days. Zero Data Retention exists but requires prior
approval and is not needed here: a packet contains SEC filings and figures
derived from them, all of which are already public.

**Lifecycle.** No `gpt-5.6` model is deprecated or scheduled for retirement.
OpenAI's stated minimum notice for a generally available model is six months.

**Rate limits.** Tier 1 is 500 requests/minute and 500K tokens/minute — three
orders of magnitude above a manual pilot that defaults to one call per
invocation.

## Cost

At `gpt-5.6-terra`, $2.00 per million input tokens and $12.00 per million
output.

| | Input | Output | Total |
| --- | ---: | ---: | ---: |
| **A.** Representative call (2,100 in, ~600 out) | $0.00420 | $0.00720 | **$0.01140** |
| **B.** Worst measured call (3,200 in, 900 max out) | $0.00640 | $0.01080 | **$0.01720** |
| **C.** Pilot cohort, 21 calls at worst case | $0.10291 | $0.22680 | **$0.32971** |
| **D.** 10 calls/day × 30 days, worst case | $1.92 | $3.24 | **$5.16** |
| **E.** 100 calls/day × 30 days, worst case | $19.20 | $32.40 | **$51.60** |

Row E is included for awareness and does not describe anything this code can
do. It is five times the cap; the guard would refuse the 582nd worst-case call
of the month. Row C is the entire experiment, at **3.6% of one month's cap**.

Assumptions, stated rather than buried. Rows A, B, D and E use the worst
measured packet; row C is not an extrapolation but the sum of all 21 cohort
packets at their own measured sizes — 51,456 input tokens, from 1,474 for
SAP.DE in 2022 to 3,203 for NVIDIA in 2026 — each charged the full 900-token
output ceiling. Tokens are estimated at four characters per token. The
"expected output" of 600 tokens in row A is the deterministic brief's typical
size and is a guess, not a measurement; row B, which assumes the ceiling, is
what the budget actually uses.

`tradabot synthesis-pilot` prints this table from live packets and sends
nothing. Dry run is the default.

## Caps

**Currency is USD**, because that is what OpenAI publishes and bills in.

**$10.00 per calendar month, and that dollar figure is the guard.** Not a euro
cap converted at some rate — no safety property here depends on an exchange
rate. An earlier draft of this document argued the cap was "at most €10 at any
EUR/USD above parity", which is a forecast about a currency pair; this project
declines to make those about equities and should not smuggle one into a
spending limit. $10.00 was chosen conservatively against Phase 18.0's €10
design target and stands on its own.

No live rate is fetched: a budget that cannot be evaluated without a network
call has a failure mode where nothing can be spent at all, and one that
silently caches a rate is worse.

**21 of the 24 cohort slots can produce a call.** The three SPY slots carry
no evidence and are refused before the cache is consulted.

**One provider call per invocation, by default.** The pilot runner accepts a
bounded batch up to 24. There is no unbounded integer anywhere on the path.

**Estimates charge the ceiling.** A request is refused when its worst-case cost
exceeds what is left, not when its expected cost does.

**Failures are charged.** A response the validator rejected was still generated
and still billed. A timeout that returned no usage metadata is charged at the
estimate, because a month of timeouts is precisely the month in which a cap
that assumed the best would leak.

## What Stage A does not include

No API key. No SDK installed. No call made. No integration with `/check`, the
screener, Discord, or any scheduled job — and structural tests assert those
paths cannot be built without deleting them.

A ChatGPT subscription is not API billing. They are separate products with
separate balances; a Plus or Pro plan grants no API credit, and the API charges
per token against its own payment method.
