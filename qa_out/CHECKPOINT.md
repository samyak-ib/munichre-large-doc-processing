# Checkpoint — batch `156671bb`, stopped 2026-08-10 ~17:2x

Batch extraction over all 30 samples was stopped mid-run at the user's
request. **16 of 30 documents are done and safely on disk** — nothing needs
to be redone. This file has everything needed to pick back up.

## What's done (16, all recorded in `qa_out/direct_calls/telemetry.xlsx`)

```
05.09.2020 to 6-30-2022- GL only Policy Loss Run Report.pdf
16-18 GL Loss Runs - Twenty Mile Acceptance.pdf
17-18 XS Loss Runs - AIG.pdf
18-19 GL Loss Run - United Specialty.pdf
18-19 XS Loss Runs - AIG.pdf
2017-22 CIC Pkg Loss Runs.PDF
22-23 LSUM RNC GLIA +XLC 5yr as of 7-15-22.pdf
78817366_KINSALE LOSS RUNS.pdf
Auto Loss Runs.pdf
CAU CPP MAR PKG WCO Loss Runs.PDF
Chubb_Loss-1.pdf
LRs_Application_CAU CPP MAR PKG WCO Loss Runs.PDF
Loss Run_Report.pdf
Loss-3.pdf
Loss-4.pdf
Loss_2.pdf
```

Each has a `qa_out/direct_calls/<name>_<timestamp>/extraction.xlsx`. There's
also a stray empty directory,
`qa_out/direct_calls/CAU_Loss_Runs_-_01_24_2023_20260810-171604/` — that
document was mid-flight when the process was killed and produced no
`extraction.xlsx`. Safe to delete or just ignore; the resume command below
reprocesses that file cleanly into a fresh timestamped directory.

## What's left (14)

```
samples/Loss Runs/CAU Loss Runs - 01 24 2023.PDF
samples/Loss Runs/CAU Loss Runs 2016-2021.PDF
samples/Loss Runs/Claim Loss Date Between 5-9-2017 and 5-9-2020 GL & UMBRELLA.pdf
samples/Loss Runs/DetailResults_ACP_3086469186.pdf
samples/Loss Runs/GLI Loss Runs.PDF
samples/Loss Runs/GLI PKG CPP WCO Loss Runs 2017-2018.PDF
samples/Loss Runs/Loss Runs Report 5 years recent.pdf
samples/Loss Runs/Loss Runs.pdf
samples/Loss Runs/LossRunsReport_9_7_2022.pdf
samples/Loss Runs/Reaco - 2022 Auto - LR_17-20.pdf
samples/Loss Runs/Reaco - 2022 Auto - LR_20-22.pdf
samples/Loss Runs/Reaco - 2022 WC - LR.pdf
samples/Loss Runs/WC Loss Runs.pdf
samples/Loss Runs/WCO Loss Runs 2018-2022.PDF
```

`CAU Loss Runs 2016-2021.PDF` and `WCO Loss Runs 2018-2022.PDF` are the two
still-unconfirmed historical merge-key-collapse cases — highest-value
documents in the remaining set.

## Resume command

Run from the repo root. This only touches the 14 files above — the 16 done
documents are untouched and won't be re-billed:

```bash
PYTHONUNBUFFERED=1 uv run lossrun extract \
  "samples/Loss Runs/CAU Loss Runs - 01 24 2023.PDF" \
  "samples/Loss Runs/CAU Loss Runs 2016-2021.PDF" \
  "samples/Loss Runs/Claim Loss Date Between 5-9-2017 and 5-9-2020 GL & UMBRELLA.pdf" \
  "samples/Loss Runs/DetailResults_ACP_3086469186.pdf" \
  "samples/Loss Runs/GLI Loss Runs.PDF" \
  "samples/Loss Runs/GLI PKG CPP WCO Loss Runs 2017-2018.PDF" \
  "samples/Loss Runs/Loss Runs Report 5 years recent.pdf" \
  "samples/Loss Runs/Loss Runs.pdf" \
  "samples/Loss Runs/LossRunsReport_9_7_2022.pdf" \
  "samples/Loss Runs/Reaco - 2022 Auto - LR_17-20.pdf" \
  "samples/Loss Runs/Reaco - 2022 Auto - LR_20-22.pdf" \
  "samples/Loss Runs/Reaco - 2022 WC - LR.pdf" \
  "samples/Loss Runs/WC Loss Runs.pdf" \
  "samples/Loss Runs/WCO Loss Runs 2018-2022.PDF" \
  --out qa_out --results-out qa_out
```

(Same ledger, same `qa_out/direct_calls/telemetry.xlsx` — new runs append to
it, so nothing needs merging afterward.)

## Regenerating the report at any point

```bash
dirs=(); for d in qa_out/direct_calls/*/; do [ -f "$d/extraction.xlsx" ] && dirs+=("$d"); done
uv run lossrun results "${dirs[@]}" --out qa_out --source-dir samples --version snapshot
```

## Numbers as of the stop (16 docs run, 15 scored — `Loss-3.pdf` has no golden)

- Row recall: 270/302 matched — **89.4%**
- Cell accuracy: 4,588/5,390 — **85.1%**
- Cost so far: **$0.866** ($0.207 input + $0.659 output)

Full per-document breakdown and defect notes are in `docs/mistakes.md` under
"Latest run — batch `156671bb`" — that section is current as of this
checkpoint and should keep being updated as the remaining 14 land.
