"""Prompt construction for layout discovery, table extraction and QA review.

Column semantics come verbatim from the schema (schema.json, or a plain column
list) — this module frames them and adds `COLUMN_HINTS`, loss-run-specific
document-shape guidance measured against golden data. The verbatim clause
carries the anti-autocorrect load that reasoning effort would otherwise carry,
because the API ignores the `reasoning` parameter.

Every function below takes the row/document wording (`schema.row_label`,
`schema.document_label`) and the key columns (`schema.key_columns`) from the
schema rather than hardcoding "claim" and "loss-run report" — the loss-run
schema sets them to exactly that, so its own prompts read unchanged, but a
different column list gets its own wording instead of a misleading frame.
"""

from __future__ import annotations

import json

from .schema_loader import TableSchema

KEY_EXAMPLE_PLACEHOLDER = "<{}>"


def _verbatim_clause(key_columns: tuple[str, ...]) -> str:
    keys = " and ".join(key_columns) if len(key_columns) <= 2 else ", ".join(key_columns)
    return f"""TRANSCRIPTION RULES — these override every other instruction:
- Copy every value character for character exactly as printed. Never normalize,
  never expand, never abbreviate, never correct an apparent typo or misspelling.
- {keys} identify a row. Do not add or remove leading
  zeros, digits, spaces, or punctuation. `C003` is `C003`, never `C0000003`.
  `McAllister` is `McAllister`, never `MacAllister`. If a value looks
  misspelled, it is not misspelled — copy it as printed.
- Never invent a value. When a column has no value for a row, emit `N/A`.
- Never merge, split, reorder, sort, or deduplicate rows. Emit them in the order
  they appear in the document."""


def _key_example(key_columns: tuple[str, ...]) -> str:
    return ", ".join(KEY_EXAMPLE_PLACEHOLDER.format(k) for k in key_columns)


def _output_contract(key_columns: tuple[str, ...]) -> str:
    return f"""OUTPUT FORMAT — return one JSON object and nothing else. No prose,
no markdown fence, no explanation:

{{"columns": [<the column names, in the order given below>],
 "rows": [[<value for each column, same order>], ...],
 "truncated": <true if you ran out of room before the last row, else false>,
 "last_row_key": [{_key_example(key_columns)}] of the final row you emitted}}

Column names are declared once in `columns`; every row is a positional array in
that same order. Do not repeat column names inside rows. This format exists to
keep long tables inside the output budget — if you are running short of room, set
`truncated` to true and stop after a complete row rather than abbreviating."""


def _qa_output_contract(key_columns: tuple[str, ...]) -> str:
    key_example = _key_example(key_columns)
    return f"""OUTPUT FORMAT — return one JSON object and nothing else. No prose,
no markdown fence, no explanation:

{{"findings": [{{"row": [{key_example}],
               "column": "<the column that is wrong>",
               "correct_value": "<what the document prints, character for character>",
               "reason": "<one short sentence>"}}],
 "missing_rows": [{{"row": [{key_example}],
                   "reason": "<one short sentence>"}}]}}

Report ONLY cells that are wrong. A chunk with nothing wrong returns
`{{"findings": [], "missing_rows": []}}` — that is the expected answer for a clean
chunk, not a failure to look."""


# Guidance added on top of the schema, one entry per column that needed it.
# Each rule describes a document shape loss runs actually use and that the
# schema's own wording does not cover; each was written after scoring against
# golden data showed the column failing for a structural reason rather than a
# transcription one. Kept separate from schema.json so this dict is exactly the
# delta to hand back for the class definition. Column names that do not appear
# in a given schema simply never match — harmless on any other column list.
COLUMN_HINTS = {
    "Policy Total": """This figure is printed in the document as a policy-level
aggregate. It is never on the claim row itself, which is why it is easy to miss.
It sits in one of two places:
- A summary table near the front, often headed `LOSS RUN SUMMARY` or
  `POLICY SUMMARY`, carrying one row per policy or per policy period with the
  total under a header such as `Net Incurred`, `Gross Incurred`, `Total Incurred`
  or `Incurred`.
- A subtotal row inside the claim table, labelled `TOTAL`, `Policy Total` or
  `Term Total`, closing each policy block.
Find that figure, then repeat it on EVERY claim row belonging to that policy.
Join the summary row to the claim rows on policy number; when the summary table
is keyed by line of business and policy effective date instead, join on those.
Never add up the claim rows yourself — report only what is printed.""",
    "Occurrence ID": """Many loss runs carry no occurrence column at all. When
there is none, and the claim numbers in this document end in a sequence suffix —
`C00320453-02`, `C00320453-03`, `C00320453-04` — the occurrence identifier is the
claim number with that suffix removed: `C00320453`. Claims sharing a prefix are
the same occurrence. Apply this only when the suffix pattern is consistent down
the whole column; if claim numbers do not carry such a suffix and no occurrence
column exists, return `N/A`.""",
    "Valuation Date": """Printed in a page or section header, labelled
`Report as of`, `Current As of Date`, `Losses as of`, `Valued as of`,
`As of Date` or `Valuation Date`. Ignore `Run Date`, `Print Date` and
`Date Printed` — those say when the report was produced, not what it is valued
at, and a document often shows both.
A bundle of loss runs from several carriers carries a DIFFERENT as-of date in
each carrier's section. Emit the date belonging to the section the claim appears
in, not one date for the whole document. Emit exactly one date per row — never
join several dates into one cell.""",
    "Description": """When the document has both a coded cause column and a
free-text narrative column, extract the CODED CAUSE.
- Coded cause — short standardized phrases from a fixed taxonomy, headed
  `Claim Description`, `Cause Desc`, `Cause Cd Desc`, `Loss Cause` or
  `Cause of Loss`. Example: `STRAIN OR INJURY BY TWISTING`.
- Free-text narrative — a sentence describing the incident, headed
  `Accident Description`, `Loss Remarks` or `Claim Notes`. Example:
  `WHILE WORKING ON HIS KNEES HE FELT A SHARP PAIN IN HIS BACK.`
Take the coded cause when both are present. Fall back to the narrative only when
the document has no coded cause column.""",
}


def column_spec_block(schema: TableSchema, columns: list[str] | None = None) -> str:
    """The per-column extraction rules: the schema's own prompt, plus our hints."""
    wanted = set(columns) if columns else None
    blocks = []
    for column in schema.columns:
        if wanted is not None and column.name not in wanted:
            continue
        block = f"### {column.name}\n{column.prompt}"
        hint = COLUMN_HINTS.get(column.name)
        if hint:
            block += f"\n\nADDITIONAL GUIDANCE\n{hint}"
        blocks.append(block)
    return "\n\n".join(blocks)


def layout_instructions(schema: TableSchema) -> str:
    """System-level framing for the layout-discovery call."""
    group_col = schema.group_column
    doc_value_columns = dict.fromkeys(
        [c.name for c in schema.doc_columns] + list(schema.backfill_columns)
    )
    doc_values_example = ", ".join(f'"{name}": "..."' for name in doc_value_columns) or '"...": "..."'
    return f"""You are analysing the first pages of a {schema.document_label} to
map its structure. You are NOT extracting the {schema.row_label} table yet.

{_verbatim_clause(schema.key_columns)}

Return one JSON object and nothing else:

{{"present_columns": {{"<target column name>": "<the exact source header or label in this document>"}},
  "absent_columns": [<target column names this document does not carry>],
  "policy_number_placement": "column" | "block_header" | "document_header",
  "row_granularity": "<what one row of the main table represents>",
  "date_format": "<the date format used, e.g. MM/DD/YYYY>",
  "currency_format": "<how amounts are written, e.g. $1,234.56 or (1,234.56) for negatives>",
  "table_starts_on_page": <1-based page number where the {schema.row_label} table begins>,
  "document_values": {{{doc_values_example}}},
  "reported_row_count": <the total number of {schema.row_label} rows this
            {schema.document_label} contains, only if you can state it with
            confidence — either these pages are the whole document and you
            counted them directly, or a summary/total line on these pages
            states the count explicitly. null otherwise; never guess>,
  "notes": "<anything a later reader of continuation pages would need, such as a
            repeating header, a subtotal row pattern, or a two-line row layout>"}}

`policy_number_placement` matters: say "block_header" when {group_col} sits
above a group of rows rather than in its own column, because later pages will
not repeat it.

Target columns and their definitions:

{column_spec_block(schema)}"""


def layout_prompt(
    page_count: int,
    total_pages: int,
    context_text: str = "",
    document_label: str = "insurance loss-run report",
    row_label: str = "claim",
) -> str:
    prompt = (
        f"The attached PDF is the first {page_count} page(s) of a "
        f"{total_pages}-page {document_label}. Map its structure and return the "
        f"JSON object described in the instructions."
    )
    if page_count >= total_pages:
        prompt += (
            f"\n\nThese pages are the complete document, so you can count its "
            f"{row_label} rows directly for `reported_row_count`."
        )
    else:
        prompt += (
            "\n\nThese are only the opening pages of a longer document. Set "
            "`reported_row_count` only if a summary or total line on these pages "
            "states the count explicitly — otherwise return null rather than "
            "guessing from what little you can see."
        )
    if context_text.strip():
        prompt += (
            "\n\nThe document arrived by email. Use this only for the "
            "document-level values if the PDF itself does not state them:\n\n"
            + _clip(context_text, 4000)
        )
    return prompt


def extraction_instructions(schema: TableSchema, layout: dict) -> str:
    """System-level framing for a table-extraction call."""
    row_columns = [c.name for c in schema.row_columns]
    return f"""You extract the complete {schema.row_label} table from a {schema.document_label}.

{_verbatim_clause(schema.key_columns)}

COMPLETENESS — this is the failure that matters most: extract EVERY {schema.row_label} row
on the attached pages, from the first to the last. Do not sample, do not
summarize, do not stop early because the table is long, and never skip rows in
the middle. Every row present in the pages must appear in your output.

Extract all {len(row_columns)} columns for every row in a single pass. Do not
return one column at a time.

{_output_contract(schema.key_columns)}

Emit exactly these columns, in this order:
{json.dumps(row_columns)}

DOCUMENT LAYOUT — established from the first pages of this document:
{json.dumps(layout, indent=2)}

Column definitions:

{column_spec_block(schema, row_columns)}"""


def extraction_prompt(
    *,
    chunk_index: int,
    chunk_total: int,
    start_page: int,
    end_page: int,
    total_pages: int,
    anchors: list[list[str]],
    resume_after: list[str] | None = None,
    key_columns: tuple[str, ...] = (),
    document_label: str = "insurance loss-run report",
    row_label: str = "claim",
) -> str:
    """The per-chunk user prompt: position, seam anchors, and resume state."""
    if chunk_total == 1:
        lines = [
            f"The attached PDF is the complete {total_pages}-page {document_label}.",
            f"Extract every {row_label} row it contains.",
        ]
    else:
        lines = [
            f"The attached PDF is chunk {chunk_index} of {chunk_total} from a "
            f"{total_pages}-page {document_label}: pages {start_page}-{end_page}.",
        ]
        if chunk_index > 1:
            lines.append(
                f"The {row_label} table continues from the previous chunk. These "
                "pages may not repeat the table header — use the layout given in "
                "the instructions to map the columns."
            )
        lines.append(
            "Chunks overlap by a few pages, so the first rows on these pages may "
            "already have been extracted."
        )

    if anchors:
        key_desc = ", ".join(key_columns) if key_columns else "the row key"
        lines.append(
            "\nAlready extracted by the previous chunk — do NOT emit these rows "
            f"again. Match on [{key_desc}] and begin after the last of them:\n"
            + json.dumps(anchors, indent=1)
        )

    if resume_after:
        lines.append(
            "\nRESUMING an interrupted extraction of these same pages. You already "
            f"emitted every row up to and including {json.dumps(resume_after)}. "
            "Start from the row immediately after it and continue to the end. Do "
            "not re-emit anything before it."
        )

    return "\n".join(lines)


def qa_instructions(schema: TableSchema) -> str:
    """System-level framing for a QA review call.

    The review is told what the cleaning stage already did to the values it is
    looking at. Without that it reports every rendered date and stripped currency
    symbol as an error, and the real findings drown in the noise.
    """
    row_columns = [c.name for c in schema.row_columns]
    key_example = _key_example(schema.key_columns)
    doc_level_names = ", ".join(c.name for c in schema.doc_columns) or "a document-level value"
    return f"""You are auditing a {schema.row_label} table that has already been extracted
from a {schema.document_label}. The attached PDF is the exact pages those rows
were read from. You are NOT extracting the table again.

Your job is to find cells that are WRONG, and to say what the document actually
prints in their place.

{_verbatim_clause(schema.key_columns)}

WHAT COUNTS AS WRONG
- A value that does not appear in the document for that row — an invented
  figure, a name or number the page does not carry.
- A value read out of the wrong column, or off a neighbouring row.
- A value the document prints differently: a changed digit or letter, a dropped
  or added leading zero, a "corrected" spelling. `C003` read as `C0000003` and
  `McAllister` read as `MacAllister` are both errors.
- A cell carrying a value where the document prints none for that row.

WHAT IS NOT WRONG — do not report these:
- Formatting. The values below have already been rendered into a fixed form:
  dates as MM/DD/YYYY, amounts as plain decimals with currency symbols,
  thousands separators and trailing zeros removed, and accounting parentheses
  turned into a minus sign. `$1,200.00` correctly appears below as `1200`, and
  `(450.75)` as `-450.75`. A difference that is only formatting is not a finding.
- `N/A` in a cell the document genuinely leaves empty for that row.
- A document-level value repeated on every row — {doc_level_names} are meant to
  repeat.

PROPOSING A CORRECTION
- `correct_value` must be copied from the attached pages character for character.
  Never propose a value you cannot point at on the page. If you believe a cell is
  wrong but cannot read what belongs there, leave it out rather than guessing.
- Use `N/A` as `correct_value` when the document prints nothing for that cell.
- `row` identifies which row you are correcting: give its [{key_example}]
  exactly as they appear in the table below, even when one of those is itself
  the cell you are correcting.

MISSING ROWS
Also list any {schema.row_label} row printed on the attached pages that is
absent from the table below. These are reported to a human rather than added
to the table, so give only the row key and why you believe it was missed.

{_qa_output_contract(schema.key_columns)}

The columns under review, in the order the rows below use:
{json.dumps(row_columns)}

Column definitions:

{column_spec_block(schema, row_columns)}"""


def qa_prompt(
    *,
    chunk_index: int,
    chunk_total: int,
    start_page: int,
    end_page: int,
    total_pages: int,
    columns: list[str],
    rows: list[list[str]],
    batch_index: int = 1,
    batch_total: int = 1,
    document_label: str = "insurance loss-run report",
) -> str:
    """The per-chunk user prompt: which pages, and the rows read from them."""
    if chunk_total == 1:
        lines = [f"The attached PDF is the complete {total_pages}-page {document_label}."]
    else:
        lines = [
            f"The attached PDF is pages {start_page}-{end_page} of a "
            f"{total_pages}-page {document_label} (chunk {chunk_index} of "
            f"{chunk_total})."
        ]

    if batch_total > 1:
        lines.append(
            f"The rows read from these pages are being reviewed in "
            f"{batch_total} parts; this is part {batch_index}. Audit only the rows "
            f"given here. A row printed on these pages but absent below may simply "
            f"belong to another part, so report it as missing only if you are "
            f"confident it was never extracted."
        )

    lines.append(
        "\nBelow is the table already extracted from these pages, as a positional "
        "array per row. Check every cell against the pages and report only what is "
        "wrong."
    )
    lines.append(json.dumps({"columns": columns, "rows": rows}))
    return "\n".join(lines)


def text_extraction_prompt(text: str, chunk_label: str = "", row_label: str = "claim") -> str:
    """Prompt for a text-only source (an email body or a spreadsheet)."""
    header = f"The content below is {chunk_label}. " if chunk_label else ""
    return (
        f"{header}Extract every {row_label} row it contains.\n\n"
        f"--- BEGIN DOCUMENT ---\n{text}\n--- END DOCUMENT ---"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"
