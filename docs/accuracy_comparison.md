# Accuracy: SuperApp route vs. Direct/Consensus route vs. QA route

What the three ways this pipeline has been run actually scored against golden, using only runs that were scored and shipped. All numbers below are read from the workbooks in `initial_results/` and `qa_out/`, or quoted from [`CHALLENGES.md`](CHALLENGES.md) #22, which computed them the same way (`lossrun compare`/`lossrun score` against `goldens/Loss Runs GTs (1).xlsx`). Nothing here required a new API call.

## Terminology, since the three names refer to different axes

- **SuperApp** and **Direct** are *transports* — how a model is reached (`lossrun/superapp_client.py` vs a vendor's own endpoint). See [PIPELINE.md](PIPELINE.md) and [README.md](../README.md).
- **Consensus** was the *architecture* run over the direct route before it was retired: two models extracted independently and a primary-model adjudicator settled disagreements. Its accuracy numbers below are therefore really "direct route, consensus architecture."
- **QA** is the architecture that replaced consensus: one model extracts, the same model reviews its own table against the source page, and a verbatim-in-document check gates every correction. Also run over the direct route.

So "direct call (consensus)" and "QA" are the same *transport* (direct) at two different points in the pipeline's history; "SuperApp" is a different transport measured against the earlier, consensus-era pipeline. No single batch varies only one axis at a time — that limitation is called out below each table.

## 1. SuperApp route vs. Direct route, same 4 documents, same architecture (consensus-era, per-model)

Batch `1f25cc60`+`df818839` (SuperApp, `initial_results/v1_superapp_20260806_232458.xlsx`) reran the exact same 4 documents as batch `36c630dd` (Direct, `initial_results/v1_20260806_212311.xlsx`), so this is the closest thing in the data to a route-only A/B. Both scores below pool the two models each run used.

| | SuperApp | Direct |
| --- | --- | --- |
| Documents | Chubb_Loss-1, Loss-4, Loss_2, LRs_Application | same 4 |
| Models | `gpt-5.6-luna` + `gemini-3.6-flash` | `gpt-5.6-luna` + `gemini-3.5-flash-lite` |
| Row recall | 130/132 — **98.5%** | 128/132 — **97.0%** |
| Cell accuracy | 2632/2810 — **93.7%** | 2441/2766 — **88.3%** |

**Caveat: the Gemini pin differs between the two runs**, not just the transport. `gemini-3.5-flash-lite` is not a selectable SuperApp catalog pin (`400 Unsupported model`), so the SuperApp run substitutes `gemini-3.6-flash` — a materially different model. That confounds "route" with "model choice," so the gap above is not purely a transport effect.

### Isolating the route: Luna only, same model, same 4 documents

| | SuperApp (Luna) | Direct (Luna) |
| --- | --- | --- |
| Row recall | 65/66 — **98.5%** | 65/66 — **98.5%** |
| Cell accuracy | 1283/1405 — **91.3%** | 1278/1405 — **91.0%** |

With the model held fixed, the two routes land within **0.3 points of cell accuracy and identical row recall** of each other — a gap far inside the run-to-run noise this project has otherwise measured (`CHALLENGES.md` #1: identical settings produced 0% and 94.1% agreement across three runs). The routes are not distinguishable on this evidence; the SuperApp-vs-Direct difference in the first table is attributable to the model swap, not the transport.

## 2. Direct/Consensus vs. QA — the comparison the project actually measured for architecture (`CHALLENGES.md` #22)

Same 5 golden documents (92 claim rows), same primary model (`gpt-5.6-luna` at `max` effort), scored the same way. Batch `36c630dd` (consensus, shipped) vs. batch `3740ef65` (QA, shipped — `qa_out/qa_results/v1_20260807_160153.xlsx`).

| | Consensus (shipped) | QA route (shipped) |
| --- | --- | --- |
| Row recall | 89/92 — **96.7%** | 88/92 — **95.7%** |
| Cell accuracy | 1585/1897 — **83.55%** | 1570/1875 — **83.73%** |
| API calls | 421 | **15** (28× fewer) |
| Wall clock | 108.7 min | **39.7 min** (⅓) |
| Cost | $0.4267 | $0.3857 |

**Read this as no measurable accuracy change, not an improvement.** `LRs_Application` alone carries 60% of the scored cells and swung from 93.0% (consensus run) to 88.5% (QA run) at identical settings — a ~50-cell move from run-to-run extraction instability alone (`CHALLENGES.md` #1). The whole consensus-vs-QA accuracy gap is 6 cells, an order of magnitude smaller than that swing. What *is* real is the cost: 399 of the 421 consensus calls were per-cell adjudications, and dropping them is where the 28× and the ⅓ wall clock come from.

### What QA itself contributed, isolated from the cross-run confound

Rescoring one run's own pre-QA `Raw Rows` against its shipped, QA-reviewed table removes the run-to-run instability above — same extraction, same run, before and after the review pass:

| | Cells | Accuracy |
| --- | --- | --- |
| Extraction alone (pre-QA) | 1573/1875 | 83.89% |
| + QA as first built | 1570/1875 | 83.73% |
| + QA, deletions proved too | 1576/1875 | **84.05%** |

QA nets **+3 cells over unreviewed extraction** once unproven deletions are held to the same verbatim-in-document proof as replacements. Source: `CHALLENGES.md` #22.

## 3. Every scored batch, for context

Per-model, per-document accuracy exactly as written by each run's own results workbook (`Summary` sheet). Not adjusted for shared documents — see the tables above for like-for-like comparisons.

| Batch / file | Route | Models | Documents | Golden rows | Row recall | Cell accuracy |
| --- | --- | --- | --- | --- | --- | --- |
| `df818839` (`v1_20260806_195933.xlsx`) | superapp | Luna + `gemini-3.6-flash` | 3 | 108 | 100% | 95.1% |
| `1f25cc60` (`v1_20260806_201003.xlsx`) | superapp | Luna + `gemini-3.6-flash` | 1 (Loss_2) | 24 | 91.7% | 85.9% |
| `0c38cbb5` (`v1_20260806_204447.xlsx`) | direct | Luna + `gemini-3.6-flash` | 2 | 154 | 0%¹ | 0%¹ |
| **`1f25cc60`+`df818839`** (`v1_superapp_20260806_232458.xlsx`) | **superapp** | Luna + `gemini-3.6-flash` | 4 | 132 | **98.5%** | **93.7%** |
| **`36c630dd`** (`v1_20260806_212311.xlsx`) | **direct (consensus)** | Luna + `gemini-3.5-flash-lite` | 5 | 184² | **90.8%** | **83.4%** |
| **`3740ef65`** (`v1_20260807_160153.xlsx`) | **direct (QA)** | Luna | 5 | 92 | **95.7%** | **83.9%** |
| `5185622e` (`v1_20260807_202952.xlsx`) | direct (QA) | Luna | 6 (new, unrelated set)³ | 39 | 43.6% | 85.0% |

¹ Two documents for which the golden workbook had no matching entry at the time of this run — 0 rows resolved, not a real 0% score.
² Golden rows are doubled because the workbook reports the two models' scores separately and this column sums them; the underlying document set is the same 92 claim rows as the QA batch below.
³ A later run against 6 additional, non-golden-baseline documents (most have no or partial golden coverage — see the low row recall). Included for completeness, not part of the route comparison above.

## Bottom line

- **SuperApp vs. Direct, model held fixed:** indistinguishable (98.5% vs 98.5% row recall, 91.3% vs 91.0% cell accuracy) — within this project's own measured run-to-run noise.
- **Direct/Consensus vs. QA, architecture compared on the same 5 golden documents:** also indistinguishable on accuracy (96.7%→95.7% row recall, 83.55%→83.73% cell accuracy, a 6-cell difference against a ~50-cell run-to-run swing) — but QA is **28× fewer calls and a third of the wall clock** for that same accuracy.
- Isolating QA's own effect (same run, before/after review): **+3 cells (83.89% → 84.05%)** once unproven deletions were required to clear the same verbatim-in-document bar as replacements.

## Sources

- `initial_results/v1_20260806_195933.xlsx`, `v1_20260806_201003.xlsx`, `v1_20260806_204447.xlsx`, `v1_20260806_212311.xlsx`, `v1_superapp_20260806_232458.xlsx`
- `qa_out/qa_results/v1_20260807_160153.xlsx`, `qa_out/qa_results_new/v1_20260807_202952.xlsx`
- [`CHALLENGES.md`](CHALLENGES.md) #1, #20, #22
- [`PIPELINE.md`](PIPELINE.md), [`README.md`](../README.md) for route/architecture definitions
