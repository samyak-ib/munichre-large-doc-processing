"""Defensive parsing of the model's JSON table payload.

The API ignores `text.format`, so JSON conformance is a prompt request rather than
a guarantee. Output that stops mid-array — the signature of an exhausted output
budget — is salvaged down to the last complete row and reported as truncated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class TablePayload:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    truncated: bool = False
    last_row_key: list[str] = field(default_factory=list)
    repaired: bool = False


class PayloadError(ValueError):
    """The response carried nothing usable as a table."""


def parse_table_payload(text: str) -> TablePayload:
    """Parse a table response, repairing a truncated array when possible."""
    candidate = strip_fences(text)
    if not candidate.strip():
        raise PayloadError("empty response body")

    try:
        return _from_object(json.loads(candidate), repaired=False)
    except json.JSONDecodeError:
        pass

    obj = first_json_object(candidate)
    if obj is not None:
        try:
            return _from_object(json.loads(obj), repaired=False)
        except json.JSONDecodeError:
            candidate = obj

    repaired = _repair_truncated(candidate)
    if repaired is None:
        raise PayloadError("response is not JSON and could not be repaired")
    payload = _from_object(repaired, repaired=True)
    payload.truncated = True
    return payload


def strip_fences(text: str) -> str:
    match = FENCE_RE.search(text)
    return match.group(1) if match else text


def first_json_object(text: str) -> str | None:
    """Return the first balanced {...} span, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair_truncated(text: str) -> dict | None:
    """Rebuild a payload from a cut-off response by keeping whole rows only."""
    columns = _extract_columns(text)
    rows_start = _rows_array_start(text)
    if rows_start is None:
        return None
    rows = _complete_rows(text, rows_start)
    if not rows:
        return None
    return {"columns": columns, "rows": rows, "truncated": True}


def _extract_columns(text: str) -> list[str]:
    marker = text.find('"columns"')
    if marker < 0:
        return []
    start = text.find("[", marker)
    if start < 0:
        return []
    end = text.find("]", start)
    if end < 0:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [str(c) for c in parsed]


def _rows_array_start(text: str) -> int | None:
    marker = text.find('"rows"')
    if marker < 0:
        return None
    start = text.find("[", marker)
    return start if start >= 0 else None


def _complete_rows(text: str, rows_start: int) -> list[list[str]]:
    """Collect every fully-closed row array inside the rows array."""
    rows: list[list[str]] = []
    depth = 0
    in_string = False
    escaped = False
    row_start = -1
    for i in range(rows_start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            if depth == 0:
                row_start = i
            depth += 1
        elif ch == "]":
            if depth == 0:
                break  # end of the rows array itself
            depth -= 1
            if depth == 0 and row_start >= 0:
                try:
                    parsed = json.loads(text[row_start : i + 1])
                except json.JSONDecodeError:
                    row_start = -1
                    continue
                rows.append([_as_text(v) for v in parsed])
                row_start = -1
    return rows


def _from_object(obj: object, *, repaired: bool) -> TablePayload:
    if not isinstance(obj, dict):
        raise PayloadError("response JSON is not an object")
    raw_rows = obj.get("rows")
    if not isinstance(raw_rows, list):
        raise PayloadError("response JSON has no `rows` array")
    rows: list[list[str]] = []
    for row in raw_rows:
        if isinstance(row, list):
            rows.append([_as_text(v) for v in row])
    columns = [str(c) for c in obj.get("columns") or []]
    key = [_as_text(v) for v in obj.get("last_row_key") or []]
    return TablePayload(
        columns=columns,
        rows=rows,
        truncated=bool(obj.get("truncated")),
        last_row_key=key,
        repaired=repaired,
    )


def _as_text(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()
