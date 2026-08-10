# Extracting a different table

This pipeline shipped tuned for one table: the loss-run claim table in
`schema.json`. Everything else in it — chunking a long document, discovering
its layout, extracting every row in one pass, reviewing the result against the
source pages, verifying identifiers appear verbatim — has nothing to do with
insurance. `--schema` is what selects the table; point it at a plain column
list instead of `schema.json` and the same pipeline runs against that list.

```bash
uv run lossrun schema --schema examples/columns.invoice.yaml   # sanity-check it
uv run lossrun extract your-document.pdf --schema examples/columns.invoice.yaml
```

Every `lossrun` subcommand that takes `--schema` (`extract`, `schema`, `score`,
`results`, `compare`) accepts either format — `schema.json`'s Instabase class
definition, or the column list described here — and picks the right loader
from the file's own shape. Nothing else changes: `--config`, `--model`,
`--qa-model`, `--provider`, `--effort` and `--no-qa` all mean the same thing
they do for a loss run, because none of them are about the columns.

## The format

A YAML (or JSON) file with a top-level `columns` list. Each entry is a plain
string, or an object with a `name` and whatever roles apply:

```yaml
columns:
  - name: Invoice Number
    key: true
  - name: Line Total
    type: money
  - Description          # a bare string: every role defaults to off
```

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | — (required) | The column header the model returns. |
| `prompt` (or `description`) | a generic "extract this value" sentence | What to tell the model to extract. Write the same kind of instruction `schema.json` carries per column — the more specific, the fewer wrong guesses. |
| `type` | `text` | `text`, `date`, or `money`. Drives rendering (dates to `MM/DD/YYYY`, money to a plain decimal) and comparison during scoring. |
| `key` | `false` | Part of the row's identity. Declare every key column, **in priority order** — it becomes the merge key across chunks and the set verified verbatim against the document's text layer. At least one column must set this. |
| `doc_level` | `false` | A document-wide value (e.g. who issued the document) rather than a per-row one: extracted once during layout discovery and stamped onto every row, instead of asked for in the per-row extraction. A `doc_level` column cannot also be a `key` — a doc-level value isn't emitted per row for the merge to key on. |
| `identifier` | `false` | Verified verbatim against the document's text layer without joining the merge key — a secondary id a key column doesn't already cover. Every `key` column is one of these too. |
| `backfill` | same as `doc_level` | Filled from layout discovery's values when a row comes back without one. Defaults on for `doc_level` columns; set it explicitly on a row-level column that sometimes needs the same treatment (the loss run's `Valuation Date` is the example — extracted per row, but backfilled from the document header when a row's own value is missing). |
| `match` | `false` | The single column used to match a row to golden data during scoring, when you supply `--golden`. At most one column should set this; without it, the schema falls back to the first `key` column. Set it explicitly when your first key column repeats across rows and cannot identify one on its own — see `Invoice Number` vs. `Line Number` in the example below. |
| `group` | `false` | The column that may sit as a block header above a group of rows instead of appearing in its own column (carried down to every row beneath it once layout discovery reports that shape). Same one-column, same fallback as `match`. |

Two more fields sit above the `columns` list, not inside it:

```yaml
row_label: line item        # default: "row"
document_label: vendor invoice   # default: "document"
```

These only change the wording of the prompts sent to the model — "extract
every **line item** row from a **vendor invoice**" instead of "extract every
**row** row from a **document**". They cost nothing to set and make the
framing match what the model is actually looking at.

## A worked example

[`examples/columns.invoice.yaml`](../examples/columns.invoice.yaml) extracts a
vendor invoice's line-item table — an unrelated domain, to show the format
carries no insurance-specific assumptions:

```bash
uv run lossrun schema --schema examples/columns.invoice.yaml
```

```
8 columns, 1.0 KiB of prompts  — 'line item' rows from: vendor invoice

key columns (row identity):  Invoice Number, Line Number
matched to golden data on:   Line Number
block-header carry-down on:  Invoice Number
```

Read the file itself for the reasoning behind each column's roles — in
particular why `Line Number`, not `Invoice Number`, is marked `match`: an
invoice number identifies the whole document, and can repeat across every line
item in it, exactly the way the loss run's `Policy Number` repeats across every
claim under one policy and so cannot be what golden rows are matched on
either.

## What stays the same

Every stage in [PIPELINE.md](PIPELINE.md) runs unchanged for a custom column
list — a column-list schema is not a smaller pipeline, it is the same one
pointed at a different contract:

- **Profiling and chunking** — page windows, overlap, and the request-size
  ceilings are about the document, not the columns.
- **Layout discovery** — one call over the opening pages maps your columns to
  whatever headers this document actually uses, exactly as it does for
  `schema.json`'s 25.
- **Extraction** — every column in one call per chunk, with the verbatim
  clause and seam anchors built from your declared key columns.
- **QA review** — the table is reviewed against the source pages it came from,
  and a correction is written back only when the value it proposes occurs in
  the document's own text layer. This is the "re-validate the results" step,
  and it does not know or care what the columns mean.
- **Key verification** — every identifier column (`key` or `identifier`) is
  checked against the document's text layer, the hallucination signature.
- **Scoring** — optional, and only runs when you pass `--golden`. Point it at
  a spreadsheet whose header row uses the same column names as your schema
  (or the loss run's own aliases, still honoured for that file) and per-column
  accuracy works the same way it does for the loss run — money within a cent,
  dates parsed before comparison, an empty golden cell reported but never
  scored. A golden column your schema does not recognise is ignored, and a
  schema column golden never mentions is skipped rather than scored blank.
  Without `--golden`, extraction and QA still run in full — scoring is a
  measurement against known-correct data, not a requirement for either.

## What is loss-run-specific and stays out of your way

`prompts.COLUMN_HINTS` carries four rules (`Policy Total`, `Occurrence ID`,
`Valuation Date`, `Description`) written after reading loss-run PDFs and
checking them against loss-run golden data. They are keyed by column name, so
a schema that does not carry a column with one of those exact names never
sees them.
