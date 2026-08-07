# lossrun

Extracts the loss-run claim table defined by `schema.json` into Excel. Calls
OpenAI and Google directly, falling back to the SuperApp Responses API for any
model whose vendor key is absent. Standalone — every backend is reached as an
API, with no dependency on the monorepo.

- [docs/APPROACH.md](docs/APPROACH.md) — the pipeline, chunking logic, column hints, and config contract
- [docs/PIPELINE.md](docs/PIPELINE.md) — the flow end to end, and where each decision came from
- [docs/CHALLENGES.md](docs/CHALLENGES.md) — known constraints and open questions

## Setup

```bash
git clone https://github.com/samyak-ib/munichre-large-doc-processing.git
cd munichre-large-doc-processing
uv sync
cp .env.example .env      # then fill in the keys below
```

| Credential | Serves | Where to get it |
| --- | --- | --- |
| `OPENAI_API_KEY` | the `openai/*` pins | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| `GEMINI_API_KEY` | the `gemini/*` pins | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `SUPERAPP_TOKEN` | the fallback route | a logged-in SuperApp session (see below) |

Each model calls its own vendor directly when that vendor's key is set, and falls
back to SuperApp when it is not — so `SUPERAPP_TOKEN` is needed only for models
no direct key covers. `lossrun check` prints the route it resolved.

The SuperApp token is an Auth0 JWT: DevTools → Network → any `/api` request →
`Authorization` header, minus the `Bearer ` prefix. It expires; on a 401 the CLI
asks for a fresh one. The vendor keys do not expire.

> Prefer the direct route. Through SuperApp every call runs a full agent loop, so
> token counts include context loading and tool scaffolding; `gemini/*` pins
> report no usage at all, which prices them at exactly zero. Each OpenAI pin also
> declares a Gemini fallback taken when the provider is unwired on the serving
> fleet, and the response echoes the pin that was requested either way — so a
> substitution is invisible and the run is costed at the wrong price.

## Use

```bash
uv run lossrun check                          # confirm credentials, and print the route
uv run lossrun schema                         # print the resolved 25-column contract
uv run lossrun extract samples/loss_run.pdf   # extract one document
uv run lossrun extract samples/a.pdf samples/b.pdf   # several — one batch, one report
uv run lossrun score out/a_2026*  --influence  # re-score offline, no API calls
uv run lossrun results out/a_2026* out/b_2026*  # write the shareable report
```

| File | Contents |
| --- | --- |
| `out/<route>_calls/<doc>_<timestamp>/extraction.xlsx` | `Final Table` · `Raw Rows` (pre-merge, per chunk) · `Issues` · `Accuracy` · `Accuracy Mismatches` · `Telemetry` |
| `out/<route>_calls/telemetry.xlsx` | Every run, call and score on that route, appended — for comparing models and costs |
| `initial_results/v1_<date>_<time>.xlsx` | One batch, shareable: `Summary` · `Accuracy By Column` · `Assumptions` · `Telemetry` |

Documents passed to a single `extract` share a **batch id**, and the results
workbook is rewritten after each one finishes — a long batch is readable while it
is still running.

`<route>` is `direct`, `superapp`, or `mixed` when the run spans both. Runs are
grouped this way because the routes do not measure the same thing: a SuperApp
total carries agent-loop tokens a direct call never pays for, so the two are not
comparable and a mixed run belongs with neither.

Each route keeps its own ledger, `<route>_calls/telemetry.xlsx`, so a folder is
self-contained and never mixes runs measured a different way. Every call row
carries the `provider` that served it. Comparing the routes means comparing two
ledgers.

### Options

| Flag | Effect |
| --- | --- |
| `--model MODEL` | Override the primary model |
| `--consensus A B` | Override the consensus pair |
| `--single-model` | Skip consensus; run the primary model only (roughly half the cost) |
| `--config PATH` | Alternate `config.yaml` |
| `--schema PATH` | Alternate `schema.json` |
| `--out DIR` | Output root (default `out/`); runs land in `<out>/<route>_calls/` |
| `--base-url URL` | Alternate API base |
| `--effort LEVEL` | Force one reasoning effort across every model |
| `--provider NAME` | Force one route — `auto`, `superapp`, `openai`, `gemini` — ignoring per-model overrides |
| `--no-adjudicate` | Keep the primary model's value on every conflict instead of asking a third opinion |
| `--results-out DIR` | Where the batch report goes (default `initial_results/`) |

## Models

Configured in `config.yaml`, not in code. The default pair is
`openai/gpt-5.6-luna` + `gemini/gemini-3.5-flash-lite`, one per vendor and each
called directly — cross-provider so the two do not correlate their errors.
Flash-Lite is reachable only on the direct route; SuperApp rejects it with
`400 Unsupported model`. For a GPT-only endpoint:

```bash
uv run lossrun extract doc.pdf --consensus openai/gpt-5.6-luna openai/gpt-5.6-terra
```

Adding a model means two config entries: a `models.overrides` page ceiling and a
`pricing` row. Nothing in the code branches on a model name.

> Prices in `config.yaml` are operator-maintained. The API returns token counts,
> not dollars, so a stale price yields a confidently wrong cost report. One price
> serves both routes for a pin, and the direct vendor rate is not what SuperApp
> bills — the `provider` column on the Telemetry sheet says which route each call
> took, so a mixed run can be re-priced by hand.

### Routing

`providers.default: auto` sends each pin to its own vendor where that vendor's
key is set, and to SuperApp where it is not. Two ways to override it:

```bash
uv run lossrun extract doc.pdf --provider superapp   # force one route for the run
```

```yaml
models:
  overrides:
    openai/gpt-5.6-luna:
      provider: superapp        # pin one model's route permanently
```

A provider named outright is honored or the run fails, naming the key it wants.
Only `auto` falls back — a silent downgrade from an explicit choice would bill
through the agent loop while config and telemetry claimed otherwise.

Request-body ceilings are set per provider, not per model, because the limit
belongs to the transport: SuperApp's endpoint returns `408 request body read
timeout` far below the 25 MiB it advertises, so that route splits windows at
1 MiB while a direct call uses the full 20 MiB inline ceiling. A model that falls
back therefore still gets the tighter window.

## Reading a result

Check these in order:

1. **Row count** — does `Final Table` match the claim count in the source? Dropped
   middle rows are the failure this pipeline exists to prevent.
2. **Accuracy** — cell accuracy and row recall per model, with every disagreeing
   cell listed in `Accuracy Mismatches`. This is the number that matters.
3. **Issues sheet** — `key_not_found` means a claim number or claimant name does
   not appear verbatim in the document text, which is the hallucination signature.
   `model_disagreement` lists every cell the two models read differently, and
   `adjudication_declined` marks the ones a third opinion would not settle —
   those are the cells worth a human's time.
4. **Telemetry** — cost and token counts for the run. A non-zero
   `calls_without_usage` means the cost is a lower bound.

## Tests

```bash
uv run pytest
```

Covers page windowing, JSON repair on truncated output, row mapping, merge and
conflict classification, cost arithmetic, and the full pipeline against a
synthetic loss run with the API stubbed out.
