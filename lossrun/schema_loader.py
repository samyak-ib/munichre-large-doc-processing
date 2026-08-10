"""Resolves a table schema into the flat column contract the extractor works against.

Two input formats are accepted, detected from the file's own shape:

- The Instabase-style class definition (`schema.json`, a `classes` key) that
  shipped with the MRe loss-run engagement. Its `Table` field is a UDF that
  joins several OBJECT_LIST fields plus the document-level Valuation Date;
  reading that join here keeps the file the single source of truth for both
  the column set and the per-column extraction prompts.
- A plain column list (a `columns` key), for any other long-table extraction.
  Each entry names a column and, optionally, its prompt and role — whether it
  is part of the row's identity, a document-level value, a date or a money
  amount. See docs/COLUMNS.md for the format and a worked example.

Either way, the result is a `TableSchema` whose columns carry their own roles
(`.key`, `.identifier`, `.backfill`, `.match`, `.group`, `.type`) — nothing
downstream needs to know a column's name to know what it means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

TABLE_FIELD_NAME = "Table"

# --- the loss-run schema.json's column roles ---------------------------------
# schema.json carries no role information inline, so it is declared here once
# and stamped onto the resulting Column objects at load time. Kept as module
# constants (rather than only inline in the loader) because a handful of call
# sites still use them as their own default parameter, which is what lets a
# function like `verify_keys(rows, texts)` keep working with no schema in hand.

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

# Identifier columns verified verbatim against the document's text layer —
# KEY_COLUMNS plus any other column that names something rather than
# describing it. See verify.VERIFIED_COLUMNS for why Insurer Loss Run is not
# among them.
_LEGACY_IDENTIFIER_COLUMNS = KEY_COLUMNS + ("Occurrence ID",)

# The single column golden data is matched on. Not Policy Number: it repeats
# across every claim under one policy, so it cannot identify a row on its own.
_LEGACY_MATCH_COLUMN = "Claim Number"

# The column that can appear as a block header above a group of claim rows
# instead of in its own column — see merge.fill_block_policy_numbers.
_LEGACY_GROUP_COLUMN = "Policy Number"

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

VALID_COLUMN_TYPES = ("text", "date", "money")


@dataclass(frozen=True)
class Column:
    name: str
    prompt: str
    doc_level: bool = False
    # Part of the row's identity: merged on, and verified verbatim against the
    # document's text layer.
    key: bool = False
    # Verified verbatim against the document's text layer without being part
    # of the merge key (e.g. a secondary id). Every key column is one too.
    identifier: bool = False
    # Filled from layout discovery's document-level values when the row itself
    # comes back without one.
    backfill: bool = False
    # The single column golden rows are matched on. At most one column should
    # set this; the schema falls back to its first key column otherwise.
    match: bool = False
    # Can appear as a block header above a group of rows instead of in its own
    # column. At most one column should set this; the schema falls back to its
    # first key column otherwise.
    group: bool = False
    type: str = "text"  # "text" | "date" | "money"


@dataclass(frozen=True)
class TableSchema:
    columns: tuple[Column, ...]
    # The row's identity, in priority order — e.g. (Policy Number, Claim
    # Number, Claimant Name). Kept as its own field rather than derived by
    # filtering `columns` for `.key`, because that filter would follow the
    # schema's overall column order (which extraction and QA also rely on, for
    # the output contract), and the two orders are not the same thing: the loss
    # run's key order is Policy Number first, but schema.json's column order
    # puts Claim Number and Claimant Name first. Positional row-key arrays
    # (`last_row_key`, a QA finding's `row`) are zipped against this order, so
    # getting it wrong misassigns every value in them.
    key_columns: tuple[str, ...] = ()
    # What one row and the document as a whole are called, for the wording of
    # the prompts sent to the model. A loss run says "claim" and "insurance
    # loss-run report"; an unrelated table can say whatever it is.
    row_label: str = "row"
    document_label: str = "document"

    def __post_init__(self) -> None:
        if not self.key_columns:
            raise ValueError(
                "the schema declares no key column — mark at least one column "
                "as the row's identity (`key: true` in a column spec); it is "
                "used to merge chunks, verify rows against the document text, "
                "and match rows to golden data"
            )
        names = set(self.names)
        unknown = [k for k in self.key_columns if k not in names]
        if unknown:
            raise ValueError(f"key column(s) {unknown} are not among the schema's columns")
        doc_level_keys = [c.name for c in self.columns if c.doc_level and c.key]
        if doc_level_keys:
            raise ValueError(
                f"column(s) {doc_level_keys} are marked both `doc_level` and `key` "
                "— a document-level value is never emitted per row, so it cannot "
                "be part of the merge key"
            )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    @property
    def row_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if not c.doc_level)

    @property
    def doc_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.doc_level)

    @property
    def identifier_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.key or c.identifier)

    @property
    def backfill_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.backfill)

    @property
    def money_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.type == "money")

    @property
    def date_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.type == "date")

    @property
    def match_column(self) -> str:
        """The column golden rows are matched on.

        The column flagged `match: true`, or the first key column when none
        is — which is the right default for a fresh column list: declare key
        columns in priority order and the most specific one need not be
        singled out unless, as with the loss run's Policy Number, the natural
        first key repeats across rows and cannot identify one on its own.
        """
        flagged = next((c.name for c in self.columns if c.match), None)
        return flagged or self.key_columns[0]

    @property
    def group_column(self) -> str:
        """The column that may sit as a block header above a group of rows.

        Same fallback as `match_column`: the column flagged `group: true`, or
        the first key column otherwise.
        """
        flagged = next((c.name for c in self.columns if c.group), None)
        return flagged or self.key_columns[0]

    def prompt_bytes(self) -> int:
        return sum(len(c.name) + len(c.prompt) for c in self.columns)


def load_schema(path: Path | None = None) -> TableSchema:
    """Load a table schema from either format, detected from its own shape."""
    schema_path = path or DEFAULT_SCHEMA_PATH
    raw = _read_structured(schema_path)

    if "classes" in raw:
        return _load_legacy_schema(raw, schema_path)
    if "columns" in raw:
        return _load_column_spec(raw, schema_path)
    raise ValueError(
        f"{schema_path} is neither an Instabase schema.json (expected a top-level "
        f"`classes` key) nor a column-list spec (expected a top-level `columns` "
        f"key) — see docs/COLUMNS.md"
    )


def _read_structured(path: Path) -> dict:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON/YAML object at the top level")
    return data


def _load_legacy_schema(raw: dict, schema_path: Path) -> TableSchema:
    """Resolve the Table UDF join in an Instabase schema.json into a TableSchema."""
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

    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, prompt: str) -> None:
        if name in seen:
            return
        seen.add(name)
        ordered.append((name, prompt.strip()))

    for arg in table.get("function_args") or []:
        field_def = by_id.get(arg.get("field_id"))
        if field_def is None:
            continue
        sub_fields = field_def.get("prompt_schema")
        if sub_fields:
            for sub in sub_fields:
                add(sub["name"], sub.get("description", ""))
        else:
            # A scalar field joined onto every row (Valuation Date).
            add(field_def["name"], field_def.get("description", ""))

    # Insured describes the report and is not part of the Table UDF's arguments,
    # but the final table carries it on every row.
    for name in DOC_LEVEL_COLUMNS:
        field_def = by_name.get(name)
        if field_def is not None:
            add(name, field_def.get("description", ""))

    columns = tuple(
        Column(
            name=name,
            prompt=prompt,
            doc_level=name in DOC_LEVEL_COLUMNS,
            key=name in KEY_COLUMNS,
            identifier=name in _LEGACY_IDENTIFIER_COLUMNS,
            backfill=name in BACKFILL_FROM_LAYOUT,
            match=name == _LEGACY_MATCH_COLUMN,
            group=name == _LEGACY_GROUP_COLUMN,
            type=("money" if name in MONEY_COLUMNS else "date" if name in DATE_COLUMNS else "text"),
        )
        for name, prompt in ordered
    )
    return TableSchema(
        columns=columns,
        key_columns=KEY_COLUMNS,
        row_label="claim",
        document_label="insurance loss-run report",
    )


def _load_column_spec(raw: dict, schema_path: Path) -> TableSchema:
    """Build a TableSchema from a plain column list.

    Every entry is either a bare name or a small object:

        columns:
          - name: Invoice Number
            key: true                 # part of the row's identity
          - name: Amount
            type: money                # "text" (default) | "date" | "money"
          - Vendor                     # a bare string: defaults apply, and a
                                        # generic extraction prompt is used

    See docs/COLUMNS.md for the full field list and a worked example.
    """
    items = raw.get("columns") or []
    if not items:
        raise ValueError(f"{schema_path} declares no columns")

    columns: list[Column] = []
    key_columns: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError(f"{schema_path}: every column needs at least a `name`")

        name = str(item["name"]).strip()
        if name in seen:
            raise ValueError(f"{schema_path}: duplicate column {name!r}")
        seen.add(name)

        col_type = str(item.get("type", "text")).strip().lower()
        if col_type not in VALID_COLUMN_TYPES:
            raise ValueError(
                f"{schema_path}: column {name!r} has type {col_type!r}, expected "
                f"one of {', '.join(VALID_COLUMN_TYPES)}"
            )

        is_key = bool(item.get("key", False))
        is_doc_level = bool(item.get("doc_level", False))
        prompt = str(item.get("prompt") or item.get("description") or "").strip() or _default_prompt(name)
        if is_key:
            key_columns.append(name)

        columns.append(
            Column(
                name=name,
                prompt=prompt,
                doc_level=is_doc_level,
                key=is_key,
                identifier=bool(item.get("identifier", False)),
                backfill=bool(item.get("backfill", is_doc_level)),
                match=bool(item.get("match", False)),
                group=bool(item.get("group", False)),
                type=col_type,
            )
        )

    return TableSchema(
        columns=tuple(columns),
        key_columns=tuple(key_columns),
        row_label=str(raw.get("row_label") or "row").strip(),
        document_label=str(raw.get("document_label") or "document").strip(),
    )


def _default_prompt(name: str) -> str:
    return f'Extract the value of "{name}" exactly as it is printed for this row.'
