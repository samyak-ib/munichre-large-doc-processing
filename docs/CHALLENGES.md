# Challenges and Things to Ponder

Known constraints, unanswered questions, and caveats for the loss-run extractor. Each entry states the constraint, what the script does about it, and the question left open — so anyone picking an item up does not have to re-derive it.

Companion to [APPROACH.md](APPROACH.md).

## Bottom Line

- **Reasoning effort is now settable, but it is not the variable that matters** — the same settings produced both the best and worst measured runs.
- **The pair recommended on the call now runs**, because calling Google directly bypasses the SuperApp catalog that rejects Flash-Lite. Measured, it trails Luna on row recall (84.8% against 96.7%) and costs more per token, so the cost argument behind it does not hold.
- **Luna is unstable on stacked two-line headers**, intermittently returning the claim adjuster as the claimant. Gemini is not.
- **The verbatim key check proves a value was copied from the document, not that it came from the right column.** The QA review is what is now asked to catch a misassigned value, because it is given the column definitions alongside the page.
- **The second extraction pass is gone.** Consensus measured agreement, not accuracy, and cost a full second read to produce it. A QA review of the single extraction audits every cell rather than only the contested ones, for one call per chunk. The consensus implementation is preserved on the `consensus-route` branch.
- **The largest remaining errors were structural, not transcription.** Three columns returned `N/A` because the value was somewhere the extraction pass never looked, and one read the wrong column entirely. Prompt guidance addresses all four — and that guidance was written from five documents, which bounds how far it generalises.
- **The scoring rules are themselves assumptions**, each measurable and each reported with what it is worth.
- **The QA review is accuracy-neutral and operationally much cheaper**: level with the consensus route on the five golden documents, at 15 API calls against 421 and a third of the wall clock. The accuracy difference is smaller than the run-to-run noise, so "level" is the claim, not "better".
- **The QA guard can only fix what the text layer confirms**, which stops it inventing a value and goes silent on exactly the scanned documents where a second look at the page is worth most.
- **The SuperApp endpoint times out on concurrent uploads**, well below its documented body cap. Working around it costs wall clock, and it is the reason request-size ceilings are set per provider rather than per model.
- **Cost figures carry two known error sources**: operator-maintained prices, and one price per pin serving two routes that do not charge the same.
- **The measurement route matters.** Reaching a model through SuperApp adds agent-loop tokens, reports no usage at all for Gemini, and can silently substitute a different model. Calling the vendor directly avoids all three, which is why it is the default and SuperApp is the fallback.
- **Deployability at Munich remains open** and is deliberately isolated to configuration.

## Platform constraints we work around

### 1. Reasoning effort is now controllable, and it is not the variable that matters

The Responses API now accepts `reasoning.effort` (`low` / `medium` / `high`, plus OpenAI-only `xhigh` / `max`), so the script sets it per stage and records it per call.

Three runs over the same 10-page loss run, same models:

| Run | `extract` effort | Rows agreed across models | Cell agreement | Luna's claimant column |
| --- | --- | --- | --- | --- |
| A | `default` (Luna runs HIGH) | 51 / 51 | 94.1% | correct |
| B | `low` | 9 / 51 | 80.0% | **adjuster** |
| C | `default` | 0 / 51 | 0.0% | **adjuster** |

Runs A and C used identical settings and produced the best and worst results in the set. **Effort is not the determining variable — run-to-run instability is.**

The instability is specific and reproducible in kind: the document stacks two labels per column, `Coverage Type / Claim Adjuster` above `Claimant Name / Claim Description`. Flattened into a text stream, the adjuster's name appears *before* the claimant's on every row. Luna sometimes takes the first human-looking name it meets and returns the adjuster — repeating one adjuster across several unrelated claims. Gemini got it right in all three runs.

Layout discovery is not the culprit: it correctly reported `Claimant Name -> "Claimant Name / Claim Description"` in every run, including the failing ones. The layout is right and extraction ignores it.

**Ponder:** the fix belongs in the extraction prompt, not in the effort setting — an explicit "do not confuse the claimant with the adjuster" clause derived from the stacked headers that layout discovery already detects. Until then, Luna is a shaky first reader on stacked-header layouts, which is most of why the QA stage exists.

### 1a. The verbatim key check cannot catch a misassigned value

Every one of those runs scored **0 keys failing the verbatim check**, including the ones where most claimant names were wrong. The adjuster's name is genuinely present in the document, one column over. The check proves a value was *copied* from the document, never that it came from the *right place*.

Cross-model disagreement is what caught it, and this is the case the QA route has to answer for now that the second extraction is gone.

The review is a better instrument for it than consensus was, in principle: it is handed the column's definition alongside the page, so "this cell holds the adjuster, not the claimant" is a finding it is explicitly asked for — where consensus could only report that two models differed and leave a biased judge to pick. The correction is also exactly the kind the guard admits, because the real claimant name is printed on the page and therefore in the text layer.

**It is unmeasured.** The claim above is an argument from design, not a number. Re-running the three `LRs_Application` runs with QA on and counting how many misassigned claimants come back as `qa_corrected` is the experiment that settles it, and it has not been run.

### 2. The exact model pair recommended on the call is now reachable, by going around SuperApp

Both gaps closed, for different reasons:

- **"Luna Max"** means Luna at maximum reasoning effort. `max` is accepted (OpenAI-only) and is the configured default for the primary model. The measured effect of *lowering* effort was negative; raising it remains untested against golden, so the setting is a recommendation carried out, not a validated one.
- **`gemini/gemini-3.5-flash-lite`** is the cheapest model in the routing registry and the one the cost argument on the call rested on. It is still not a selectable SuperApp catalog pin — requesting it there returns `400 Unsupported model`, since it exists internally as a fallback and grounding model rather than a user selection. Calling Google directly bypasses the catalog entirely and reaches it.

**What the script does:** defaults to `openai/gpt-5.6-luna` + `gemini/gemini-3.5-flash-lite`, and pins the latter to `provider: gemini` rather than leaving it on `auto`. The explicit pin matters: on `auto` a missing `GEMINI_API_KEY` would fall the model back to SuperApp, which cannot serve it, turning a missing credential into a confusing `400` instead of a message naming the key.

**Consequence:** the recommended pair is no longer a stand-in, but its accuracy is now measured. Across the five golden documents Flash-Lite scored 81.8% of cells and 84.8% row recall, against Luna's 83.6% and 96.7% — competitive on cells, materially worse at finding rows. At $0.30/$2.50 per 1M tokens against Luna's $0.20/$0.80 it is also not the cheaper pin, so the cost argument that motivated it does not survive contact with the published prices.

### 3. Same-family models make weak second readers

The routing registry groups `gpt-5.6-sol`, `-terra` and `-luna` under a single `gpt-5.6` family because they share base weights, and uses that grouping to reason about diversity. Two pins from one family correlate their errors, so a second reading by one of them is weaker evidence than its independence suggests.

This used to be an argument about which pair to extract with. It is now an argument about `qa.model` — see #8, which is the same constraint in its sharper form, since the default reviewer is not merely same-family but the same pin.

**What the script does:** leaves the choice in config. `qa.model` accepts any pin, and the routing already handles the mixed run a cross-vendor reviewer produces.

**Ponder:** if Munich's endpoint permits only OpenAI models, a same-family reviewer may be the only option — in which case the verbatim text-layer check carries proportionally more weight.

### 4. No structured output

`text.format` and `json_schema` are ignored by the shipped handler, so JSON conformance is a prompt request rather than a guarantee. Every response needs defensive parsing, fenced-block extraction, and repair of truncated arrays.

**Ponder:** the design doc describes a post-v1 "schema-enforced final turn" that would validate the answer server-side and repair-retry on mismatch. That would remove this entirely — worth tracking.

### 5. Truncation resume cannot use response chaining

`previous_response_id` rejects any ancestor carrying an attachment. Every chunk call carries a chunk PDF, so a cut-off chunk cannot be continued through a chained follow-up — the resume must be a fresh call that re-attaches the same pages, paying the input tokens a second time.

**Ponder:** cheaper resume paths. Smaller page windows reduce the chance of truncation but multiply fixed per-call overhead. A text-only resume — send the extracted page text rather than the PDF — would be far cheaper but loses the layout signal that makes the model good at tables in the first place.

### 6. The 64 KiB combined text budget bounds seam context

`input` plus `instructions` must stay under 64 KiB. The 25 column specs consume 15.7 KiB, leaving room for the layout and a bounded set of overlap anchors — row keys only. Richer seam context, such as the full previous rows, does not fit.

### 7. Create is rate-limited to 60 per minute per principal

Shared across the extraction and QA stages, which now issue roughly two calls per chunk between them. Batch runs across many documents need pacing; the telemetry ledger shows when the limit binds.

## Accuracy questions we cannot answer yet

### 8. A model reviewing its own work shares its blind spots

The QA pass defaults to the same pin that extracted the table. A systematic misreading — the same ambiguous column header read the same wrong way twice — is invisible to it, because the second look is taken by the reader that made the first one.

**What the script does:** nothing automatic. `qa.model` accepts any pin, so pointing the review at a different vendor is a one-line config change, and the routing already supports the mixed run that produces.

**Ponder:** this is the one property consensus had that the review does not, and it is measurable — run the same golden set with `qa.model` empty and then set to a cross-vendor pin, and compare corrected-cell counts.

### 9. Scanned pages defeat the verbatim key check

The text-layer comparison that catches hallucinated claim numbers needs a text layer. A scanned loss run has none, and those documents lose the strongest defense against the primary failure mode.

**This is not the edge case it was assumed to be.** Of the sample loss runs tested, `Loss Run_Report.pdf` reports **0% text-layer coverage** — every page is an image. On those documents the run reports `unverified_keys=0` because nothing was checked, not because nothing was wrong. Read that number together with the "no text layer" warning in the Issues sheet, never alone.

Scanned pages are also far more expensive: roughly 9k input tokens per page versus about 5k for a text-layer PDF of the same size.

**Ponder:** this sits in direct tension with the "no form recognizer, no table detector" guidance. A cheap OCR pass used *only for verification* — never for extraction, never shown to the extraction model — may thread that needle: it would not reintroduce the pixel-based table interpretation that guidance was aimed at.

### 10. The row key includes the least stable field

Claimant Name is simultaneously part of the merge key and the field most prone to autocorrection, and the schema legitimately returns `N/A` for it when no claimant column exists. A wrong or missing name silently splits one claim into two rows, or collapses two claims into one.

**Ponder:** a fuzzy secondary match on Claim Number alone, surfaced in Issues rather than applied automatically. Automatic fuzzy merging on a hallucination-prone key risks manufacturing rows that were never in the document.

### 11. Records split across a page boundary

Raised on the call as the reason chunking is risky: a claim whose row spans two pages loses context when the split lands mid-record. Two pages of overlap mitigates it, but a row split across a *chunk* seam remains the likeliest source of a duplicate or a dropped row.

**What the script does:** overlap plus explicit anchors, and seam integrity is an acceptance criterion — no duplicate keys, no gap at a boundary.

### 12. Page limits are advisory, not enforced

The 50-page default comes from the GPT-5.4 ceiling. SuperApp does not police page counts, and no error is returned for exceeding a model's practical limit — the failure appears as dropped rows.

**What the script does:** treats the row-count completeness check as the primary acceptance gate, precisely because the failure is silent.

### 17a. Concurrent uploads draw `408 request body read timeout`

The endpoint times out reading a request body far below its documented 25 MiB cap, and the trigger is **concurrency, not size**. Two models start their layout calls at the same instant and one of the two uploads fails; a 0.1 MiB document trips it on the first call of a run and recovers on retry, while a 0.9 MiB document failed all five attempts with both models running together. Running the same file with the models serialized succeeds.

Three settings work around it, all in `config.yaml`:

- `api.max_concurrent_calls: 1` — models run one after the other rather than together. This is the one that actually fixes it, and it doubles wall clock.
- `api.max_retries: 4` — covers the first-call timeout that recovers on its own.
- `chunking.max_request_bytes: 1048576` — splits page windows so no single body is large enough to be at risk.

**Ponder:** none of this is visible from the API contract, and the thresholds were inferred from failures rather than documented. Whether the timeout lives in the endpoint, an ingress proxy, or the local uplink is unresolved — and it decides whether these settings are permanent or a workaround for one evening's network.

### 18. The column hints were fitted to five documents

`prompts.COLUMN_HINTS` encodes document shapes read out of the samples in this repository: a `LOSS RUN SUMMARY` table at the front, a `TOTAL` subtotal closing a policy block, an occurrence derivable by dropping a claim number's sequence suffix, `Losses as of` rather than `Run Date`. Each was confirmed against golden data — on **five documents and 92 claim rows**.

That is enough to fix a failure and not enough to prove a rule. The occurrence-suffix rule in particular is a derivation, not a transcription: a document that prints a genuine occurrence column and *also* uses suffixed claim numbers would be read wrongly by a model that applies the rule too eagerly. The hint says to apply it only where the pattern is consistent down the column, which is a mitigation rather than a guarantee.

**Ponder:** the honest next step is a holdout — score a document whose golden data was never read while writing these hints, and report that number separately.

### 19. Golden and `schema.json` disagree about `Description`

`schema.json` lists `Accident Description` among the allowed sources for `Description`. In a document carrying both a coded cause column and a free-text narrative, that phrasing points at the narrative — and golden holds the coded cause, consistently, across all 51 rows. Following the schema literally scores near zero on that column.

The extraction hint now prefers the coded cause, and this is recorded in the results workbook's Assumptions sheet as a **recommended correction to the class definition** rather than applied silently.

**Ponder:** the same ambiguity may sit in other column descriptions and only show up when a document happens to carry both candidate columns. Reading the remaining specs against golden, rather than waiting for a score to expose them, is cheap.

### 20. Why the consensus route was retired (measured)

Kept because it is the evidence behind the current design, and because the implementation still exists on the `consensus-route` branch.

With `consensus.adjudicator` empty, the primary model adjudicated conflicts between itself and the second model. It was given the source text and could answer `neither`, so in principle it could concede.

Measured across five documents: **166 conflicts raised, 119 settled, 14 conceded to the second model (11.8% of those settled).** The distribution is what matters, because it is not uniform:

| Document | Conflicts | Settled | Conceded |
| --- | --- | --- | --- |
| `LRs_Application…` | 113 | 91 | **0** |
| `Loss_2.pdf` | 41 | 23 | 12 |
| `Loss Run_Report.pdf` | 11 | 4 | 2 |
| `Loss-4.pdf` | 1 | 1 | 0 |
| `Chubb_Loss-1.pdf` | 0 | — | — |

On `LRs_Application` it conceded nothing across 91 decisions — on the document where the second model scored **higher** (96.7% against 92.7%), so a large share of those tie-breaks kept the worse value. That single document also carried the cost: 113 extra calls, roughly twentyfold on that run. Elsewhere it concedes about half the time.

A model asked to choose between its own reading and a rival's is not a neutral judge, but "it never concedes" would be too strong — the behaviour is document-dependent.

**What was concluded:** the second extraction pass paid for a full re-read of every document to surface a set of cells that a biased judge then mostly waved through. The QA review keeps the part that was working — a model looking at the page again — and drops the part that was not.

## Cost and portability caveats

All three of the following are properties of the `superapp` route. Routing a pin to its vendor directly avoids each one, which is why `providers.default` is `auto`: a direct call is the measurement, and SuperApp is the fallback.

### 13a. Gemini through SuperApp reports no token usage at all

Confirmed on every run: `gemini/gemini-3.6-flash` returns a complete answer — full layout, full table — with `input_tokens: 0, output_tokens: 0`. Its cost therefore computes to exactly zero. The same model called directly reports real usage.

**What the script does:** every call carries a `usage_missing` flag, the run summary carries `calls_without_usage`, and the CLI prints `cost warning: N of M calls reported no token counts; $X is a lower bound`. A partial cost is never presented as a total.

**Consequence for model comparison:** a run that reaches Gemini through the fallback undercounts, and no like-for-like cost comparison is possible from that telemetry. A run with `GEMINI_API_KEY` set is comparable; `calls_without_usage: 0` is the check.

### 13. A SuperApp run can attribute cost to the wrong model

The `model` echoed on a Response is the pin that was requested, not necessarily what served the turn: each OpenAI pin declares a Gemini fallback, taken when the provider is unwired on the serving fleet. Cost is computed as configured price times returned tokens, so a silent substitution prices Gemini tokens at OpenAI rates or vice versa — and nothing in the response distinguishes the two cases.

A direct call has no such indirection: the vendor endpoint serves the model named in the path or it errors. The `provider` column on the Telemetry sheet says which route each call took, so the calls exposed to this are identifiable rather than merely suspected.

### 14. Prices are operator-maintained configuration

No route returns dollars, only token counts, and the repository carries only coarse relative cost tiers. The `pricing` block in `config.yaml` is hand-maintained, and stale numbers produce confidently wrong cost reports.

The table is keyed by model pin, so one price serves both routes for that pin — and the direct vendor rate is not what SuperApp bills. A run that mixes routes is therefore priced correctly for at most one of them. The `provider` column makes the affected calls identifiable, so such a run can be re-priced by hand.

### 15. SuperApp runs a full agent, not a bare model call

Each response on the `superapp` route is a complete SuperAgent run: context loading, tool scaffolding, agent-loop overhead. Token usage therefore exceeds what the same prompt would consume against a raw model endpoint.

**Consequence:** a cost measured on the fallback route is an upper bound on a direct-to-model implementation, not a prediction of it. A run with both vendor keys set has no agent-loop overhead in its counts, and is the figure transferable to what Munich would pay on their own endpoint.

### 16. Munich's endpoint may not expose the models this is validated on

Their Azure endpoint carries GPT-4o, GPT-5.4 and text-embedding, apparently behind middleware that blocks calls to other models. A solution proven on Luna plus Gemini Flash still has an open deployment question.

**What the script does:** every model-specific value lives in configuration — the pin, the page ceiling, the price. Re-validating on GPT-5.4 is a config change, not a rewrite. That is the main reason the design is model-agnostic rather than tuned to one model.

**Ponder:** the 50-page chunk default exists for exactly this scenario. If the answer is GPT-5.4, chunking stops being the last-10% path and becomes the common path — which raises the stakes on seam integrity considerably.

### 17. A completed response can carry empty output

A silent agent route returns `status: completed` with an empty `output_text`. Read naively, that is a document with zero claims.

**What the script does:** treats an empty output on an extraction call as a failed chunk and retries it, rather than recording zero rows.

### 21. The `Idempotency-Key` does not deduplicate a retried create

Both Responses clients send an `Idempotency-Key` on every create, but the key is generated inside the retry loop — a fresh UUID per attempt. Deduplication requires the key to be stable across attempts of the *same* logical call, so as written it cannot fire: a create that reached the server but whose response was lost to a timeout is retried as a new request and billed a second time.

The failure needs a lost response rather than a lost request, so it is not the common case — but `408 request body read timeout` on large uploads is exactly the shape of failure that produces it, and `max_retries` is 4.

**Fix:** hoist the UUID above the `for attempt` loop in `superapp_client.run`, so every attempt of one call carries one key. `OpenAIClient` inherits the same path, and OpenAI honours the header, so the fix covers both routes at once.

**Until then:** a run's cost is an upper bound whenever `attempt > 1` appears in the ledger — the `attempt` column on each call is how to spot it.

### 22. The QA route, measured against the consensus route it replaced

Batch `3740ef65`, 2026-08-07, five golden documents (92 claim rows), Luna at `max` effort extracting and reviewing. Compared against batch `36c630dd`, the last consensus run on the same documents.

| | Consensus (shipped) | QA route (shipped) |
| --- | --- | --- |
| Row recall | 89/92 — 96.7% | 88/92 — 95.7% |
| Cell accuracy | 1585/1897 — **83.55%** | 1570/1875 — **83.73%** |
| API calls | 421 | **15** |
| Wall clock | 108.7 min | **39.7 min** |
| Cost | $0.4267 | $0.3857 |

**The accuracy difference is inside the noise.** Extraction is unstable run to run (#1), and the instability is larger than the effect: `LRs_Application` carries 60% of the scored cells and returned 93.0% in the consensus run against 88.5% here, at identical settings. That single swing is ~50 cells; the whole QA effect is 6. Read the table as *no measurable accuracy change*, not as an improvement — one run per configuration cannot support more.

What is not inside the noise is the cost of getting there: **28× fewer calls and a third of the wall clock**, because 399 of the consensus run's calls were per-cell adjudications. On `LRs_Application` alone that was 276 calls and 81 minutes.

**What QA itself contributed** is measurable without the cross-run confound, by rescoring the same run's `Raw Rows` (which are pre-QA) against its shipped table:

| | Cells | Accuracy |
| --- | --- | --- |
| Extraction alone | 1573/1875 | 83.89% |
| + QA as first built | 1570/1875 | 83.73% |
| + QA, deletions proved too | 1576/1875 | **84.05%** |

The stage applied 14 corrections. Ten were deletions to `N/A`, admitted without proof by a carve-out that reasoned removing a value cannot invent one. On the scanned `Loss Run_Report.pdf` those were the *only* corrections applicable at all, and all six deleted a `Description` that golden agreed with — `TRIP AND FALL`, `SLIP AND FALL`, `AUTO DAMAGE`. The carve-out was removed; deletions are now held to the same proof as replacements.

The four proven replacements were good. Two fixed `Loss-4.pdf` outright (88.9% → 100%). Two more caught a real schema violation on `Loss_2.pdf`: `Valuation Date` had come back as `12/04/2020; 11/30/2020`, two dates joined into one cell, which the column spec explicitly forbids.

**Ponder:** the honest state is that QA is roughly free accuracy-wise and much cheaper operationally. To claim more, run each configuration three times and compare distributions — #1 says a single run cannot separate a 6-cell effect from a 50-cell swing.

### 23. The QA guard is only as good as the text layer

A correction the review proposes is written into the table only when that value occurs in the document's text layer. That is what keeps a reviewer that can invent a value from putting one in the deliverable, and it inherits #9's limitation exactly: a scanned loss run has no text layer, so **every finding on such a document is reported and none is applied**.

The stage still costs its calls there. What it buys is a warning list rather than a repaired table.

**What the script does:** logs the situation explicitly during the run and writes a `qa_unverifiable` row into the Issues sheet, so a clean-looking table is never mistaken for a reviewed one. Each individual finding is also reported as `qa_unverified`.

**Ponder:** the natural fix is a text layer — running OCR over the scanned pages purely to build the haystack the guard checks against, never to extract from. That is a local `pymupdf`/Tesseract step, not a form recognizer, so it does not reopen the cost objection from the call. Worth measuring against the alternative of trusting the review outright on scanned documents only.
