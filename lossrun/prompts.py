"""Prompt construction for layout discovery and table extraction.

Column semantics come verbatim from schema.json — this module frames them and
adds `COLUMN_HINTS`, the document-shape guidance measured against golden data.
The verbatim clause carries the anti-autocorrect load that reasoning effort would
otherwise carry, because the API ignores the `reasoning` parameter.
"""

from __future__ import annotations

import json

from .schema_loader import TableSchema

VERBATIM_CLAUSE = """TRANSCRIPTION RULES — these override every other instruction:
- Copy every value character for character exactly as printed. Never normalize,
  never expand, never abbreviate, never correct an apparent typo or misspelling.
- Claim numbers and claimant names are primary keys. Do not add or remove leading
  zeros, digits, spaces, or punctuation. `C003` is `C003`, never `C0000003`.
  `McAllister` is `McAllister`, never `MacAllister`. If a name looks misspelled,
  it is not misspelled — copy it as printed.
- Never invent a value. When a column has no value for a row, emit `N/A`.
- Never merge, split, reorder, sort, or deduplicate rows. Emit them in the order
  they appear in the document."""

OUTPUT_CONTRACT = """OUTPUT FORMAT — return one JSON object and nothing else. No prose,
no markdown fence, no explanation:

{"columns": [<the column names, in the order given below>],
 "rows": [[<value for each column, same order>], ...],
 "truncated": <true if you ran out of room before the last row, else false>,
 "last_row_key": [<Policy Number>, <Claim Number>, <Claimant Name>] of the final row you emitted}

Column names are declared once in `columns`; every row is a positional array in
that same order. Do not repeat column names inside rows. This format exists to
keep long tables inside the output budget — if you are running short of room, set
`truncated` to true and stop after a complete row rather than abbreviating."""


# Guidance added on top of schema.json, one entry per column that needed it.
# Each rule describes a document shape that loss runs actually use and that the
# schema's own wording does not cover; each was written after scoring against
# golden data showed the column failing for a structural reason rather than a
# transcription one. Kept separate from schema.json so this dict is exactly the
# delta to hand back for the class definition.
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
    """The per-column extraction rules: schema.json verbatim, plus our hints."""
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
    return f"""You are analysing the first pages of an insurance loss-run report to
map its structure. You are NOT extracting the claim table yet.

{VERBATIM_CLAUSE}

Return one JSON object and nothing else:

{{"present_columns": {{"<target column name>": "<the exact source header or label in this document>"}},
  "absent_columns": [<target column names this document does not carry>],
  "policy_number_placement": "column" | "block_header" | "document_header",
  "row_granularity": "<what one row of the main table represents>",
  "date_format": "<the date format used, e.g. MM/DD/YYYY>",
  "currency_format": "<how amounts are written, e.g. $1,234.56 or (1,234.56) for negatives>",
  "table_starts_on_page": <1-based page number where the claim table begins>,
  "document_values": {{"Insured": "...", "Valuation Date": "...", "Insurer Loss Run": "..."}},
  "notes": "<anything a later reader of continuation pages would need, such as a
            repeating header, a subtotal row pattern, or a two-line row layout>"}}

`policy_number_placement` matters: say "block_header" when the policy number sits
above a group of claim rows rather than in its own column, because later pages
will not repeat it.

Target columns and their definitions:

{column_spec_block(schema)}"""


def layout_prompt(page_count: int, total_pages: int, context_text: str = "") -> str:
    prompt = (
        f"The attached PDF is the first {page_count} page(s) of a "
        f"{total_pages}-page loss-run report. Map its structure and return the "
        f"JSON object described in the instructions."
    )
    if context_text.strip():
        prompt += (
            "\n\nThe report arrived by email. Use this only for the document-level "
            "values (insured, valuation date, insurer) if the PDF itself does not "
            "state them:\n\n" + _clip(context_text, 4000)
        )
    return prompt


def extraction_instructions(schema: TableSchema, layout: dict) -> str:
    """System-level framing for a table-extraction call."""
    row_columns = [c.name for c in schema.row_columns]
    return f"""You extract the complete claim table from an insurance loss-run report.

{VERBATIM_CLAUSE}

COMPLETENESS — this is the failure that matters most: extract EVERY claim row on
the attached pages, from the first to the last. Do not sample, do not summarize,
do not stop early because the table is long, and never skip rows in the middle.
Every row present in the pages must appear in your output.

Extract all {len(row_columns)} columns for every row in a single pass. Do not
return one column at a time.

{OUTPUT_CONTRACT}

Emit exactly these columns, in this order:
{json.dumps(row_columns)}

DOCUMENT LAYOUT — established from the first pages of this report:
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
) -> str:
    """The per-chunk user prompt: position, seam anchors, and resume state."""
    if chunk_total == 1:
        lines = [
            f"The attached PDF is the complete {total_pages}-page loss-run report.",
            "Extract every claim row it contains.",
        ]
    else:
        lines = [
            f"The attached PDF is chunk {chunk_index} of {chunk_total} from a "
            f"{total_pages}-page loss-run report: pages {start_page}-{end_page}.",
        ]
        if chunk_index > 1:
            lines.append(
                "The claim table continues from the previous chunk. These pages may "
                "not repeat the table header — use the layout given in the "
                "instructions to map the columns."
            )
        lines.append(
            "Chunks overlap by a few pages, so the first rows on these pages may "
            "already have been extracted."
        )

    if anchors:
        lines.append(
            "\nAlready extracted by the previous chunk — do NOT emit these rows "
            "again. Match on the row key [Policy Number, Claim Number, Claimant "
            "Name] and begin after the last of them:\n"
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


def text_extraction_prompt(text: str, chunk_label: str = "") -> str:
    """Prompt for a text-only source (an email body or a spreadsheet)."""
    header = f"The loss-run content below is {chunk_label}. " if chunk_label else ""
    return (
        f"{header}Extract every claim row it contains.\n\n"
        f"--- BEGIN DOCUMENT ---\n{text}\n--- END DOCUMENT ---"
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"
