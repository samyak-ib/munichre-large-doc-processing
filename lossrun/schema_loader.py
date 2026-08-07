"""Resolves schema.json into the flat column contract the extractor works against.

The schema's `Table` field is a UDF that joins several OBJECT_LIST fields plus the
document-level Valuation Date. Reading that join here keeps schema.json the single
source of truth for both the column set and the per-column extraction prompts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

TABLE_FIELD_NAME = "Table"

# Scalars that describe the report rather than a claim. Extracted once during
# layout discovery and stamped onto every row.
DOC_LEVEL_COLUMNS = ("Insured",)

# Columns layout discovery reports as document values and that also travel with
# the rows. Valuation Date is here rather than in DOC_LEVEL_COLUMNS because a
# bundle of loss runs from several carriers carries one as-of date per section:
# the extraction pass emits it per row, and the layout value only backfills rows
# that came back without one.
BACKFILL_FROM_LAYOUT = ("Insured", "Valuation Date")

# The row identity used for merging and for matching against golden data.
KEY_COLUMNS = ("Policy Number", "Claim Number", "Claimant Name")

# Columns whose values are monetary amounts.
MONEY_COLUMNS = (
    "Indemnity Paid",
    "Indemnity Reserve",
    "Expense Paid",
    "Expense Reserved",
    "Recovery Salvage Subro Reins",
    "Recovery Deductible",
    "Claim Total",
    "Policy Total",
)

DATE_COLUMNS = (
    "Accident Date",
    "Report Date",
    "Closed Date",
    "Policy Effective Date",
    "Policy Expiration Date",
    "Valuation Date",
)

DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.json"


@dataclass(frozen=True)
class Column:
    name: str
    prompt: str
    doc_level: bool


@dataclass(frozen=True)
class TableSchema:
    columns: tuple[Column, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def row_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.doc_level)

    @property
    def doc_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.doc_level)

    def prompt_bytes(self) -> int:
        return sum(len(c.name) + len(c.prompt) for c in self.columns)


def load_schema(path: Path | None = None) -> TableSchema:
    """Resolve the Table UDF join into an ordered, deduplicated column list."""
    schema_path = path or DEFAULT_SCHEMA_PATH
    raw = json.loads(schema_path.read_text())

    classes = raw.get("classes") or {}
    if not classes:
        raise ValueError(f"{schema_path} has no classes")
    # The schema ships a single class; take it without hardcoding its id.
    klass = next(iter(classes.values()))
    fields = klass.get("fields") or []
    by_id = {f["id"]: f for f in fields}
    by_name = {f["name"]: f for f in fields}

    table = by_name.get(TABLE_FIELD_NAME)
    if table is None:
        raise ValueError(f"{schema_path} has no `{TABLE_FIELD_NAME}` field")

    ordered: list[Column] = []
    seen: set[str] = set()

    def add(name: str, prompt: str) -> None:
        if name in seen:
            return
        seen.add(name)
        ordered.append(
            Column(name=name, prompt=prompt.strip(), doc_level=name in DOC_LEVEL_COLUMNS)
        )

    for arg in table.get("function_args") or []:
        field = by_id.get(arg.get("field_id"))
        if field is None:
            continue
        sub_fields = field.get("prompt_schema")
        if sub_fields:
            for sub in sub_fields:
                add(sub["name"], sub.get("description", ""))
        else:
            # A scalar field joined onto every row (Valuation Date).
            add(field["name"], field.get("description", ""))

    # Insured describes the report and is not part of the Table UDF's arguments,
    # but the final table carries it on every row.
    for name in DOC_LEVEL_COLUMNS:
        field = by_name.get(name)
        if field is not None:
            add(name, field.get("description", ""))

    return TableSchema(columns=tuple(ordered))
