# Loss-Run Extractor — Approach

| Field | Value |
| --- | --- |
| **Purpose** | Extract the loss-run claim table defined by `schema.json` into Excel |
| **Inputs** | `.pdf`, `.eml`, `.msg`, and the office/text attachments they carry |
| **Model access** | OpenAI and Google called directly with API keys; the SuperApp Responses API as the fallback for any model no vendor key covers |
| **Outputs** | `out/<route>_calls/<doc>_<timestamp>/extraction.xlsx` (final table + audit sheets), `out/<route>_calls/telemetry.xlsx` (that route's cumulative cost and accuracy), `initial_results/v1_<date>_<time>.xlsx` (a shareable batch report) |
| **Companion docs** | [PIPELINE.md](PIPELINE.md) · [CHALLENGES.md](CHALLENGES.md) |

## Bottom Line

- **One call per chunk, all 25 columns at once.** Extracting column groups in separate calls is what makes rows diverge; this removes the divergence at its source rather than repairing it afterwards.
- **Profile first, chunk only when needed.** Documents under the page ceiling go through in a single shot. Chunking engages for the long tail, with page overlap and a layout hand-off so continuation pages know what the columns are.
- **A compact output contract.** Column names are declared once and rows travel as positional arrays, which is what keeps long tables inside the output-token budget.
- **One extraction, then a QA review of it.** The table is read once and audited once, with the source pages re-attached for the audit. A correction reaches the table only when the document's text layer carries the value it proposes, so a reviewer that can invent a value cannot put one in the deliverable.
- **Each model calls its own vendor directly, with SuperApp as the fallback.** A direct call reaches the model it names and reports real token counts; through the agent loop neither holds. Routing is per model, so a missing key downgrades one pin rather than failing the run.
- **Accuracy is measured against golden data, and so are the rules used to measure it.** Every equivalence the scorer honours can be switched off, so each one carries a number rather than an assertion.
- **Cost is measured, not estimated.** Every API call lands in a telemetry ledger with tokens and dollars, so model choices can be compared on evidence.
- **Model-agnostic by construction.** Nothing branches on a model name outside two config blocks; adding a model is two config entries.

## The final table

`schema.json` (class `68813`) defines a `Table` UDF field joining `Key Fields`, `Policy Claim Details`, `Claim_Details_Basic`, `Claim Financial Details`, and the document-level `Valuation Date`. Deduplicated, that is **25 columns**, one row per claim:

| Group | Columns |
| --- | --- |
| Document-level (repeated on every row) | `Insured` |
| Section-level (extracted per row, backfilled from the document header) | `Valuation Date`, `Insurer Loss Run` |
| Policy | `Policy Number`, `Line of Business`, `Policy Effective Date`, `Policy Expiration Date`, `Policy Year`, `Policy Total` |
| Claim identity (the row key) | `Claim Number`, `Claimant Name` |
| Claim detail | `Description`, `Claim Status`, `Accident Date`, `Report Date`, `Closed Date`, `Loss State`, `Occurrence ID` |
| Financial | `Indemnity Paid`, `Indemnity Reserve`, `Expense Paid`, `Expense Reserved`, `Recovery Salvage Subro Reins`, `Recovery Deductible`, `Claim Total` |

Per-column extraction prompts are read from `schema.json` at runtime (`prompt_schema[].description`, 15.7 KiB in total) and never restated in code — the schema stays the single source of truth for extraction semantics.

On top of them, `prompts.COLUMN_HINTS` adds guidance for four columns whose failures scoring showed to be structural rather than transcription errors. It is kept separate from `schema.json` deliberately: that file is the client's class definition, and the hints dict is exactly the delta we recommend folding into it. See [Column hints](#column-hints).

`Claim Total` and `Policy Total` are extracted verbatim, never computed: the schema instructs returning `N/A` when the document does not state them. A computed-versus-extracted comparison is written to the Issues sheet as an audit signal only, and never overwrites an extracted value.

## Providers and routing

Three transports reach a model, chosen **per model** so one run can compare a pin called directly against the same pin through SuperApp.

| Route | Endpoint | Credential | Client |
| --- | --- | --- | --- |
| `openai` | `api.openai.com/v1` | `OPENAI_API_KEY` | `openai_client.py` |
| `gemini` | `generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` | `gemini_client.py` |
| `superapp` | the Responses API on `api.base_url` | `SUPERAPP_TOKEN` | `superapp_client.py` |

`providers.default: auto` sends each pin to the vendor named by its catalog prefix whenever that vendor's key is set, and to SuperApp when it is not. A provider named outright — in `models.overrides.<model>.provider` or `--provider` — is honored or the run fails naming the key it wants. Only `auto` falls back, because a silent downgrade from an explicit choice would bill through the agent loop while the config and the ledger both claimed otherwise.

Clients are built lazily, so a run needs only the credentials for the routes it actually takes: an all-direct run never asks for a bearer token.

**Why direct is preferred.** Three defects are properties of the SuperApp route, not of the models:

- Its calls are full SuperAgent runs, so token counts include context loading and tool scaffolding a raw call never pays for.
- `gemini/*` pins return complete answers with `input_tokens: 0, output_tokens: 0`, pricing them at exactly zero.
- Every OpenAI pin declares a Gemini fallback taken when the provider is unwired on the serving fleet, and the response echoes the requested pin either way — so a substitution is invisible and the run is costed at the wrong price.

Going direct also reaches pins SuperApp rejects: `gemini/gemini-3.5-flash-lite` exists in the routing registry but is not a selectable catalog pin, and returns `400 Unsupported model` through the agent loop.

### Constraints that shape the design

The `superapp` route is the constrained one, and the pipeline is built to its limits — which the direct routes then satisfy trivially. From `backend/api-server/internal/handlers/responses/`, documented in `docs/api-server/01.004-responses-api-integration-guide.md`.

| Constraint | Value on `superapp` | Consequence |
| --- | --- | --- |
| Background only | `background: true` required; `stream: true` rejected | Every call is create → poll → read. OpenAI accepts the same shape, so that client inherits it; Gemini is synchronous and has nothing to poll |
| Combined `input` + `instructions` | 64 KiB | Column specs fit; overlap context must be capped to row keys. Kept as a shared budget on every route, so a prompt that fits one fits all |
| Request body | 25 MiB documented | Chunk PDFs are size-checked after base64 and recursively halved. The effective ceiling is per route — see below |
| Inline attachments | 20 per request, base64 data URI only | One chunk PDF per call; no remote URLs, no Files API |
| Structured output | `text.format` ignored | JSON is requested in the prompt and parsed defensively |
| `reasoning.effort` | Accepted: `low`/`medium`/`high`, plus OpenAI-only `xhigh`/`max` | Set per stage and per model, and recorded per call. Effort is not what drives extraction quality here — see [CHALLENGES.md](CHALLENGES.md) #1 |
| `previous_response_id` | Rejects ancestors carrying attachments | A truncation resume is a fresh call, not a chained one |
| Create rate limit | 60/min per principal | Chunk concurrency is capped and shared across both models |
| Request body read timeout | Undocumented, far below 25 MiB | Concurrent uploads draw a `408`. Models are serialized and windows split at 1 MiB on this route — see [CHALLENGES.md](CHALLENGES.md) #17a |
| Run ceiling | 60-minute workflow timeout | Per-call poll deadline of 15 minutes, then retry the chunk |

**The request-body ceiling belongs to the transport, not the model.** It resolves in three layers — the `chunking` block, then the route's `providers.<name>.max_request_bytes`, then a `models.overrides` entry. Keying it per model instead would leave a model that falls back to SuperApp uploading bodies that endpoint times out on.

| Route | Ceiling | Why |
| --- | --- | --- |
| `superapp` | 1 MiB | The `408` workaround; a 0.9 MiB upload failed all five attempts |
| `openai`, `gemini` | 20 MiB | The vendor inline limit. Fewer, larger windows mean fewer calls, fewer seams, better row continuity across pages |

## Pipeline

### Stage 0 — Ingest (`ingest.py`)

Accepts a file or a directory and normalizes everything into a list of `SourceDoc {path, kind, pages, bytes, has_text_layer}`.

| Input | Handling |
| --- | --- |
| `.eml` | `email.parser.BytesParser`; attachments saved, body retained as context |
| `.msg` | `extract-msg` |
| `.pdf` | Passed through |
| `.xlsx`, `.csv`, `.docx`, `.txt` | Text extracted and sent inline rather than as a file part |
| Images | Sent as `input_image` |

Attachments are the claim source. The email body supplies document-level hints only — insured, valuation date — and never contributes rows unless there is no attachment at all.

### Stage 1 — Profile and layout discovery (`docprofile.py`)

**Local profile, no LLM:** page count, per-page text-layer presence, byte size, token estimate.

**Routing decision:** `single_shot` when the document fits inside the page ceiling and the payload size, otherwise `chunked`.

**One LLM call over the first `header_pages` (default 5)** produces `layout.json`:

- which of the 25 columns are present, and under what source header labels;
- whether Policy Number is a table column or a block-level parent value sitting above the rows;
- the date and currency formats in use;
- the document-level scalars `Insured`, `Valuation Date`, `Insurer Loss Run`.

This exists because continuation pages carry no header. Later chunks are handed this layout so they can map columns without ever seeing the top of the table.

No table detector and no form recognizer are used anywhere in the pipeline.

### Stage 2 — Extraction (`chunking.py`, `extract.py`, `jsonparse.py`)

**Chunking:**

- Page windows of `max_pages_per_chunk` (default 50) with `overlap_pages` (default 2), built with pymupdf `insert_pdf`.
- Any chunk exceeding the route's `max_request_bytes` after base64 is recursively halved — 20 MiB direct, 1 MiB through SuperApp.
- A `single_shot` document is one chunk covering every page — the chunking machinery never engages.

**Every chunk call carries:**

- the 25 column specs from `schema.json`;
- `layout.json`;
- its position: "chunk *i* of *n*, pages *a*–*b*; the table continues from the previous chunk";
- the last K row keys from the previous chunk as overlap anchors, with an instruction not to re-emit them — the model resolves the seam;
- a verbatim clause: transcribe claim numbers and names character-for-character, never normalize, never correct an apparent typo.

**Output contract** — column names once, rows as positional arrays:

```json
{ "columns": ["Policy Number", "Claim Number", "…"],
  "rows": [["P-1", "C003", "…"], ["P-1", "C004", "…"]],
  "truncated": false,
  "last_row_key": ["P-1", "C004"] }
```

**Truncation:** when `truncated` is true, or the JSON ends mid-array, the chunk resumes through a **fresh call** that re-attaches the same pages and instructs the model to continue after `last_row_key`. Response chaining is unavailable because the chunk carries an attachment. Bounded by `max_resumes` (default 3).

**Retries:** bounded per chunk (`api.max_retries`, default 4 — large attachments draw `408` from the SuperApp endpoint often enough that two is not sufficient). Each create carries an `Idempotency-Key`.

> ⚠️ The key is generated per attempt rather than per logical call, so it cannot deduplicate: a create that landed but whose response was lost is retried as a new, separately billed run. The protection the header exists to give is not currently in effect on either Responses route. See [CHALLENGES.md](CHALLENGES.md) #21.

### Stage 3 — Merge (`merge.py`)

- Row key: normalized `(Policy Number, Claim Number, Claimant Name)`.
- Across chunks, the first non-empty value wins per cell; every disagreement is recorded rather than resolved silently.
- Normalization is deferred to `cleaning.py`, which runs after the merge and is the single owner of formatting: dates to `MM/DD/YYYY`, money to plain decimals, empty to `N/A`.
- Parent-child fill: where `layout.json` reports Policy Number as a block-level value, it propagates down to the claims beneath it.

### Stage 4 — QA review (`qa.py`)

Stages 1–3 run once, under `models.primary` — **`openai/gpt-5.6-luna` at `max` effort**, called directly. There is no second extraction pass.

What replaced it is a **review of the table that already exists**, on by default (`qa.enabled`, or `--no-qa`). One call per chunk carries:

- that chunk's pages, re-attached — the reviewer looks at the document, not at a transcript of it;
- the rows extracted from those pages, as the same positional arrays the extraction returned;
- every column's definition from `schema.json`, so "wrong column" is a finding and not just "wrong value";
- a statement of what `cleaning.py` already did, without which every rendered date and stripped currency symbol comes back as a false finding.

Rows are matched to chunks positionally, by walking the pre-merge rows in merge order. Keying that lookup instead would miss exactly the rows whose Policy Number was filled in from a block header after the merge. A chunk whose rows exceed the 64 KiB text budget is reviewed in parts rather than truncated, because a silently unreviewed row would read as a clean one.

The reviewer returns two things: cells it believes are wrong, each with the value it says the document prints, and claims it found on the page but not in the table.

**The guard.** Adjudication could not invent a value because it could only pick between two candidates. A reviewer has no such limit, so the constraint moves to the point of application: a proposed correction is written into the table **only when that value occurs in the document's own text layer**, whitespace and case ignored. Anything unconfirmed is logged as `qa_unverified` and the extracted value stands.

Deleting a value is held to the same bar. An early version exempted a correction to `N/A`, reasoning that removing a value cannot invent one — measurement over the five golden documents showed it was the only harm the stage did, so the exemption was removed. See [CHALLENGES.md](CHALLENGES.md) #22.

Applied values go through `cleaning.clean_value` before they are written, so a correction copied off the page as `$1,200.00` lands in the table as `1200` like every other amount.

Rows reported missing are **flagged, never added** — reconstructing a full 25-column row from a key is a re-extraction, which is the thing this stage is not.

Findings land in the Issues sheet: `qa_corrected` at info as an audit trail, `qa_unverified` and `qa_row_missing` at warning — those are the cells a human should read.

> ⚠️ The guard is only as good as the text layer. A scanned document has none, so on exactly the files where a second look at the page is worth most, every finding is reported and none is applied. Measured: 39 findings on `Loss Run_Report.pdf`, 0 applicable. See [CHALLENGES.md](CHALLENGES.md) #23.

### Stage 5 — Output (`report.py`)

`out/<route>_calls/<doc>_<timestamp>/extraction.xlsx`:

| Sheet | Contents |
| --- | --- |
| `Final Table` | The 25 columns, one row per claim |
| `Raw Rows` | Pre-merge rows tagged with model, chunk and page range |
| `Issues` | Conflicts, unresolved cells, duplicate or dropped keys, truncation events, total mismatches |
| `Accuracy` / `Accuracy Mismatches` | Per-model and per-column scores against golden, plus every disagreeing cell |
| `Telemetry` | This run's calls and costs |

**Output is grouped by route.** Runs land in `out/<route>_calls/`, where `<route>` is `direct`, `superapp`, or `mixed` when a run spans both. `route_class` derives it from every model's resolved provider, plus the reviewer's when QA is on, since that stage bills real calls and can be a different pin. A single SuperApp call makes the whole run `mixed`: its totals then carry agent-loop tokens a direct call never pays for, so it belongs with neither population rather than being filed under whichever route dominated.

`out/<route>_calls/telemetry.xlsx` accumulates that route's runs — `Runs`, `Calls`, `Accuracy`, `Accuracy By Column` — so model and configuration experiments are directly comparable within a route, and runs measured a different way stay out of the file. Every row carries a `batch_id` shared by all documents of one `extract` invocation, which is how one experiment is pulled back out of the pile; every call row carries the `provider` that served it. Comparing the routes themselves means comparing two ledgers.

A ledger written before a column existed is migrated in place on the next append: rows are re-keyed by their own header names, so a new column reads blank on old rows instead of shifting every later value one place left.

`initial_results/v<n>_<date>_<time>.xlsx` is the shareable form of a single batch: `Summary`, `Accuracy By Column`, `Assumptions` and `Telemetry`. See [Scoring, and what it assumes](#scoring-and-what-it-assumes).

## Column hints

Four columns failed for reasons no amount of transcription care would fix. `prompts.COLUMN_HINTS` names the document shapes that explain them; the rules were written after reading the source PDFs and confirming each against golden data.

| Column | What the document actually does | What the hint says |
| --- | --- | --- |
| `Policy Total` | A policy aggregate printed either in a `LOSS RUN SUMMARY` table at the front, or as a `TOTAL` subtotal row closing each policy block. It is never on the claim row. | Find that figure and repeat it on every claim row of that policy, joining on policy number or on line of business plus effective date. Never sum the claim rows. |
| `Occurrence ID` | Often no such column exists. Claim numbers carry a sequence suffix — `C00320453-02`, `-03` — and claims sharing a prefix are one occurrence. | Derive the occurrence by dropping the suffix, but only where that pattern is consistent down the column. |
| `Valuation Date` | Labelled `Report as of`, `Current As of Date` or `Losses as of` — and a `Run Date` usually sits nearby as a decoy. A bundle of loss runs carries one as-of date per carrier section. | Read the section's date, ignore the print date, and emit exactly one date per row. |
| `Description` | Documents carry both a coded cause (`STRAIN OR INJURY BY TWISTING`) and a free-text narrative (`WHILE WORKING ON HIS KNEES...`). Golden holds the coded cause. | Take the coded cause when both are present; fall back to the narrative only when there is no cause column. |

> ⚠️ The `Description` hint contradicts `schema.json`, which lists `Accident Description` among the allowed sources and so points at the narrative. Golden is unambiguous across all 51 rows of the document that carries both, so the schema text is what needs correcting. This is recorded as a recommended change to the class definition, not applied silently.

## Scoring, and what it assumes

`accuracy.py` scores each model separately against `goldens/Loss Runs GTs.xlsx`, matching rows on claim number. Cell comparison is type-aware: money within a cent, dates parsed before comparison, and a set of equivalences that exist because golden records values as-is.

Each equivalence is a judgement about the golden data rather than about the extraction, so each is a flag on `ScoringPolicy` and can be switched off:

| Flag | What it treats as equal |
| --- | --- |
| `ignore_golden_blank` | A cell golden leaves empty is counted and reported, never scored |
| `money_empty_is_zero` | `N/A` and `0.00` in a money column |
| `leading_zero_tolerant_ids` | `40512146091` and `040512146091` — Excel coerced golden's numeric ids |
| `status_synonyms` | `CLOSED`, `C`, `Settled / Closed` |
| `state_synonyms` | `TX` and `Texas` |
| `semantic_description` | Descriptions stating the same thing: identical after normalisation, one a prefix of the other (golden clips at 50 characters), or 0.80 similar. A narrative and a taxonomy code are **not** equal |

`lossrun score --influence` measures what each is worth by scoring the same rows again with that one flag off. Two of them change which rows match at all, so the row count travels with every delta — dropping hard rows out of the match raises cell accuracy on the easy ones that remain, and a delta read without the row count reads backwards.

## Telemetry and cost

One record per API call: `run_id`, document, stage, model, provider, chunk pages, attempt, `response_id`, status, queue and completion latency, poll count, input tokens, output tokens, cost in, cost out, cost total, retry count, error code.

Cost is computed locally from the `pricing` block in `config.yaml`. The API returns token counts, not dollars, so prices are operator-maintained configuration. `provider` records which route served the call: one pin is reachable through more than one, and the direct vendor rate is not what SuperApp bills, so a mixed run's totals are re-priceable from the ledger.

Run-level summary: total cost, total tokens, wall clock, pages, rows extracted, corrections the review proposed, and how many of them the document confirmed.

## Configuration

```yaml
api:
  base_url: https://<host>/api/v1      # the superapp route; SUPERAPP_TOKEN from .env
  poll_interval_s: 3
  poll_deadline_s: 900
  max_concurrent_calls: 1              # 1 because concurrent uploads draw a 408
  max_retries: 4

providers:
  default: auto                        # each pin to its own vendor where its key is
                                       # set, superapp where it is not
  superapp: { max_request_bytes: 1048576 }    # this endpoint's 408 workaround
  openai:   { max_request_bytes: 20971520 }
  gemini:   { max_request_bytes: 20971520, temperature: 0.0 }

models:
  primary:   openai/gpt-5.6-luna         # "Luna" — the one extraction pass
  overrides:
    openai/gpt-5.6-luna:           { max_pages_per_chunk: 50, effort: max }   # "Luna Max"
    openai/gpt-5.6-terra:          { max_pages_per_chunk: 50 }
    gemini/gemini-3.5-flash-lite:  { max_pages_per_chunk: 100, provider: gemini }

qa:
  enabled: true                # review the extracted table against the pages
  model: ""                    # empty means the extracting model reviews

chunking:
  max_pages_per_chunk: 50      # GPT-5.4 ceiling; raised per model above
  overlap_pages: 2
  header_pages: 5
  max_request_bytes: 20971520  # baseline; each provider above overrides it
  max_resumes: 3

pricing:                             # USD per 1M tokens — operator-maintained
  openai/gpt-5.6-luna:           { input: 0.20, output: 0.80 }
  openai/gpt-5.6-terra:          { input: 1.25, output: 5.00 }
  gemini/gemini-3.5-flash-lite:  { input: 0.30, output: 2.50 }
```

Every value is overridable by CLI flag. Nothing branches on a model name outside `models.overrides` and `pricing`.

`pricing` is keyed by model pin, so one price serves that pin on every route — and the direct vendor rate is not what SuperApp bills. A run that mixes routes is therefore priced correctly for at most one of them; the `provider` column on each call is what makes it re-priceable. See [CHALLENGES.md](CHALLENGES.md) #14.

### CLI overrides worth knowing

| Flag | Effect |
| --- | --- |
| `--provider {auto,superapp,openai,gemini}` | Forces one route across every model, ignoring `models.overrides`. This is how a direct-versus-SuperApp A/B on the same document is run |
| `--effort LEVEL` | Forces one reasoning effort across every model, outranking per-model overrides |
| `--model`, `--qa-model` | Replace the extracting model, or the one that reviews it |
| `--no-qa` | Ship the extracted table unreviewed |
| `--out DIR` | Output root; runs land in `<out>/<route>_calls/` |

## Module layout

| File | Role |
| --- | --- |
| `lossrun/config.py` | `config.yaml` plus the credentials from `.env`, and the per-model route resolution |
| `lossrun/router.py` | Dispatches each model to its provider's client, built lazily and cached. The pipeline holds one object and never learns which provider served a call |
| `lossrun/superapp_client.py` | httpx client: create (always background, always `Idempotency-Key`), poll with backoff, honor `Retry-After`, surface `error.code`. Asserts the 64 KiB / 25 MiB / 20-attachment limits before sending |
| `lossrun/openai_client.py` | The same Responses cycle against `api.openai.com`, inherited from the SuperApp client — only the base URL, the bearer credential and the wire model id differ |
| `lossrun/gemini_client.py` | Direct `generateContent` against Google's API. Synchronous, so nothing to poll, and it can set `temperature: 0` where the Responses subset cannot |
| `lossrun/pipeline.py` | Orchestrates one document end to end: ingest → profile → extract → merge → qa → verify → score → write. Owns the route grouping of the output |
| `lossrun/schema_loader.py` | Resolves the `Table` UDF join in `schema.json` into the 25-column contract and its prompts |
| `lossrun/prompts.py` | Prompt assembly, and `COLUMN_HINTS` — the four-column delta recommended for `schema.json` |
| `lossrun/ingest.py` | eml/msg/pdf/office normalization |
| `lossrun/docprofile.py` | Local profile (pages, text layer, size) and the one-call layout discovery |
| `lossrun/chunking.py` | Page windowing with overlap, and the recursive halving that keeps a body under the route's ceiling |
| `lossrun/extract.py` | Per-chunk extraction and truncation resume |
| `lossrun/jsonparse.py` | Defensive parsing and repair of truncated JSON, since `text.format` is ignored |
| `lossrun/merge.py` | Row-key merge and normalization |
| `lossrun/verify.py` | Checks each row key appears verbatim in the text layer — the hallucination signature |
| `lossrun/qa.py` | Reviews the extracted table against the pages it came from, and applies only the corrections the text layer confirms |
| `lossrun/cleaning.py` | Renders dates and money into golden's representation, once, after the merge |
| `lossrun/accuracy.py` | Scoring against golden, and the `ScoringPolicy` flags that make each assumption measurable |
| `lossrun/rescore.py` | Rebuilds a finished run's tables from its own workbook, so scoring rules can change without re-running the API |
| `lossrun/results.py` | The shareable batch workbook written into `initial_results/` |
| `lossrun/telemetry.py` | Call ledger and cost model |
| `lossrun/report.py` | Excel writers and ledger reads |
| `lossrun/cli.py` | `extract`, `score`, `results`, `check`, `schema` |

## Authentication

One credential per route, all in `.env`, and a run needs only the ones its routes actually take — clients are built lazily, so an all-direct run never asks for a bearer token.

| Route | Credential | On rejection |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `401`/`403` exit naming the key; a region or access failure is not retryable |
| `gemini` | `GEMINI_API_KEY` | `401`/`403` exit naming the key |
| `superapp` | `SUPERAPP_TOKEN`, a pasted user JWT | `401` exits with a request for a fresh token |

No rejected credential is retried — retrying only spends the rate limit.

## Running it

```bash
cd "munichre MRSNA"

# Confirm credentials and print the route each model resolved to.
uv run lossrun check

# Extract. Several documents in one invocation share a batch id.
uv run lossrun extract samples/Chubb_Loss-1.pdf samples/Loss_2.pdf

# The same documents through the fallback route, for a like-for-like comparison.
uv run lossrun extract samples/Chubb_Loss-1.pdf --provider superapp

# Re-score finished runs offline — no API calls, so scoring rules are free to change.
uv run lossrun score out/direct_calls/Chubb_Loss-1_* --influence

# Write the shareable batch report.
uv run lossrun results out/direct_calls/Chubb_Loss-1_* out/direct_calls/Loss_2_* --out initial_results
```

> Background a long batch with `PYTHONUNBUFFERED=1`, or Python block-buffers stdout when it is redirected and the progress lines do not appear until the run ends.

Acceptance criteria for a run:

- **Completeness** — the `Final Table` row count equals the claim count in the source. Check this first on every sample.
- **Row-key fidelity** — every Claim Number and Claimant Name appears character-for-character in the PDF text layer. The check costs no API call; every miss lands in Issues.
- **No partial rows** — no row carries a Claim Number with an empty Claimant Name because a column group came back short.
- **Seam integrity** — on a chunked document, no duplicate row keys and no gap at a chunk boundary.
- **Cost** — the telemetry total for a 100-page document sits in the expected band. A wild number means the compact output contract is not being honored.
