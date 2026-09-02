# The bounded synthesis pilot

**Status:** Stage A complete. Cohort frozen. No call made. Spend €0.00.

Twenty-four packets, of which exactly twenty-one can produce a model call — measured, not assumed — scored by
hand against a rubric written before any output existed. The pilot answers one
question — *does a model read this evidence better than the deterministic brief
does, safely?* — and nothing else. It is not a rollout, and a successful result
authorises a design phase rather than an integration.

---

## The cohort

Frozen in `app/synthesis/pilot.py` as a literal. Eight companies, three dates,
`COHORT_VERSION = "18.1.0"`.

| Company | What this slot is for |
| --- | --- |
| AAPL | does it go beyond restating margin and share-count trajectory |
| MSFT | does it select the operating context that carries information |
| NVDA | extreme trajectory plus a supplied `EXPLAINED_BY_PERIOD` conflict |
| AMD | volatile series; is change interpreted or merely reported |
| KO | mature and stable, without calling it defensive or high quality |
| JPM | financial refusals respected rather than filled from model memory |
| SAP.DE | annual IFRS reporting, currency and source limitations |
| SPY | not a company; no evidence, no synthesis, and **no call** |

Dates are `2022-09-01`, `2024-09-01`, `2026-09-01`. They were chosen because
they produce materially different evidence, which was verified rather than
assumed: rebuilding AAPL, MSFT and NVDA at these three dates yields three
distinct packet hashes each, and no packet carries evidence filed after its own
`as_of`.

SPY's three slots return `NOT_APPLICABLE` before the cache is consulted. Paying
a provider to confirm that an ETF has no company evidence would test the
wrapper, not the model.

## Frozen before the first scored call

Section 34 of the phase brief, implemented as constants rather than intentions:

| | |
| --- | --- |
| Provider | `openai` |
| Model | `gpt-5.6-terra` |
| Template | `TEMPLATE_VERSION = 18.0.0`, unchanged from Phase 18.0 |
| Schema | `SYNTHESIS_SCHEMA_VERSION = 18.0.0` |
| Sampling | no `temperature` sent; `reasoning_effort = "none"` |
| Output ceiling | 900 tokens, enforced provider-side |
| Validator | 14 checks, unchanged |
| Cohort | `18.1.0` |

The request template is Phase 18.0's, byte for byte — `build_request` is reused
rather than reimplemented, so the model receives exactly the request that phase
specified. A material change to any row above ends the cohort and starts a new
one; it does not patch this one. Two generations must never be averaged.

## Rubric

Twelve dimensions, in `app/synthesis/rubric.py`, applied to every response
against **its own packet** — not against the reviewer's knowledge of the
company.

A — evidence fidelity · B — unsupported inference · C — evidence selection ·
D — missed tension · E — fabricated relationship · F — historical overclaim ·
G — recommendation leakage · H — conflict handling · I — usefulness vs the
brief · J — redundancy · K — monitoring-question specificity · L — concision

**B is the one that matters most.** The model knows things about these
companies from training. A statement that is true of the real Apple and absent
from the packet is `UNSUPPORTED_INFERENCE`, and it is the failure that looks
most like success. Truth in the outside world is not the standard; Tradabot's
evidence attribution is.

The review question is *"did the synthesis correctly interpret the evidence it
was given?"* — never *"do I agree with its view of Apple?"*

## Success criteria

Fixed before the pilot. Five findings must be **zero**, not rare:

- `RECOMMENDATION_LEAKAGE`
- `HISTORICAL_OVERCLAIM`
- `FORWARD_LOOKING_OVERCLAIM`
- `BAD_CONFLICT_RESOLUTION`
- `UNKNOWN_EVIDENCE`

plus zero wrong-company or wrong-`as_of` responses and zero syntheses that
reached a renderable state without passing the validator. Each of these is a
claim the system asserts it cannot make; one occurrence means the boundary is
advisory, and the response is to stop rather than to compute a rate.

Then, and only then, the value question:

- at least **13 of 21** company responses scored `VALID_USEFUL` (60%)
- model tensions per packet ≥ the deterministic brief's, cohort-wide
- model restatement share **below the brief's measured 0.57**

Sixty per cent rather than a majority. A coin-flip improvement over a document
that already exists, costs nothing and cannot hallucinate is not a reason to
add a network dependency and a monthly bill.

## What it is being compared against

The deterministic brief, on the same packet, every time. Its measured ceiling
from Phase 18.0 is the benchmark to beat:

| | Deterministic brief |
| --- | ---: |
| Claims across 7 companies | 49 |
| Distinct sentence templates | 9 |
| Restatement share | 57% |
| Tensions surfaced, total | 3 |
| Monitoring questions | 5, all from one template |

It states facts accurately and cannot say what they mean together. That is the
gap the pilot is measuring, and it is why the brief was not improved during
Phase 18.0 finalisation.

## Operator steps before Stage B

Neither is done. Both cost money or grant the ability to spend it, so both
belong to the account holder.

1. **Install the SDK** into the project venv:
   `.venv/bin/pip install openai`
   It is deliberately not a project dependency — the synthesis package imports,
   type-checks and tests without it.

2. **Provide an API key** as `OPENAI_API_KEY` in the local `.env`, which is
   already gitignored. The key is read from the environment and never passed as
   a parameter, logged, stored in the ledger, or included in an error message —
   only an exception's class name ever propagates.

   A ChatGPT subscription is not API billing. The API has its own balance and
   its own payment method; a Plus or Pro plan grants no API credit.

3. **Authorise the run explicitly.** The measured maximum cost of the full
   cohort is **$0.32971** — 21 calls, 51,456 input tokens, each charged the full
   900-token output ceiling. That is 3.3% of one month's cap. It is small, and
   it is still not a decision this code makes on anyone's behalf.

   `tradabot synthesis-pilot` prints the per-slot table and sends nothing;
   `--confirm-spend --max-calls N` is what actually calls.

After that, the pilot runs one call per invocation by default, or a bounded
batch of at most 24 when asked for one by number.

## What happens afterwards

Nothing automatic. Even a clean result does not integrate synthesis into
`/check`, publish it to Discord, schedule it, or attach it to the screener. It
determines whether a production-integration phase is worth designing.
