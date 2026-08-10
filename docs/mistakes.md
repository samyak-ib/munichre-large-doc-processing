# Mistakes found in the latest run

**Final state: 29 of 30 samples processed.** `LossRunsReport_9_7_2022.pdf` is
deliberately excluded (see Issue 4) — every other sample has a completed,
scored run.

Batches `156671bb` (documents 1–16), `a8777471`/`5019e81d`/`50620c0b` (the
rest, resumed and concurrent), `f42cd57e` (a targeted re-extraction of 2
documents under the merge fix, Issue 2), and `ade58f57` (`WCO Loss Runs
2018-2022.PDF`) against `goldens/Loss Runs GTs (1).xlsx`,
`openai/gpt-5.6-luna` at max effort, QA on.

Reproduce with:

```bash
dirs=(); for d in qa_out/direct_calls/*/; do [ -f "$d/extraction.xlsx" ] && dirs+=("$d"); done
uv run lossrun results "${dirs[@]}" --out qa_out --source-dir samples --version snapshot
```

(A few documents have more than one run directory — an old one from before
the merge fix and a newer one after, or repeated failed write attempts for
the excluded document. Use the newest timestamp; older ones are kept only as
before/after evidence in Issue 2.)

## Per-document

| Document | Rows matched/golden | Recall | Cell acc. | Cost |
| --- | --- | --- | --- | --- |
| `17-18 XS Loss Runs - AIG.pdf` | 0/1 | **0%** | **0%** | $0.032 |
| `18-19 XS Loss Runs - AIG.pdf` | 0/1 | **0%** | **0%** | $0.034 |
| `2017-22 CIC Pkg Loss Runs.PDF` | 1/21 | **4.8%** | 68.4% | $0.041 |
| `CAU Loss Runs 2016-2021.PDF` | 1/20 | **5.0%** | 95.0% | $0.061 |
| `WCO Loss Runs 2018-2022.PDF` | 1/8 | **12.5%** | 81.2% | $0.287 |
| `Auto Loss Runs.pdf` | 8/16 | 50.0% | 79.7% | $0.078 |
| `GLI PKG CPP WCO Loss Runs 2017-2018.PDF` | 3/4 | 75.0% | 95.7% | $0.055 |
| `Loss_2.pdf` | 11/12 | 91.7% | 83.2% | $0.114 |
| `Loss Run_Report.pdf` (scanned, no text layer) | 25/26 | 96.2% | 73.3% | $0.174 |
| `05.09.2020 to 6-30-2022- GL only Policy Loss Run Report.pdf` | 12/12 | 100% | 84.7% | $0.040 |
| `16-18 GL Loss Runs - Twenty Mile Acceptance.pdf` | 3/3 | 100% | 94.4% | $0.018 |
| `18-19 GL Loss Run - United Specialty.pdf` | 1/1 | 100% | **100%** | $0.013 |
| `22-23 LSUM RNC GLIA +XLC 5yr as of 7-15-22.pdf` | 103/103 | 100% | 79.6% | $0.077 |
| `78817366_KINSALE LOSS RUNS.pdf` | 1/1 | 100% | 90.9% | $0.028 |
| `CAU CPP MAR PKG WCO Loss Runs.PDF` | 51/51 | 100% | 95.5% | $0.059 |
| `CAU Loss Runs - 01 24 2023.PDF` | 16/16 | 100% | 66.9% | $0.088 |
| `Chubb_Loss-1.pdf` | 2/2 | 100% | 92.0% | $0.018 |
| `Claim Loss Date Between 5-9-2017 and 5-9-2020 GL & UMBRELLA.pdf` | 9/9 | 100% | 83.0% | $0.050 |
| `DetailResults_ACP_3086469186.pdf` | 1/1 | 100% | 66.7% | $0.046 |
| `GLI Loss Runs.PDF` | 5/5 | 100% | 67.3% | $0.055 |
| `LRs_Application_CAU CPP MAR PKG WCO Loss Runs.PDF` | 51/51 | 100% | 93.4% | $0.057 |
| `Loss-4.pdf` | 1/1 | 100% | 84.2% | $0.027 |
| `Loss Runs.pdf` | 5/5 | 100% | 94.5% | $0.060 |
| `Loss Runs Report 5 years recent.pdf` | 2/2 | 100% | 76.2% | $0.017 |
| `Reaco - 2022 Auto - LR_17-20.pdf` | 5/5 | 100% | 84.3% | $0.096 |
| `Reaco - 2022 Auto - LR_20-22.pdf` | 1/1 | 100% | 80.0% | $0.077 |
| `Reaco - 2022 WC - LR.pdf` | 12/12 | 100% | 95.0% | $0.129 |
| `WC Loss Runs.pdf` (scanned, no text layer) | 5/5 | 100% | 82.5% | $0.066 |
| `Loss-3.pdf` | — no golden entry — | | | $0.061 |

**Pooled (28 scored, excludes `Loss-3.pdf`): 336/395 rows matched (85.1%
recall), 5,967/7,027 cells correct (84.9%).** Total cost across the 29
processed documents (including the 2 old, now-superseded runs of CIC and
KINSALE kept as before/after evidence): **$2.02**. Total API call time:
~95 minutes of extraction, ~125 minutes of QA review, summed across
documents — most of which ran concurrently, not sequentially, once
`--concurrency` landed partway through the batch.

## Issue 1 — scorer matches on one column, and it's blank on some documents

**The dominant defect. It fully explains all three of the historically
documented "merge collapse" cases — none of them are a merge problem.**

`accuracy.py`'s `score()` pairs extracted rows to golden rows using a single
column, `schema.match_column` (`Claim Number` for the loss run schema):

```python
golden_by_key = {_match_key(r, match_column): r for r in golden}
extracted_by_key = {_match_key(r, match_column): r for r in extracted}
```

(`lossrun/accuracy.py:395-396`.) A dict comprehension keeps only the last
value per key. All three documents below print no claim number at all —
confirmed both in golden and in each document's own `Final Table`:

| Document | Rows | Recall | Confirmed blank |
| --- | --- | --- | --- |
| `2017-22 CIC Pkg Loss Runs.PDF` | 21 | 4.8% | golden `Claim ID` is `None` for all 21 rows; extracted `Claim Number` is `N/A` for all 21 |
| `CAU Loss Runs 2016-2021.PDF` | 20 | 5.0% | same, all 20 rows |
| `WCO Loss Runs 2018-2022.PDF` | 8 | 12.5% | same, all 8 rows — also has no `Claimant Name` |

On each, `golden_by_key` and `extracted_by_key` both collapse to one entry,
so the pairing loop can report at most one match, no matter how correct the
extraction is. Confirmed independent of merge (Issue 2): CIC's merge step
reports **21 raw rows → 21 merged rows** — the shipped table already has all
21 distinct claims — and the recall number still can't see past the first
one. Until `score()` matches on more than one column, or falls back to row
content when the match column is uniformly blank, any document without a
usable per-row claim number will under-report recall regardless of
extraction quality.

## Issue 2 — merge-key collapse: fixed, confirmed independent of Issue 1

The old merge collapsed rows sharing a fixed `(Policy Number, Claim Number,
Claimant Name)` key, which silently destroyed data on documents where that
key isn't unique per claim. `lossrun/merge.py` was rewritten to merge on
whole-row consistency instead (two rows merge only if no column states two
different things; a blank is "unknown," not a match) — see
`tests/test_merge.py`'s `test_blank_identity_columns_do_not_collapse_distinct_claims`.

Re-extracted the two structurally-collapsed documents under the fix and
compared against their original, pre-fix runs:

| Document | Before (old merge) | After (new merge) |
| --- | --- | --- |
| `78817366_KINSALE LOSS RUNS.pdf` | 10 raw rows → 3 shipped (golden wants 1) — under-merged | 2 raw rows → **1 shipped**, 1/1 matched, 100% recall, 90.9% cell accuracy — **matches golden exactly** |
| `2017-22 CIC Pkg Loss Runs.PDF` | 21 raw rows → 16 shipped (5 lost) — over-merged | **21 raw rows → 21 shipped**, no collapse — but accuracy still reads 1/21 because of Issue 1, not merge |

KINSALE is fully resolved end-to-end — the one case in this whole report
where both the underlying defect and the reported number are fixed. CIC and
WCO's merge is also structurally correct now (confirmed via raw→shipped row
counts), but their *reported* score is gated entirely by Issue 1.

## Issue 3 — composite claim/occurrence key defeats scoring (both AIG docs)

`17-18 XS Loss Runs - AIG.pdf` and `18-19 XS Loss Runs - AIG.pdf` both score
0% cells, 0% recall. Golden's claim identifier is a composite
`claim/occurrence` value that the extracted claim number doesn't reproduce
verbatim, so `_match_key` never lines up — same failure shape as Issue 1 (a
matching problem, not necessarily an extraction problem), but the cause here
is a formatting mismatch rather than a missing column. Not yet re-checked
directly against the extracted table's row content.

## Issue 4 — extraction can succeed and still fail to write (fixed; document excluded)

`LossRunsReport_9_7_2022.pdf` (36 pages) extracted and scored correctly on
every attempt — the log consistently showed `127/127 rows matched, ~91%
cells correct` — then failed at the very last step:

```
failed: All strings must be XML compatible: Unicode or ASCII, no NULL bytes or control characters
```

Root cause took two passes to fully nail down. That exact error string comes
from **lxml**, openpyxl's XML backend, not from openpyxl's own
`ILLEGAL_CHARACTERS_RE` check — which only catches C0 control characters
(`\x00-\x08`, `\x0b-\x0c`, `\x0e-\x1f`). lxml's validator is stricter: it also
rejects lone UTF-16 surrogates (`\ud800-\udfff`) and the two Unicode
noncharacters (`￾`, `￿`), both plausible OCR/decoding artifacts on
a noisy 36-page scan. A first fix covering only the C0 range still failed on
retry with the identical error — the second, broader fix
(`lossrun/report.py`'s `_sanitize`, now applied to every cell in
`_write_sheet`) covers all three categories and is covered by
`tests/test_report.py::test_control_characters_are_stripped_instead_of_failing_the_write`.

The fix is real and in the codebase, but **this document is excluded from
the dataset by decision** rather than re-run a third time. Two things worth
flagging about the 3 failed attempts before that decision:

- The QA step separately flagged this document's row count as suspicious —
  `layout discovery reported 86 row(s); the final table holds 127 (41 more
  than expected)` — but since the extracted count matched golden exactly
  (127/127) on every attempt, that heuristic warning looks like a false
  positive here, not a real over-extraction.
- **A silent cost leak**: `pipeline.py` calls `write_workbook` before
  `append_ledger` (`lossrun/pipeline.py:239` vs. `:252`). When the write
  raises, the function never reaches the ledger call, so none of the 3
  attempts' real API cost was ever recorded anywhere — not in
  `telemetry.xlsx`, not in this report. The money was spent; it's just
  invisible to every number in this file. Worth fixing independently of
  this document (write the ledger row first, or wrap the workbook write so
  a failure there doesn't also swallow the cost record).

## Issue 5 — "Policy Year" read as 0% accuracy, but it was never scored

Asked, separately from the numbers above: why did the `Columns` sheet show
`Policy Year` at 0% on every document? Root cause was two stacked defects:

1. Golden has no `Policy Year` column at all. `_column_map_for` (identity
   mapping over every schema column) still puts `"Policy Year": ""` on every
   golden row regardless, and `ColumnScore.accuracy` (`accuracy.py:179-181`)
   returns `0.0` when `compared == 0` instead of `None` — so a column with
   *zero scoreable data* renders identically to a column the model *always
   gets wrong*. This part of the code is unchanged; it's still true for any
   column golden doesn't carry.
2. Per direction from the project owner, backfilled `Policy Year` into
   `goldens/Loss Runs GTs (1).xlsx` (column 26 of `Sheet1`/`Sheet2`) using
   **the model's own extracted values**, matched to golden rows by Claim
   Number. This is explicitly not independent ground truth — it makes
   `Policy Year` accuracy circular by construction, chosen deliberately over
   rigorously reading each document's PDF, for speed. Rows with a blank
   Claim Number (Issue 1's three documents) were left blank rather than
   guessed, to avoid mis-attributing a value to the wrong row — confirmed
   again on `WCO Loss Runs 2018-2022.PDF`, whose all-8-rows-blank Claim
   Number left every row unfilled, consistent with the other two.

Result: 272/272 (100.0%) across the 16 documents where at least one row had
a Claim Number to match on. The 100% is expected and uninformative — it
confirms the mechanical fix (golden now has real, non-blank values to
compare against) rather than saying anything about extraction quality for
this column. If `Policy Year` accuracy needs to mean something, golden needs
real transcription, not extracted values copied back into it.

Backfilling this hit one bug worth flagging on its own: the first attempt
silently wrote nothing for numeric Claim IDs. Golden loaded without
`data_only=True` (needed to write) returns a numeric Claim ID as a Python
`float` (`34454.0`), which stringifies differently than the extraction's
`"34454"`, so the key comparison silently missed every numeric-claim-number
row. Fixed by reusing `accuracy.py`'s existing `_as_text` (already handles
this exact Excel float/int quirk for the same reason) instead of a bare
`str()`.

## Not a bug

`Loss Run_Report.pdf`: 32 raw rows → 27 unique keys → 27 shipped, but recall
is 96.2% (25/26 against golden) — the reduction tracks the golden row count,
so this dedup looks legitimate (the same claim listed across multiple report
periods), not a lossy collapse.
