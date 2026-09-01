# Research intelligence: source and architecture study (phase 14.0)

**`PRIMARY_SOURCE_LAYER_VIABLE`. `NEWS_LAYER_CONSTRAINED`. `INDUSTRY_LAYER_NOT_REALISTIC_AT_€0`.**

Research and architecture only. No production code was written, no provider was
subscribed to, no account created, no API key issued, and no LLM was called.
Every claim below that carries a number was measured against a live endpoint on
2026-09-01; the probes are recorded inline so they can be re-run.

The question this phase answers is not "can Tradabot summarise the news". It is
whether a **source-backed event layer** can be built for approximately €0/month
such that every narrative claim remains traceable to a document. That is a
question about sources, not about models, and it is answered by measuring what
the sources actually expose.

---

## 1. The target capability

A future `/check NVDA` would gain a section answering *what is currently
happening at this company, and how do we know*:

```
CURRENT DEVELOPMENTS
  CATALYSTS   new products · major contracts · capacity expansion ·
              strategic investment · capital allocation · guidance changes
  RISKS       litigation · tax disputes · regulatory action · export
              restrictions · restatement · customer concentration ·
              management departures · guidance cuts
  CONTEXT     competitor capacity · supply/demand · tariffs · FX · rates ·
              commodity exposure · geopolitical dependency
  THESIS      what currently supports the business case, and which observable
              development would weaken it
```

Constrained exactly as the rest of Tradabot is: no BUY/SELL/HOLD, no price
target, no return forecast, no claim without a citation. The existing
`test_advisor_safety` vocabulary ban extends to this layer unchanged.

The pipeline this implies is the phase brief's, and the ordering is the design:

```
company identity → verified event → primary-source evidence → event store →
deterministic materiality → [optional synthesis] → presentation
```

Not `LLM → internet → opinion`. The difference is that every arrow above is
independently testable, and the layer can be shipped and be useful with the
optional step permanently switched off.

---

## 2. Source discovery

### Tier 1 — primary sources

| Source | Documented | Machine-readable | Historical | Auth | Cost | Verdict |
|---|---|---|---|---|---|---|
| EDGAR `submissions/CIK*.json` | **Yes** — official API page | JSON | full, paged | none | €0 | **USE** |
| EDGAR filing `index.json` | Yes — Archives | JSON | full | none | €0 | **USE** |
| EDGAR `{acc}-index-headers.html` | Yes — Archives | SGML | full | none | €0 | **USE** |
| EDGAR document/exhibit files | Yes — Archives | HTML/XML | full | none | €0 | **USE** |
| EDGAR daily/full index | Yes — Archives | JSON/text | since 1993 | none | €0 | **USE** |
| `browse-edgar` Atom feed | Yes | Atom | recent | none | €0 | **USE** |
| EDGAR full-text search (`efts.sec.gov`) | **No** | JSON | since 2001 | none | €0 | **AVOID** — see below |
| Issuer IR / newsroom feeds | Per-issuer | RSS/Atom | varies | none | €0 | **PARTIAL** |

**EDGAR full-text search is not a documented API.** It returns HTTP 200 and
Elasticsearch-shaped JSON, and it is widely used, but SEC's official
[EDGAR Application Programming Interfaces](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
page documents only four endpoints — submissions, companyconcept, companyfacts,
frames — and does not mention `efts.sec.gov`. SEC's own
[EDGAR Full-Text Search FAQ](https://www.sec.gov/edgar/search/efts-faq.html)
describes the web UI and is silent on programmatic access, automation, and rate
limits. It is therefore an undocumented backing endpoint for a UI, and this
phase's constraint is explicit: no undocumented private APIs. **Nothing in the
proposed architecture needs it** — the documented submissions + index + exhibit
path supplies everything, so this costs us nothing.

**SEC access rules that do apply**, from the
[Webmaster FAQ](https://www.sec.gov/os/webmaster-faq#developers): *"our current
maximum access rate is 10 requests per second"*, and a declaring `User-Agent`
with a contact address is required — an undeclared automated tool is refused.
Tradabot's existing `app/fundamentals/client.py` already enforces a 0.11 s gap
and sets a `User-Agent`; the same client extends to this layer unchanged.

### Tier 2 — secondary

| Source | Coverage | Provenance fields | Cost at 52 symbols | Verdict |
|---|---|---|---|---|
| **Alpaca News API** (Benzinga) | US equities, ~130 articles/day, back to 2015 | id, headline, summary, **content**, author, source, url, symbols, created_at, updated_at | **€0** — included in the free market-data plan Tradabot already uses | **STRONG CANDIDATE** |
| Issuer newsroom RSS | per-issuer | title, link, pubDate | €0 | PARTIAL |
| Exchange announcement feeds | venue-specific | varies | €0 | UNEXPLORED |

### Rejected on provenance or licensing grounds

- **NewsAPI.org** — developer tier is non-commercial, delayed, and restricts
  historical access.
- **GDELT** — free and broad, but headline/metadata and tone scores rather than
  article text with a stable issuer identity.
- **Alpha Vantage / Marketstack / Twelve Data free tiers** — 25 req/day,
  100 req/month, and similar; below what 52 symbols need, before licensing.
- **Seeking Alpha** — named in the brief as the *analytical benchmark*, not as
  a data source. It is not approved, not consumed, and not reproduced.

No account was created and no provider was contacted for any of the above.

---

## 3. SEC full-text feasibility — measured

Probed against NVDA (CIK 1045810), 2026-09-01. `submissions/CIK*.json` returns
1,002 recent filings with **these per-filing fields**:

```
accessionNumber · filingDate · reportDate · acceptanceDateTime · act · form
fileNumber · filmNumber · items · core_type · size
isXBRL · isInlineXBRL · isXBRLNumeric · primaryDocument · primaryDocDescription
```

Three of those matter more than the rest:

- **`items`** carries **8-K item codes** directly. NVDA's recent history:
  `2.02` ×26, `5.02` ×16, `9.01` ×38, `8.01` ×10, `5.03` ×6, `5.07` ×6,
  `7.01` ×4, `1.01` ×3, `2.03` ×2, `1.02` ×1.
- **`acceptanceDateTime`** is the publication instant to the second
  (`2026-08-26T20:21:19.000Z`) — not the filing date.
- **`reportDate`** is the date of the *event* (`2026-08-26`), separate from
  `filingDate`. This is the `occurred_at` vs `published_at` distinction the
  event model needs, supplied by the source rather than inferred.

The exhibit layer resolves too. For NVDA's 8-K accession `0001045810-26-000073`:

- `index.json` lists all 17 documents in the filing.
- `{acc}-index-headers.html` carries the typed manifest and the human-readable
  **`ITEM INFORMATION: Results of Operations and Financial Condition`** /
  `Financial Statements and Exhibits`, plus `ACCEPTANCE-DATETIME`.
- `q2fy27pr.htm` (EX-99.1) returned 341,113 bytes → **25,575 characters of
  extractable text**, opening: *"NVIDIA Announces Financial Results for Second
  Quarter Fiscal 2027 · Revenue of $96.2 billion, up 106% from a year ago…"*

So the full earnings press release is retrievable, typed, timestamped, and
attributable — for €0, from a documented endpoint.

### What 8-K item codes establish as FACT without any NLP

| Item | Event established | Tradabot event kind |
|---|---|---|
| 1.01 / 1.02 | Material agreement entered / terminated | `MAJOR_CONTRACT` |
| 2.01 | Completion of acquisition or disposition | `M_AND_A` |
| 2.02 | Results of operations released | `EARNINGS_RELEASE` |
| 2.03 / 2.04 | Direct financial obligation created / accelerated | `DEBT_EVENT` |
| 2.05 / 2.06 | Exit/disposal costs; **material impairment** | `IMPAIRMENT` |
| 3.01 | Delisting / listing-rule failure | `REGULATORY_ACTION` |
| 4.01 / **4.02** | Auditor change; **non-reliance on prior financials** | `ACCOUNTING_RESTATEMENT` |
| 5.02 | Director/officer departure or election | `MANAGEMENT_CHANGE` |
| 5.03 | Fiscal-year or bylaw change | — |
| 7.01 / 8.01 | Reg FD disclosure / other events | needs extraction |
| 1.03 | Bankruptcy or receivership | `BANKRUPTCY` |

**That is a deterministic classifier with no model in it.** Item 4.02 is a
restatement because the SEC form says so, not because something read the prose.
The same applies to earnings, management change, M&A, debt and impairment. Only
7.01 and 8.01 — "other" — genuinely require semantic extraction.

### The asymmetry that decides the international question

Foreign private issuers file **6-K / 20-F / 40-F**, and those forms **carry no
item codes at all**. Measured:

| Issuer | 6-K/20-F/40-F filings in `recent` | With item codes |
|---|---|---|
| SAP | 396 | **0** |
| Novo Nordisk | 904 | **0** |
| Royal Bank of Canada | 22 | **0** |

**1,322 foreign filings, zero item codes.** So the deterministic layer covers US
domestic filers and stops dead at the border — the same boundary Phase 13 drew
for market data, arrived at independently. Everything international needs
semantic extraction from free text, which is precisely the part this phase is
least willing to trust.

---

## 4. Issuer IR feasibility

Probed seven publicly-advertised feed URLs on 2026-09-01:

| Symbol | URL | Status | Content-Type |
|---|---|---|---|
| NVDA | `nvidianews.nvidia.com/releases.xml` | **200** | text/xml |
| AAPL | `apple.com/newsroom/rss-feed.rss` | **200** | application/rss+xml |
| MSFT | `news.microsoft.com/feed/` | **200** | application/rss+xml |
| SAP | `news.sap.com/feed/` | **200** | application/rss+xml |
| CSCO | `investor.cisco.com/rss/news-releases.xml` | 404 | — |
| INTC | `intc.com/rss/news-releases.xml` | 404 | — |
| BOX | `boxinvestorrelations.com/rss/news-releases.xml` | 404 | — |

Four of seven resolved, **each on a different host with a different path**, and
the three failures were plausible URLs on the correct domains. IR feeds exist
widely — Q4 Inc, Notified, and similar vendors power many IR sites and expose
press-release/event/filing RSS — but **there is no discoverable uniform
pattern**, no registry mapping issuer → feed, and no guarantee the feed found is
the *investor-relations* feed rather than a corporate newsroom.

**Verdict: `PARTIALLY_SCALABLE`.** Practical only with a hand-maintained
per-company feed registry, in exactly the spirit of `app/instruments/seed.py` —
declared, verified, and refusing where unknown. Roughly 52 hand-verified entries
for the current watchlist; this does not scale to 1,000 symbols without an
IR-vendor directory that this phase did not find. **No scrapers were built and
none should be**: an HTML scraper per issuer is an unbounded maintenance
liability, and the moment a feed is absent the honest answer is that Tradabot
has no IR source for that issuer.

---

## 5. News provider study

The finding that changes the arithmetic: **Tradabot already holds credentials
for a news API it is not using.** Alpaca's News API is Benzinga-sourced, is
included with the market-data plan Tradabot is already on (free tier: 200
calls/min), reaches back to 2015, and returns per article:

```
id · headline · summary · content · author · source · url · symbols
created_at · updated_at · images
```

`content` is documented as *"Content of the news article (might contain
HTML)"* — full text, not a snippet — with `include_content` and
`exclude_contentless` query parameters. `source` names the originating
publication; `url` links the article; `symbols` supplies the ticker linkage;
`created_at`/`updated_at` supply publication and revision timestamps.

**That is every provenance field the event model requires**, at €0 incremental
cost, from a provider already integrated.

Two caveats, both material and neither resolved by this phase:

1. **Ticker ≠ identity.** `symbols` is a ticker array. Phase 13 established at
   length that a bare ticker is a query, not an identity. Any ingestion must
   route `symbols` through `InstrumentRegistry` and **refuse ambiguous
   mappings** rather than attach an article to a guessed company. An article
   tagged `DTE` must not become a Deutsche Telekom event.
2. **Licensing is unverified.** Alpaca's public docs describe availability and
   rate limits but do not state redistribution terms for Benzinga content. A
   personal, non-redistributing Discord bot is a different posture from
   republishing article text, and **the terms must be read before any
   ingestion** — this phase deliberately did not create an account or accept
   terms to find out. Storing `url` + `headline` + an extracted fact, rather
   than mirroring `content`, is the conservative design either way.

Coverage is US-centric (Benzinga), which compounds the international gap in §3.

---

## 6. Event data model

Provider-neutral, and deliberately separating the three timestamps and the three
confidences that a naive schema collapses.

```python
@dataclass(frozen=True, slots=True)
class ResearchEvent:
    event_id: str  # deterministic hash of (source_document_id, kind, subject)
    scope: EventScope  # COMPANY | LISTING  — see below
    company_id: int | None  # registry company. None only when unresolvable
    listing_id: int | None  # set only for listing-scoped events
    kind: EventKind
    occurred_at: str | None  # when the event happened. SEC reportDate. None if unknown
    published_at: str  # when the source made it public. SEC acceptanceDateTime
    fetched_at: str  # when Tradabot retrieved it
    source_url: str
    source_domain: str
    source_type: SourceType  # REGULATOR | ISSUER | EXCHANGE | NEWS_AGENCY | PUBLICATION
    source_quality: SourceTier  # PRIMARY | HIGH_SECONDARY | SECONDARY
    source_document_id: str  # SEC accession, or provider article id
    source_hash: str  # sha256 of the retrieved document
    title: str
    fact_summary: str  # what the source states. No interpretation
    evidence_excerpt: str  # verbatim span from the document
    evidence_offset: tuple[int, int] | None  # where in the document
    materiality: Materiality  # reuses app/monitoring/schemas.py
    source_confidence: Confidence
    extraction_confidence: Confidence
    interpretation: str | None = None  # never a fact. May be absent forever
    interpretation_confidence: Confidence | None = None
    historical_evidence: HistoricalEvidence = HistoricalEvidence.NOT_ESTABLISHED
    supersedes_event_id: str | None = None
    superseded_at: str | None = None
```

**Company vs listing scope** is the Phase 13 distinction carried forward, and it
is not cosmetic:

- *NVIDIA raises guidance* is **COMPANY** — it is true of the issuer, and every
  listing of that issuer inherits it.
- *NASDAQ halts trading in NVDA* is **LISTING** — it is true of one line on one
  venue and false of the Xetra line.

Collapsing them would let a halt on one venue render on another venue's card,
which is the same class of defect as the ADR price leak Phase 13.7 closed.

**Why `event_id` is a hash of the source document**, not a counter: the same
filing re-fetched must produce the same id, so re-ingestion is idempotent and a
dedup key exists before any storage decision is made.

---

## 7. Evidence model

Three output classes, never merged:

| Class | Definition | Permitted basis |
|---|---|---|
| **FACT** | Directly established by a cited source | The document says it. Excerpt + offset retained |
| **INTERPRETATION** | Tradabot's explanation of why the fact may matter | Deterministic rule, or (later) synthesis over facts. Always labelled |
| **HISTORICAL EVIDENCE** | A claim about what comparable events did | **Only** a Tradabot event-study dataset |

The third is the one that needs enforcing. *"Historically this is bullish"* is
forbidden unless Tradabot has measured comparable historical events — and
Tradabot's own research record is the reason to take that seriously.
`docs/filing-events.md` records `NO_EVENT_INFORMATION` for magnitude and
`NO_STABLE_DIRECTIONAL_INFORMATION` for direction on exactly this question:
post-filing windows lifted regardless of EPS direction, and the effect did not
survive pre-registration. So the default is:

```
Historical evidence: NOT_ESTABLISHED
```

and it stays that way until an event study says otherwise. `HistoricalEvidence`
is an enum with `NOT_ESTABLISHED` as the default member, not a nullable string,
so the absence is structural rather than a convention someone can forget.

**Three separate confidences**, because they fail independently:

- `source_confidence` — how much the *source* is trusted. An SEC filing is HIGH
  by construction; an unattributed aggregator is LOW.
- `extraction_confidence` — how sure we are the event was read correctly. An 8-K
  item code is HIGH (the form asserts it); a regex over prose is MEDIUM; an LLM
  reading free text is at best MEDIUM and must never be HIGH.
- `interpretation_confidence` — how sure we are the explanation is apt. Usually
  the weakest, and absent entirely when no interpretation is offered.

Collapsing these into one score would let a confident reading of a weak source
present as a strong finding, which is the specific failure the whole design is
built to avoid. This mirrors `weakest()` in `app/advisor/schemas.py`: a section
is only as good as its worst input, never the average.

---

## 8. Source hierarchy and conflict

```
PRIMARY          SEC · issuer IR · regulator · exchange
HIGH_SECONDARY   major financial news agencies with named attribution
SECONDARY        other publications
EXCLUDED         social media · anonymous claims · unsourced aggregators ·
                 AI-generated articles without primary evidence
```

**Conflict handling: preserve both, merge never.** If the company's 8-K says X
and a publication reports Y, Tradabot holds two events with two sources and
renders the disagreement:

```
Company (8-K, 26 Aug): [X]
Reported (Reuters, 27 Aug): [Y]
These sources disagree. Tradabot does not resolve which is correct.
```

Silently preferring the primary source would be defensible and is still wrong —
the reader loses the fact that a discrepancy exists, which is often the most
informative thing on the card. This is the same instinct as the ambiguity card:
refuse to choose, show the candidates, say why.

---

## 9. Freshness, dedup and corrections

**Freshness is per event kind**, because "current" means different things:

| Kind | Fresh | Rationale |
|---|---|---|
| `EARNINGS_RELEASE` | until next quarterly report | superseded by its successor, not by time |
| `GUIDANCE_RAISED` / `_CUT` | until next guidance event | a standing figure until revised |
| `MANAGEMENT_CHANGE` | 90 days | a departure stops being news; the vacancy may not |
| `M_AND_A` | until completion or termination | lifecycle, not decay |
| `LEGAL_ACTION` / `TAX_DISPUTE` | until resolution or 24 months | multi-year by nature |
| `MAJOR_CONTRACT` | 180 days | |
| `CAPACITY_EXPANSION` | until stated completion date | the announcement names its own horizon |
| `EXPORT_RESTRICTION` / `REGULATORY_ACTION` | until lifted or 12 months | |

A `/check` renders only events inside their window, and an expired event is
retained in the store but not presented as current. **A six-month-old event must
never render as "current" merely because it is still in the database** — the
same discipline as `app/notifications/trends.py`, where a persisting condition
is not news.

**Dedup** operates at three levels:

1. `source_hash` — the identical document re-fetched is the same event.
2. `event_id` — deterministic over `(source_document_id, kind, subject)`.
3. **Cross-source clustering** — the same real-world event reported by an 8-K,
   the issuer's IR feed, and three publications is **one event with five
   sources**, not five events. Clustering key: same company, same `kind`,
   `occurred_at` within a tolerance window. The primary source becomes the
   event's basis; the others become corroboration and are counted, not
   concatenated.

**Corrections and supersession** use `supersedes_event_id`. An amended filing
(`10-K/A`, `8-K/A`) supersedes its original; revised guidance supersedes prior
guidance. The superseded event is **retained** with `superseded_at` set — never
deleted — so the point-in-time discipline that governs the fact store governs
this store too. A question asked *as of* a past date must see what was known
then, not what was later corrected. This is the same invariant `FactStore._known`
enforces with `filed <= as_of`.

---

## 10. Materiality

Deterministic first pass, in the spirit of `app/monitoring/materiality.py`:
pre-declared thresholds, never fitted.

**Form-derived** (no computation): `MATERIAL_FORMS` already treats 10-K/20-F/40-F
as significant; extend with item codes — 4.02 (non-reliance) and 1.03
(bankruptcy) are CRITICAL by form alone.

**Magnitude-derived**, where a figure is extracted and the units are compatible:

> A $2B tax dispute should never render as "$2B". Rendered against canonical
> Tradabot figures it becomes *31% of TTM operating income*, which is the
> number that carries meaning.

Permitted denominators — TTM revenue, TTM operating income, TTM free cash flow,
cash, market capitalisation — **subject to the Phase 13.7 gate**. The
`MarketIdentity.unit_mismatch` rule applies unchanged: if the event amount is in
USD and the company reports in DKK, the ratio is refused, with the same stated
reason. A `$2B / revenue` ratio built across currencies is the Novo Nordisk
1.99× P/E defect in a new costume.

| Event kind | Deterministic materiality? |
|---|---|
| `EARNINGS_RELEASE` | **Yes** — surprise vs prior TTM, from stored facts |
| `GUIDANCE_RAISED` / `_CUT` | **Yes** — magnitude of revision |
| `TAX_DISPUTE`, `LEGAL_ACTION` | **Yes when an amount is stated** — ratio to canonical figures |
| `M_AND_A` | **Yes when consideration is stated** — ratio to market cap |
| `DEBT_EVENT` | **Yes** — ratio to existing total debt |
| `CAPACITY_EXPANSION` | **Partial** — capex figures yes, strategic weight no |
| `MANAGEMENT_CHANGE` | **No** — a CEO departure is qualitative. Role-based band only |
| `EXPORT_RESTRICTION`, `REGULATORY_ACTION` | **No** — qualitative |
| `INDUSTRY_SUPPLY_CHANGE` | **No** — qualitative |

**No LLM may compute or invent these ratios.** The numerator comes from the
document, the denominator from `FactStore`, the division from Python, and the
currency gate from `valuation_allowed`. A model that produced "roughly 30% of
operating income" from memory would be reproducing the exact class of error that
`app/advisor` exists to prevent.

---

## 11. Kioxia-style case study — how far €0 actually goes

The brief names a Kioxia-style thesis as the analytical benchmark. This is the
most important section, because it is where the €0 assumption is tested against
a real target rather than a convenient one.

**Measured on 2026-09-01, and decisive: Kioxia files no financial reports with
the SEC at all.** Its only EDGAR presence (CIK 2053383,
`KIOXIA HOLDINGS CORPORATION/ADR`, ticker KXHCF, OTC) is **6 filings, all ADR
depositary paperwork** — 4 × `F-6EF`, 2 × `F-6 POS` — with **no SIC code** and
**no 10-K, 10-Q, 20-F, 40-F, 6-K or 8-K**. Kioxia is listed in Tokyo; its
financials live with the TSE and its own IR site, neither of which is an SEC
endpoint.

Its peer set splits sharply:

| Company | CIK | Filings in `recent` | Financial forms |
|---|---|---|---|
| Micron | 723125 | 1,001 | 10-K, 10-Q, 8-K |
| Western Digital | 106040 | 1,008 | 10-K, 10-Q, 8-K |
| Sandisk | 2023554 | 217 | 10-K, 10-Q, 8-K |
| SK hynix | 2120882 | 31 | 6-K only (no item codes) |
| Samsung | — | — | **not an SEC registrant** |
| **Kioxia** | 2053383 | **6** | **none** |

Per fact type:

| Fact | Classification | Basis |
|---|---|---|
| Record quarterly profitability | **NOT REALISTIC AT €0** *(for Kioxia)* | No SEC financials exist. Would need TSE/IR ingestion in Japanese |
| — same fact for Micron / WDC | **AVAILABLE FROM PRIMARY SOURCE** | 8-K item 2.02 + EX-99.1 + XBRL |
| NAND ASP-driven growth | **REQUIRES SPECIALIZED INDUSTRY DATA** | ASP series are TrendForce/Yole/Omdia products. Not free |
| Large capacity investment | **AVAILABLE FROM PRIMARY SOURCE** *(US filers)* | 8-K 8.01/7.01 + capex in XBRL |
| Future fab timing | **AVAILABLE FROM SECONDARY SOURCE** | Announcements carry dates; not a structured field |
| Buyback | **AVAILABLE FROM PRIMARY SOURCE** | 8-K item 8.01 + share-count series already in the fact store |
| Long-term customer agreements | **AVAILABLE FROM PRIMARY SOURCE** | 8-K item 1.01 (material definitive agreement) |
| Enterprise SSD / AI demand | **REQUIRES SPECIALIZED INDUSTRY DATA** | Segment shipment data is a paid research product |
| Weak consumer demand | **REQUIRES SPECIALIZED INDUSTRY DATA** | Same |
| Competitor capacity decisions | **AVAILABLE FROM PRIMARY SOURCE** *(where the competitor is a US filer)* | Micron/WDC/Sandisk file it. Samsung and Kioxia do not |
| Chinese competitor expansion (YMTC) | **NOT REALISTIC AT €0** | Not an SEC registrant; no free structured source |
| Industry supply/demand forecasts | **NOT REALISTIC AT €0** | The defining product of paid industry research |

**The honest summary: roughly half of a Kioxia-style thesis is reachable at €0,
and it is the wrong half for that specific company.** The company-specific
financial and contractual facts are strong — *for US SEC registrants*. The
industry-structure facts that make such a thesis interesting (ASPs, supply/demand
balance, competitor capacity in Asia) are precisely what industry-research firms
sell, and there is no free substitute. And for Kioxia itself, the primary-source
layer produces literally nothing.

This is a genuinely useful negative result. It says a source-backed event layer
should be scoped to **US-listed SEC registrants** first, where it is strong, and
that promising Kioxia-grade industry analysis at €0 would be a promise Tradabot
cannot keep.

---

## 12. LLM role — architecture only, not integrated

No LLM was integrated, called, or configured. The boundary, for when the
question is asked:

**MAY** — summarise verified events into readable prose · explain relationships
between facts already established · organise dependencies into bull/base/bear
*structure* over verified facts · translate filing language into plain language ·
identify what further evidence would be needed.

**MUST NOT** — create or alter any financial figure · override canonical
Tradabot numbers · invent an event · invent or infer a source · conclude an event
occurred because it is plausible · make a historical price-effect claim without
an event study · output BUY/SELL/HOLD · output a price target · touch execution.

The enforcement is structural, not prompt-based, and follows the pattern
`app/advisor` already uses. An import-graph test asserts the synthesis package
cannot reach a broker; a vocabulary test extends `test_advisor_safety`'s banned
tokens; and — the load-bearing one — **the synthesis layer is handed a list of
`ResearchEvent` objects and cannot fetch anything**. It has no HTTP client, no
web search, and no fact-store handle. A claim it makes that does not map to an
event id in its input is, by construction, unsupported and rejected before
rendering.

That is the difference between the two architectures. `LLM → internet → opinion`
has no point at which a claim can be checked. `events → LLM → prose` has exactly
one, and it is mechanical.

---

## 13. Cost model

Anthropic list pricing per million tokens, as bundled in the `claude-api` skill,
cached **2026-06-24**: Haiku 4.5 $1.00 in / $5.00 out; Sonnet 5 $3.00 / $15.00;
Opus 5 $5.00 / $25.00. Prompt-cache reads ≈0.1× input; cache writes ≈1.25×
(5-minute TTL). Batch API ≈50% discount. No API key was created and no request
was billed; these are published rates used for arithmetic only.

Event volume is the input that matters, and it is small. From the measured NVDA
history: ~62 8-K filings and ~25 10-K/10-Q per 1,002 filings, i.e. roughly
**2–4 material filings per company per month**. Assume ~3 events/company/month
needing synthesis, ~4k input and ~600 output tokens each.

| Companies | Events/month | Arch. B or C, Haiku 4.5 | Arch. B or C, Sonnet 5 |
|---|---|---|---|
| 10 | 30 | ~$0.21 | ~$0.63 |
| 52 (current watchlist) | ~156 | ~$1.09 | ~$3.28 |
| 100 | 300 | ~$2.10 | ~$6.30 |

Halve again with the Batch API, since none of this is latency-sensitive.

**Architecture comparison:**

| | A: AI+search per `/check` | B: scheduled ingest, cache, AI on new events | C: deterministic first, AI only for ambiguous |
|---|---|---|---|
| Recurring cost | **Worst** — every invocation pays | Low — pay per event, once | **Best** — pay only for 7.01/8.01-class events |
| Latency | Worst — seconds of web+model inside a 3 s Discord window | Best — precomputed | Best |
| Hallucination risk | **Highest** — uncontrolled text, no citation guarantee | Bounded to verified events | **Lowest** — most events never touch a model |
| Duplicate processing | Worst — same event re-processed per user, per call | None | None |
| Reproducibility | **None** — same question, different answer | Good — cached per event | **Best** — deterministic path is exactly reproducible |

**C wins on every axis**, and the §3 measurement is why: 8-K item codes classify
the majority of material events with no model at all. The model is reserved for
the residue — 7.01/8.01 "other events", and foreign 6-K filings, which have no
item codes. Architecture A is not merely expensive; it is unreproducible, which
disqualifies it from a system whose entire value proposition is that its figures
are traceable.

The Discord constraint reinforces it independently. `bot.py` defers because the
interaction window is three seconds; architecture A would put a web search and a
model call inside a `/check`, and the audit already found that a cold analysis
does not reliably finish in three seconds without them.

---

## 14. Interaction with existing monitoring

`NEW_SEC_FILING` already exists and must not be confused with content
understanding: it detects *that* an accession appeared and its form type. It is
the correct trigger and the wrong abstraction for the answer. Research events
would be a **new kind space**, not additional members of `EventKind` — the
monitoring vocabulary is transitions in measured numbers; these are documented
occurrences. Merging them would put a `LEGAL_ACTION` next to a
`PORTFOLIO_WEIGHT_CHANGE` in one enum and one materiality table, which serves
neither.

| Proposed kind | Primary-source deterministic? | Basis |
|---|---|---|
| `EARNINGS_RELEASE` | **Yes** | 8-K item 2.02 |
| `MANAGEMENT_CHANGE` | **Yes** | 8-K item 5.02 |
| `M_AND_A` | **Yes** | 8-K item 2.01 (completion) / 1.01 |
| `MAJOR_CONTRACT` | **Yes** | 8-K item 1.01 |
| `DEBT_EVENT` | **Yes** | 8-K items 2.03 / 2.04 |
| `ACCOUNTING_RESTATEMENT` | **Yes** | 8-K item 4.02 |
| `REGULATORY_ACTION` | **Partial** | item 3.01 yes; enforcement generally not |
| `CAPITAL_ALLOCATION` | **Partial** | buybacks/dividends via 8.01 + share-count series |
| `GUIDANCE_RAISED` / `GUIDANCE_CUT` | **No** | Guidance is prose inside an EX-99.1. Extraction required |
| `CAPACITY_EXPANSION` | **No** | Usually 7.01/8.01 free text |
| `LEGAL_ACTION` | **No** | Item 103 in 10-K/10-Q prose, or 8.01 |
| `TAX_DISPUTE` | **No** | Prose, usually in notes |
| `EXPORT_RESTRICTION` | **No** | Prose, often 8.01 |
| `CUSTOMER_CONCENTRATION_CHANGE` | **No** | 10-K prose + segment data |
| `INDUSTRY_SUPPLY_CHANGE` | **No** | Not company-filed at all |

**Six deterministic, two partial, seven requiring extraction.** The deterministic
six are also the highest-frequency: item 2.02 alone accounted for 26 of NVDA's
recent 8-Ks. A layer shipping only those would already answer "what has this
company formally announced recently, with a link to the filing" — for €0, with
no model, and with every claim citable.

---

## 15. Peer Comparison vs Research Intelligence

Scored 0–5; higher is better on every axis (so "implementation risk" and
"misleading-output risk" score high when the risk is *low*).

| | Immediate `/check` value | Long-term differentiation | Dependency readiness | €0 feasibility | Data quality | Impl. risk (5=low) | Misleading risk (5=low) | Arch. leverage | **Σ** |
|---|---|---|---|---|---|---|---|---|---|
| **A. Peer Comparison** | 5 | 3 | **5** | **5** | **5** | 4 | 3 | 4 | **34** |
| **B. Research Intelligence** | 4 | **5** | 2 | 4 | 3 | 2 | 2 | **5** | **27** |

**Peer Comparison.** Every dependency is present and verified: 989 companies of
point-in-time fundamentals, SIC on 986 of 1,009, prices on 1,003 listings, a
currency gate, a financial-sector refusal, and — after Phase 13.7 — a report
object safe for a cross-sectional consumer to read. Zero new sources, zero
recurring cost, zero new failure modes. Its risk is bounded and known: SIC peer
groups are coarse (7372 groups SAP with Shopify and Microsoft), which is
manageable by showing the peer set and refusing small groups. Its ceiling is
also known — it is a strong feature, not a new product category.

**Research Intelligence.** Higher ceiling and higher leverage; it is the only
candidate that changes what kind of system Tradabot is. But dependency readiness
scores 2 because §3–§5 found the source layer is *viable and partial*: the
deterministic classifier stops at the US border (1,322 foreign filings, zero item
codes), IR feeds need a hand-built registry, the news provider's licensing is
unread, and §11 showed the industry layer is simply not purchasable at €0.
Misleading-output risk scores 2 because this is the first Tradabot layer whose
output is *narrative*, and narrative fails in ways a refused ratio does not.

**Peer Comparison wins on the arithmetic, and the arithmetic understates the
case.** Research Intelligence is not merely harder — it is a layer that should
be built on a system that already has a cross-sectional view, because *"this
company announced a $2B capacity expansion"* becomes a finding rather than a
fact only when Tradabot can also say how that compares to its peers'. The
sequence is Peer Comparison first, then Research Intelligence on top of it, and
that ordering is a design conclusion rather than a scheduling convenience.

---

## Recommended sequencing

1. **Phase 14.1 — Peer Comparison.** Cross-sectional valuation and quality over
   the existing fact store. €0, all dependencies verified present.
2. **Phase 15.x — Research Intelligence, primary-source only.** SEC 8-K item-code
   event extraction for US registrants, deterministic materiality against
   canonical figures, no news provider, no LLM. The six deterministic kinds.
3. **Later, and separately** — the news layer (after reading Alpaca/Benzinga
   terms), then synthesis over the event store, with the event store as its only
   input.
4. **Not planned** — industry supply/demand data. §11 establishes it is not
   available at €0, and pretending otherwise would misrepresent what Tradabot
   knows.
