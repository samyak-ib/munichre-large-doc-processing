# Why accuracy is lower on some documents

Every number here comes from scoring the shipped `Final Table` of a real run
against `goldens/Loss Runs GTs (1).xlsx`. Reproduce any of it with:

```bash
uv run python -m lossrun.cli compare "qa=out_qa/direct_calls" --out comparisons/x.xlsx
uv run python -m lossrun.cli score out_qa/direct_calls/*/ --influence
```

Companion to [CHALLENGES.md](CHALLENGES.md), which covers constraints of the
pipeline. This covers the documents.

## What the extended sample set changed

Twenty-four documents were added to the five the pipeline was built against. They
did not mostly reveal a worse extractor — they revealed **four defects in our own
code**, three of which the original five could never have surfaced because all
five carry clean claim numbers and conventional dates.

| Defect | Cost, measured | Status |
| --- | --- | --- |
| Rows collapsed by the merge key (§ below) | **31 rows** across three documents; two collapse to a single row | **open** — fix is described, not applied |
| `13-Jul-17` and `07-06-21` unparseable | 191 cells on one document | **fixed** |
| Scorer matches on claim number alone | 2 documents scored 0% that are 80% and 91% | **open** — measurement change, deliberately not made mid-run |
| Composite `claim/occurrence` defeats matching | same 2 documents | **open** |

Three of the four are keying or parsing, not reading. In every case the model had
transcribed the page correctly and the pipeline threw the work away afterwards.

## The most serious defect found: rows silently collapsed by the merge key

**A document with no claim-number column loses every row but one, and says
nothing about it.**

`CAU Loss Runs 2016-2021.PDF` is one page carrying 20 claims. The extraction read
**all 20 correctly** — the raw rows are right, with the correct loss dates and
amounts. One row shipped.

The merge key is `(Policy Number, Claim Number, Claimant Name)`. This document
prints no claim number and no claimant; every claim sits under one policy. So all
20 rows normalize to the same key:

```
('b1t9180y', '', '')          <- 20 rows, 1 distinct key
```

`merge_rows` keeps the first row per key and discards the rest. 19 claims,
including a $92,839 loss and a $10,906 loss, never reached the table.

**It is silent.** `merge_rows` does record what it dropped — `MergeResult` carries
`duplicate_keys` and a `Conflict` per discarded value — but `finalize_rows`
returns only `rows` and throws that away, so none of it reaches the Issues sheet.
That run's Issues sheet is empty. Nothing in the workbook, the ledger or the
console says 19 rows were lost.

Measured across every run scored here:

| Document | Rows extracted | Rows shipped | Lost | Golden | Merge key |
| --- | --- | --- | --- | --- | --- |
| `CAU Loss Runs 2016-2021.PDF` | 20 | 1 | **19** | 20 | `('b1t9180y', '', '')` |
| `WCO Loss Runs 2018-2022.PDF` | 8 | 1 | **7** | 8 | `('0196-47556', '', '')` |
| `2017-22 CIC Pkg Loss Runs.PDF` | 21 | 16 | **5** | 21 | mixed |

**31 rows**, and all three documents are recent additions — the original five all
carry claim numbers, which is why this never showed up before.

Two of the three collapse to a *single* row. A document that returns one row where
golden holds eight or twenty is not a subtle degradation; it is the pipeline
silently discarding almost everything it correctly read.

> Not every collapse is a loss. `Reaco - 2022 WC - LR.pdf` shows 24 raw rows
> merging to 12, and that is the merge working: the model emitted every row twice
> and deduplication removed them. It scores 12/12 at 95.0%. The distinction is
> whether the collapsed rows were duplicates or distinct claims, which is exactly
> what `MergeResult.conflicts` records and `finalize_rows` discards.

**Fix, in order of urgency:**

1. **Surface it.** `finalize_rows` already has the evidence; pass
   `MergeResult.duplicate_keys` and its conflicts to the caller and write them
   into Issues. Silent row loss is worse than the loss itself. This is a small,
   safe change.
2. **Fall back on the key.** When a document carries no claim number, the key has
   to include something that distinguishes the rows — accident date plus amount
   would separate all 20 here. Layout discovery already reports
   `absent_columns`, so the pipeline knows when it is in this situation before
   extraction starts.
3. **Refuse to ship a collapse.** A merge that turns 20 rows into 1 is never
   correct; it should fail the document rather than deliver one row as though it
   were the whole table.

Not fixed in this branch: changing the merge key mid-measurement would make the
runs in this document incomparable. It is the first thing to do next.

## The scorer makes the same assumption, in a second place

`merge.normalize_key` loses rows when there is no claim number. `accuracy._match_key`
loses *matches* for the same reason — it keys on claim number alone:

```python
def _match_key(row):
    value = row.get("Claim Number", "")
    return "" if is_empty(value) else _WS_RE.sub("", str(value)).casefold()
```

**`2017-22 CIC Pkg Loss Runs.PDF`** — golden and extraction *both* hold
`Claim Number = N/A` on every row, and the claimant names line up exactly
(`WOB BETHESDA, LLC`, `MYRA CLEARY`, `HOLDINGS SOLIDCORE`). All 21 golden rows
collapse onto the single key `""`, so the document scores **1/21 rows — 5%
recall** for data that is largely right.

**The two AIG documents** fail differently. The model returned
`501-869408-001/0533293199` where golden holds `501-869408-001`: the page prints
a composite claim/occurrence identifier and the model kept both halves. Exact
matching sees two different claims, so both documents score **0%**.

Measured with a key that falls back to claimant + accident date, and that
compares only the first segment of a composite identifier:

| Document | As measured | With a fallback key |
| --- | --- | --- |
| `17-18 XS Loss Runs - AIG.pdf` | 0/1 rows, 0.0% | 1/1 rows, **80.0%** |
| `18-19 XS Loss Runs - AIG.pdf` | 0/1 rows, 0.0% | 1/1 rows, **90.9%** |

**This has deliberately not been changed.** Altering the match key mid-measurement
would move every number in this document and in the comparison workbooks without
a single extraction changing. It is a recommendation, and the experiment above is
what it is worth.

> `2017-22 CIC` is not fixed by the fallback either — its accident dates come back
> `N/A` on many rows, so claimant + date does not identify them. A document with
> neither a claim number nor a reliable date needs the row's ordinal position,
> which nothing currently carries through the merge.

## Dates the parser could not read

Two formats reached the pipeline that no format string covered, so they passed
through verbatim and could never equal golden:

| Printed | Why it failed | Now |
| --- | --- | --- |
| `13‐Jul‐17` | separator is **U+2010 HYPHEN**, not ASCII `-`; and `%d-%b-%y` was absent | `07/13/2017` |
| `07-06-21` | `%m/%d/%y` existed for slashes, `%m-%d-%Y` for hyphens, but not `%m-%d-%y` | `07/06/2021` |

The first cost **191 cells on `22-23 LSUM RNC GLIA +XLC`** — `Accident Date`
0/103 and `Closed Date` 0/88, every one of them read correctly off the page. That
document moved **67.1% → 76.4%**, with `Accident Date` going 0/103 → 103/103.

Fixed in `cleaning.py` and in `accuracy.py`. Both, because they answer different
questions: cleaning renders a date into golden's form for tables written from now
on, while the scorer parses the extracted value at score time and so also repairs
the tables already shipped.

Typographic dash folding covers hyphen, en dash, em dash, horizontal bar, minus
sign and fullwidth hyphen — a PDF supplies any of them where a date format
expects ASCII.

## Bottom line

- **Half of all wrong cells are omissions, not misreads** — 148 of 305 are cells
  where the extraction returned `N/A` and golden holds a value. Omission is a
  different failure from transcription and needs a different fix.
- **Accuracy tracks one property more than any other: whether the page has a
  text layer.** The one scanned document scores 72.1%; the four born-digital
  ones average 90.3%.
- **A large slice of the remaining "error" is not error.** On the biggest
  document, 27 of its wrong cells are a golden disagreement the golden set's own
  second transcription resolves in the model's favour.
- **Two failures are real, systematic and fixable**: the claimant/adjuster swap
  on stacked headers, and document-level values joined across carrier sections.

## Where the five documents stand

Shipped QA run (batch `3740ef65`), scored against golden:

| Document | Pages | Text layer | Rows | Cell acc. | Wrong cells | Omitted | Misread |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `Loss-4.pdf` | 2 | yes | 1/1 | **100%** | 0 | 0 | 0 |
| `Chubb_Loss-1.pdf` | 6 | yes | 2/2 | **91.7%** | 4 | 0 | 4 |
| `LRs_Application…` | 10 | yes | 51/51 | **88.5%** | 129 | 51 | 78 |
| `Loss_2.pdf` | 15 | 93% | 11/12 | **81.1%** | 41 | 16 | 25 |
| `Loss Run_Report.pdf` | 38 | **0%** | 23/26 | **72.1%** | 131 | 81 | 50 |

Pooled by column, worst first — this is where the losses actually live:

| Column | Correct | Accuracy | Dominant cause |
| --- | --- | --- | --- |
| Claimant Name | 18/80 | **22.5%** | adjuster read as claimant (§1) |
| Insurer Loss Run | 23/88 | **26.1%** | carrier named only in a logo, and brand-vs-entity (§4) |
| Indemnity Paid | 55/88 | **62.5%** | golden disagreement (§3) |
| Line of Business | 56/87 | **64.4%** | omitted on the scan; abbreviation mismatch elsewhere (§2, §4) |
| Report Date | 23/32 | **71.9%** | omitted on the scan (§2) |
| Insured | 67/88 | **76.1%** | joined across carrier sections (§5) |
| Description | 71/87 | **81.6%** | omitted on the scan (§2) |
| Occurrence ID | 59/71 | **83.1%** | derivation rule applied where a real column exists (§6) |

Everything not listed is above 85%, and eight columns — including `Claim
Number`, the row key — are at or near 100%.

---

## 1. The claimant/adjuster swap — the single largest real error

`LRs_Application`: **0 of 51** claimant names correct.

| Claim | Extracted | Golden |
| --- | --- | --- |
| C00320453-02 | `CARMEN RIVAS` | `EMILEEANNE SYBRANT` |
| C00320453-03 | `SUE MEDUSKI` | `EMILEEANNE SYBRANT` |
| C00320453-04 | `SUE MEDUSKI` | `ALICE SYBRANT` |
| C00320453-05 | `SUE MEDUSKI` | `KAYLEE SYBRANT` |

One name repeating across unrelated claims is the signature: the document stacks
two labels per column — `Coverage Type / Claim Adjuster` above `Claimant Name /
Claim Description` — and flattened into a text stream the adjuster's name comes
first on every row. This is [CHALLENGES.md](CHALLENGES.md) #1, and it is
intermittent: the same document at identical settings returned correct claimant
names on other runs.

**Why the safety nets missed it.** The verbatim key check passes, because
`SUE MEDUSKI` *is* printed on the page, one column over — the check proves a
value was copied, never that it came from the right column (#1a). And the QA
review proposed **zero** corrections on this document, so the second look did not
catch 51 wrong names either. That is the clearest open weakness in the current
pipeline.

**Fix:** an explicit clause in the extraction prompt keyed off the stacked
headers that layout discovery already reports correctly, plus a QA prompt that
names the adjuster/claimant confusion as a thing to look for.

## 2. No text layer means omission, not misreading

`Loss Run_Report.pdf` is 38 scanned pages, 0% text coverage, and scores 72.1% —
the lowest of the five. **81 of its 131 wrong cells are omissions**: the
extraction returned `N/A` where golden holds a value.

| Column | Correct | Typical failure |
| --- | --- | --- |
| Line of Business | 3/23 | `N/A` where golden holds `WC` |
| Description | 7/23 | `N/A` where golden holds `CAUGHT IN OR BETWEEN — …` |
| Report Date | 11/20 | `N/A` where golden holds `09/04/2018` |

It also loses rows outright — 23 of 26 — and it is the only one of the five that
does.

Everything downstream degrades with it. The verbatim key check is skipped
entirely (nothing to check against). The QA review ran, found 39 problems, and
could apply **none** of them, because the guard that keeps QA from inventing
values has no text layer to consult — [CHALLENGES.md](CHALLENGES.md) #23.

**Fix:** OCR the scanned pages locally, purely to build the text layer the
verifier and the QA guard read. Never to extract from — that was ruled out on
cost grounds and this does not reopen it.

## 3. Some of the "error" is the golden set disagreeing with itself

`Indemnity Paid` on `LRs_Application` scores 24/51. Every miss looks like this:

| Claim | Extracted | Golden |
| --- | --- | --- |
| C00317085-01 | `0` | `975.39` |
| C00318744-01 | `0` | `36263.01` |

These are WCC medical-only claims. `goldens/wrong_goldens.md` §5 already flags
this: the golden transcription put **Paid Medical into Indemnity Paid** on 27
rows. The model returned `0`, which is what the document prints for indemnity.

The golden set now settles it. The same PDF appears in the workbook **twice**,
under two names with two different transcriptions, and the newer one zeroes
exactly those rows:

| Golden transcription | Cell accuracy | Indemnity Paid |
| --- | --- | --- |
| `093fca2c-…__Updated Acords LRs_Application_…` (sheet 1) | 88.5% | 24/51 |
| `CAU CPP MAR PKG WCO Loss Runs.PDF` (sheet 2) | **90.5%** | **51/51** |

Same extraction, same PDF — 2 percentage points of the reported gap is which
transcription you score against. The newer one agrees with the model.

> This duplicate is also why `accuracy._resolve_golden_filename` prefers the
> golden entry carrying the whole document name: the two entries disagree, so
> the choice has to be deliberate rather than a function of sheet order.

## 4. `Insurer Loss Run` is a definition problem, not a transcription one

26.1% pooled, and almost every miss is a different kind of disagreement:

| Extracted | Golden | What is going on |
| --- | --- | --- |
| `FEDERAL INSURANCE COMPANY` | `CHUBB` | legal entity vs brand; both printed |
| `N/A` | `FCCI` | carrier appears **only in a letterhead image** |
| `AIG` | `AIG IntelliRisk` | brand vs product |
| `velocity risk underwriters®` | `Velocity Risk Underwriters` | casing and `®` |
| `N/A` | `CRC Group shown as broker; carrier not stated` | golden holds a *note*, not a value |

Only the last two are addressable by the extractor. The rest need the column
defined — brand or entity — and the FCCI case needs the page read as an image.
This is the largest column-level gap that is **not** a model failure.

`Line of Business` has the same shape: `Coml Inland Marine` against golden's
`INLAND MAR` is one abbreviation against another, and golden's
`H - Aerospace; CMA - Commercial …` is a composite nobody could produce.

## 5. Document-level values joined across carrier sections

`Loss_2.pdf` is a bundle of loss runs from several carriers. The model returned
`Insured` as every variant it saw, concatenated:

```
Friendswood Independent School District; FRIENDSWOOD INDEPENDENT SCHOOL DISTRICT; FRIENDSWOOD INDEPENDENT SCHOOL
```

Keeping only the first segment lifts the document from **81.1% to 83.9%**, worth
6 cells — and `Insured` is the single worst column on it (0/11).

This is the same bug the QA review caught on `Valuation Date`, where the model
returned `12/04/2020; 11/30/2020` — two dates joined into one cell, which the
column spec explicitly forbids. QA corrected the date and missed the insured.

Golden also caps what is reachable here: it records the same insured four ways
on one document — `Friendswood Independent School District` (5 rows),
`FRIENDSWOOD INDEPENDENT SCHOOL` (4), `FRIENDSWOOD INDEPENDENT SCHOOL DISTRICT`
(2) and `Friendswood ISD` (1). Two of those four will mismatch whatever is
returned.

**Fix:** the prompt already says "Emit exactly one date per row — never join
several dates into one cell" for `Valuation Date`. That clause needs to cover
every document-level column, and `merge.stamp_document_values` should reject a
joined value rather than pass it through.

## 6. `Occurrence ID` derivation fires where a real column exists

On `Loss_2` the model returned `501-168276` for claim `501-168276-001` — the
claim number with its sequence suffix stripped. `COLUMN_HINTS` says to do that
**only when the document carries no occurrence column**. This document has one,
holding `7248665175US`.

| Extracted | Golden |
| --- | --- |
| `501-168276` | `7248665175US` |
| `501-091014` | `5190030648US` |

The rule is right; its guard is not being honoured. Layout discovery already
reports which columns are present, so the derivation should be gated on
`absent_columns` containing `Occurrence ID` rather than left to the model's
judgement.

---

## What to fix, in order of measured value

| # | Fix | Worth | Confidence |
| --- | --- | --- | --- |
| 1 | OCR scanned pages for the text layer | 81 omitted cells on one document, and it unblocks QA there | high |
| 2 | Anti-adjuster clause in extract + QA prompts | up to 51 cells on one document | medium — the fault is intermittent |
| 3 | Forbid joined document-level values | 6 cells on `Loss_2`, more on any bundle | high |
| 4 | Gate the `Occurrence ID` derivation on layout | 5 cells on `Loss_2` | high |
| 5 | Define `Insurer Loss Run`: brand or legal entity | up to 65 cells, but needs a decision first | blocked on the class definition |

Items 3 and 4 are prompt and merge changes with no model risk. Item 1 is the
largest and is a local dependency, not an API cost. Item 5 is not an engineering
task until somebody decides what the column means.
