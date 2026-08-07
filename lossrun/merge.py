"""Row-key merge across chunks, plus value normalization.

Chunks overlap, so the same claim can arrive twice. Merging is keyed on
(Policy Number, Claim Number, Claimant Name) — the same primary key used to match
against golden data. Disagreements are recorded, never silently resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import RawRow
from .schema_loader import BACKFILL_FROM_LAYOUT, KEY_COLUMNS, TableSchema

NA = "N/A"
_EMPTY_VALUES = {"", "n/a", "na", "none", "null", "-", "--"}
_WS_RE = re.compile(r"\s+")


@dataclass
class Conflict:
    key: tuple[str, ...]
    column: str
    kept: str
    discarded: str
    kept_from: str
    discarded_from: str


@dataclass
class MergeResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    duplicate_keys: int = 0

    def by_key(self) -> dict[tuple[str, ...], dict[str, str]]:
        return {normalize_key(r): r for r in self.rows}


def is_empty(value: str | None) -> bool:
    return value is None or value.strip().lower() in _EMPTY_VALUES


def normalize_key(values: dict[str, str]) -> tuple[str, ...]:
    """Case- and whitespace-insensitive row identity.

    Whitespace is removed outright rather than collapsed: models disagree on
    whether a wrapped identifier is `001-WC19A-78355` or `001- WC19A-78355`, and
    treating those as different claims splits one row into two. Only the key used
    for merging and diffing is stripped; the printed value keeps its spacing.
    """
    return tuple(_key_part(values.get(name, "")) for name in KEY_COLUMNS)


def _key_part(value: str) -> str:
    if is_empty(value):
        return ""
    return _WS_RE.sub("", value).casefold()


def merge_rows(raw_rows: list[RawRow], schema: TableSchema) -> MergeResult:
    """Collapse rows sharing a key, keeping the first non-empty value per cell."""
    result = MergeResult()
    merged: dict[tuple[str, ...], dict[str, str]] = {}
    provenance: dict[tuple[str, ...], dict[str, str]] = {}
    order: list[tuple[str, ...]] = []

    for raw in raw_rows:
        key = normalize_key(raw.values)
        source = f"{raw.model} {raw.chunk}"
        if key not in merged:
            merged[key] = dict(raw.values)
            provenance[key] = {c: source for c in raw.values}
            order.append(key)
            continue

        result.duplicate_keys += 1
        target = merged[key]
        for column, value in raw.values.items():
            existing = target.get(column)
            if is_empty(existing):
                target[column] = value
                provenance[key][column] = source
            elif not is_empty(value) and _differs(existing, value):
                result.conflicts.append(
                    Conflict(
                        key=key,
                        column=column,
                        kept=existing,
                        discarded=value,
                        kept_from=provenance[key].get(column, ""),
                        discarded_from=source,
                    )
                )

    for key in order:
        result.rows.append(merged[key])
    return result


def _differs(a: str, b: str) -> bool:
    return _WS_RE.sub(" ", a).strip().casefold() != _WS_RE.sub(" ", b).strip().casefold()


def normalize_row(row: dict[str, str], schema: TableSchema) -> dict[str, str]:
    """Fill in every schema column and reduce empty markers to N/A.

    Rendering values into golden's representation is `cleaning.clean_row`'s job,
    which runs after the merge — this keeps one owner for formatting.
    """
    out: dict[str, str] = {}
    for column in schema.columns:
        value = row.get(column.name, "")
        out[column.name] = NA if is_empty(value) else _WS_RE.sub(" ", value).strip()
    return out


def finalize_rows(
    raw_rows: list[RawRow],
    schema: TableSchema,
    *,
    document_values: dict[str, str] | None = None,
    block_header_policy: bool = False,
    log=None,
) -> list[dict[str, str]]:
    """Turn one model's per-chunk rows into its final table.

    Merge the seams, carry a block-level policy number down where the layout
    says there is one, then fill the document-level values. Formatting is left
    to `cleaning.clean_table`, which the caller applies last.
    """
    merged = merge_rows(raw_rows, schema)
    rows = [normalize_row(r, schema) for r in merged.rows]
    if block_header_policy:
        filled = fill_block_policy_numbers(rows)
        if filled and log:
            log(f"  filled {filled} block-level policy numbers")
    stamp_document_values(rows, document_values or {}, schema)
    return rows


def stamp_document_values(
    rows: list[dict[str, str]], document_values: dict[str, str], schema: TableSchema
) -> None:
    """Fill Insured and Valuation Date from layout discovery, in place.

    Only rows that came back without a value are touched, so a valuation date the
    extraction pass read from a section header outranks the document-level one.
    """
    names = {n for n in BACKFILL_FROM_LAYOUT if n in set(schema.names)}
    for row in rows:
        for name in names:
            if is_empty(row.get(name)):
                value = document_values.get(name)
                row[name] = value.strip() if value and not is_empty(value) else NA


def fill_block_policy_numbers(rows: list[dict[str, str]]) -> int:
    """Carry a block-level policy number down to the rows beneath it.

    Used when layout discovery reports the policy number sits above a group of
    claim rows rather than in its own column. Returns how many rows were filled.
    """
    filled = 0
    current = ""
    for row in rows:
        value = row.get("Policy Number", "")
        if not is_empty(value):
            current = value
        elif current:
            row["Policy Number"] = current
            filled += 1
    return filled
