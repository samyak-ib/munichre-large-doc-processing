"""Post-processing the extracted table into the golden set's representation.

Scope is deliberately narrow. Golden stores dates as real dates and amounts as
real numbers, so those have exactly one correct rendering and are safe to
rewrite. Everything else in the golden set is recorded **as-is** — `Claim Status`
appears as `CLOSED`, `C`, `Closed` and `C (Closed)`; `Loss State` as both `TX`
and `Texas` — so canonicalizing those would turn matching rows into mismatches.

Semantic equivalence for those columns belongs in the comparison, not here: see
`accuracy.values_match`.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .schema_loader import DATE_COLUMNS, MONEY_COLUMNS, TableSchema

NA = "N/A"
GOLDEN_DATE_FORMAT = "%m/%d/%Y"

_WS_RE = re.compile(r"\s+")
_EMPTY = {"", "n/a", "na", "none", "null", "-", "--", "nan"}

# Formats seen from the models, most specific first. Two-digit years are
# ambiguous and handled last so `01/02/2023` never parses as day-first.
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
    "%m/%d/%y",
)

_MONEY_STRIP_RE = re.compile(r"[^0-9.\-]")


def clean_table(rows: list[dict[str, str]], schema: TableSchema) -> list[dict[str, str]]:
    """Return the table with every cell rendered the way golden records it."""
    return [clean_row(row, schema) for row in rows]


def clean_row(row: dict[str, str], schema: TableSchema) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for column in schema.columns:
        cleaned[column.name] = clean_value(column.name, row.get(column.name, ""))
    return cleaned


def clean_value(column: str, value: object) -> str:
    """Clean one cell. Unparseable input is returned as-is, never discarded."""
    text = _collapse(value)
    if text.lower() in _EMPTY:
        return NA
    if column in DATE_COLUMNS:
        return _format_date(text)
    if column in MONEY_COLUMNS:
        return _format_money(text)
    return text


def _collapse(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime(GOLDEN_DATE_FORMAT)
    if isinstance(value, date):
        return value.strftime(GOLDEN_DATE_FORMAT)
    return _WS_RE.sub(" ", str(value)).strip()


def _format_date(text: str) -> str:
    """Render a date as MM/DD/YYYY, the form golden stores.

    A value that parses in no known format is kept verbatim — losing it would
    hide a real extraction from the score.
    """
    stripped = text.split(" ")[0] if _looks_like_timestamp(text) else text
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(stripped, fmt).strftime(GOLDEN_DATE_FORMAT)
        except ValueError:
            continue
    return text


def _looks_like_timestamp(text: str) -> bool:
    return ":" in text and " " in text


def _format_money(text: str) -> str:
    """Render an amount as a plain decimal, dropping currency decoration.

    Parenthesised amounts are the accounting form for negatives. A trailing
    `.0` is dropped so a whole number reads as golden writes it.
    """
    negative = text.strip().startswith("(") and text.strip().endswith(")")
    stripped = _MONEY_STRIP_RE.sub("", text)
    if not stripped or stripped in {"-", ".", "-."}:
        return text
    try:
        number = float(stripped)
    except ValueError:
        return text
    if negative:
        number = -abs(number)
    if number == int(number):
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")
