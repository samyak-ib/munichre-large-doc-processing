# Pipeline Flow

How a loss-run document becomes a scored Excel table, and where each design
decision was recommended on the Aug 4 call (`MRe - scope of work to resolve
issues`). Timestamps are transcript positions.

Companion to [APPROACH.md](APPROACH.md) · [CHALLENGES.md](CHALLENGES.md)

## The flow

Both models (Luna Max and Gemini 3.6 Flash) run this flow independently and
concurrently. Chunks within a model run in order — each gets the previous
chunk's trailing row keys as seam anchors.

```text
 Input  (pdf / eml / msg / xlsx / docx)
                 |
                 v
        [0 Ingest]  ingest.py
                 |
                 v
   [1a Local profile]  docprofile.py   <-- no LLM, no cost
                 |
                 v
      Fits page + size budget?
            /            \
          yes             no
          /                \
 [single_shot]        [chunked]  chunking.py
  whole doc           50-page windows,
                      2-page overlap;
                      halve windows > 20 MiB
            \            /
             \          /
              v        v
   [1b Layout discovery]  first 5 pages -> layout.json
                 |
                 v
   [2 Extraction]  extract.py / prompts.py
                   all 24 row columns in ONE call per chunk
                 |
                 v
         Response usable?
        /    |      |     \
   empty  truncated  unparseable  ok
     |       |          |         |
     v       v          v         v
  retry   resume     save raw/   [3 Merge]  merge.py
  chunk   after                  key = policy+claim+claimant
            last_row_key              |
            (loop to 2)               v
                            [Post-process]  cleaning.py
                                      |
                                      v
                                [4 QA]  qa.py
                            one call per chunk: that chunk's
                            pages re-attached + the rows read
                            from them -> wrong cells, and the
                            value the document prints
                                      |
                                      v
                            Proposed value found in the
                            PDF text layer?
                               /            \
                             yes             no
                             /                \
                     applied to the        reported as
                     final table           qa_unverified;
                     (cleaned first)       extracted value stands
                             \                /
                              v              v
                               [Verify]  verify.py
                                      |
                                      v
                         [5 Score]  accuracy.py vs golden
                                      |
                +---------------------+---------------------+
                v                     v                     v
     out/<route>_calls/       out/<route>_calls/         initial_results/
       <doc>_<timestamp>/       telemetry.xlsx           v1_<date>_<time>.xlsx
       extraction.xlsx          Runs / Calls / Accuracy  Summary / Assumptions
       layout-*.json            Accuracy By Column       Telemetry
       raw/                     (keyed by batch_id)
```

`<route>` is `direct`, `superapp`, or `mixed`. Runs and their ledger are grouped
by route because the routes do not measure the same thing: a SuperApp total
carries agent-loop tokens a direct call never pays for.

| Stage | What it does |
| --- | --- |
| **0 Ingest** | Unwrap email; pick the source document |
| **1a Profile** | Pages, text-layer coverage, size — local only, no cost |
| **Budget fork** | Under budget → `single_shot`; else 50-page windows (halve any window over the route's ceiling — 20 MiB direct, 1 MiB through SuperApp) |
| **1b Layout** | One call on first 5 pages → column map, date/currency format, doc-level values |
| **2 Extract** | All 24 row columns in one call per chunk; verbatim clause; previous chunk's row keys as seam anchors; `COLUMN_HINTS` on top of the schema's own column specs |
| **Retry / resume** | Empty → retry; truncated → fresh call continuing after `last_row_key`; unparseable → `raw/` |
| **3 Merge** | Dedupe seams, fill gaps, record conflicts |
| **Post-process** | Dates → `MM/DD/YYYY`, money → decimal |
| **4 QA** | One call per chunk carrying that chunk's pages again plus the rows read from them. Reports wrong cells and what the document prints instead, and claims it found on the page but not in the table. On by default |
| **4b Apply** | A correction lands only if its value occurs in the PDF text layer; otherwise it is reported and the extracted value stands. Deleting a value is held to the same proof as replacing one. Missing rows are flagged, never added |
| **Verify** | Every key must appear verbatim in the PDF text layer |
| **5 Score** | Match to golden on claim number — per column and per model, under a `ScoringPolicy` whose every assumption can be switched off and measured |
| **Outputs** | `out/<route>_calls/<doc>_<timestamp>/`, that route's `telemetry.xlsx`, and a per-batch `initial_results/v<n>_<date>_<time>.xlsx` |

## Where each decision came from

| Time | Recommendation (Anant, unless noted) | Implemented as |
| --- | --- | --- |
| **00:16:28** | Form recognizer adds cost — *"additional cost form recognizer"* (Ashish/Samyak on the OCR checkbox, ~$0.50 per 100 pages) | No form recognizer anywhere. pymupdf only splits pages and supplies text for verification |
| **00:24:40** | Diagnosis: *"we are chunking the tables… every table field is a separate API call… we are getting different numbers of rows in every place"* (Samyak) | **Every column in one call per chunk** — the divergence is removed at its source |
| **00:36:40** | *"can we chunk the document and provide some type of layout to each of the chunks"* (Ashish) | `layout.json` from Stage 1b travels with every chunk |
| **00:44:37** | *"literally required divide and conquer"* | The `single_shot` / `chunked` split |
| **00:58:03** | *"Use two models… compare if both match. One is the Luna Max… and use the Gemini 3.5 flash light"* | Originally Stage 4 consensus. **Superseded:** the second extraction pass was dropped in favour of a QA review by Luna over the table the first pass produced — one call per chunk instead of a second full extraction, and every cell audited rather than only the contested ones. The consensus implementation is preserved on the `consensus-route` branch |
| **00:58:03** | *"you take a PDF metadata, you figure out what is the number of pages"* | Stage 1a local profile |
| **00:59:00** | *"I'm going to give you PDF in chunks… figure out some overlapping part to do the join"* | 50-page windows with 2-page overlap |
| **00:59:00** | *"the first run should figure out the metadata, what is the column — because when you go to next pages they will not have the metadata"* | Layout discovery over the first 5 pages |
| **00:59:00** | *"you are able to do the recursive append… PDF could be 10,000 pages, you just do it in 10 steps"* | Seam anchors + row-key merge |
| **00:59:53** | *"Step one is metadata generation… if it is small enough just pass the whole thing"* | `single_shot` is the default path |
| **00:59:53** | *"second pass is the value, and then last one is the reconciliation"* | Stages 2 and 3–4 |
| **00:59:53** | *"you should not use any of the Microsoft table detector — that will do more damage than good"* | No table detector, no layout model |
| **01:02:22** | *"combining these two models you're spending 24 bucks versus 249"* | Per-call cost in the telemetry ledger |
| **01:03:13** | *"Let the model do it — you literally just tell the model that hey this was broken, this is the overlap… exact overlap"* | Overlap anchors in the prompt; the deterministic key merge is only a backstop |
| **01:03:13** | *"85% of documents are less than 50 pages"* | Chunking engages only when the profile says it must |
| **00:58:03** | *"compare if both match"* — and what to do when they do not | Adjudication settles each conflicting cell against the document's own text rather than defaulting to the primary model |
| **01:04:15** | *"build an architecture as a backup just in case you get something much bigger than 400 pages"* | Recursive size-halving on top of page windows |
| **01:06:23** | *"maximum 500 to 600 lines of code… it's about whether the code is written correctly"* | Small modules, 132 tests on windowing, JSON repair, merge, scoring, cost |
| **01:15:27** | *"do you know the cost of this run — less than 1 cent"* | Cost reported per run; `usage_missing` marks a total as a lower bound |
| **00:52:12** | *"everybody should move to GPT 5.6 Luna Max"* | `models.overrides."openai/gpt-5.6-luna".effort: max` |

## Two recommendations that are implemented, and what measuring them showed

Both were once out of reach. Both now run as asked — and neither improved the result, which is worth saying plainly.

| Recommendation | Status |
| --- | --- |
| **Gemini 3.5 Flash-Lite** (00:58:03) — the cheapest model, and what the cost argument rested on | **Running.** Not a selectable SuperApp pin (`400 Unsupported model`), so it is called directly against Google. Measured across the five golden documents it trails Luna on row recall, 84.8% against 96.7%, at 81.8% of cells against 83.6%. At $0.30/$2.50 per 1M tokens against Luna's $0.20/$0.80 it is also the dearer pin — the cost premise does not hold |
| **Luna Max** (00:52:12) | **Running** at `max` effort, and it fixed a real column-mapping bug. But three runs at identical settings produced both the best and worst results in the set, so effort is not the variable that drives quality here |

## What the golden data has since shown

The call's premise was that hallucination was the core failure. Measured across
all 92 golden rows in six documents:

| Document | Pages | Golden | Luna Max | Gemini 3.6 Flash |
| --- | --- | --- | --- | --- |
| `Chubb_Loss-1.pdf` | 6 | 2 | 2/2 · **87.5%** | 2/2 · 79.2% |
| `LRs_Application…` | 10 | 51 | 51/51 · 67.8% | 51/51 · **72.9%** |
| `Loss-4.pdf` | 2 | 1 | 1/1 · 57.9% | 1/1 · **100%** |
| `Loss_2.pdf` | 15 | 12 | **11/12** · 63.9% | 10/12 · **72.4%** |
| `Loss Run_Report.pdf` | 38, scanned | 26 | **23/26** · 50.8% | 20/26 · **58.7%** |
| **Row recall overall** | | **92** | **95.7%** | 91.3% |

- **Row recall is 100% on every text-layer document**, and 95.7% across the whole set including the scanned one. The dropped-rows failure the project set out to fix is solved.
- **Luna Max leads on row recall; Gemini leads on cell accuracy.** Neither dominates — which is the argument for keeping both rather than picking one.
- **Zero keys fail the verbatim check** on text-layer documents. The genuine misreads (`EE87100` → `E8F7100`) appear only on the scanned document, where that check cannot run.
- The largest remaining gap is not hallucination but **six columns returning `N/A`** — `Description`, `Occurrence ID`, `Policy Total`, `Expense Reserved`, `Recovery Deductible`, `Report Date`. No row is yet 100% correct on any document, and these are why.
