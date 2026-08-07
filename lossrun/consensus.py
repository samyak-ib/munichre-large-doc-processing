"""Cross-model comparison.

Two models run the pipeline independently; this diffs their tables on the row key.
Nothing is arbitrated silently — the primary model's value is kept and every
disagreement is reported.

Agreement is not accuracy. Two models can agree and both be wrong, so the number
this produces is a proxy, reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .merge import is_empty, normalize_key
from .schema_loader import TableSchema


@dataclass
class CellConflict:
    key: tuple[str, ...]
    column: str
    primary_value: str
    other_value: str
    primary_model: str
    other_model: str


@dataclass
class ConsensusResult:
    rows: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[CellConflict] = field(default_factory=list)
    only_primary: list[tuple[str, ...]] = field(default_factory=list)
    only_other: list[tuple[str, ...]] = field(default_factory=list)
    agreement_by_column: dict[str, float] = field(default_factory=dict)
    compared_rows: int = 0

    @property
    def overall_agreement(self) -> float:
        if not self.agreement_by_column:
            return 0.0
        values = list(self.agreement_by_column.values())
        return sum(values) / len(values)


def compare(
    primary_rows: list[dict[str, str]],
    other_rows: list[dict[str, str]],
    *,
    schema: TableSchema,
    primary_model: str,
    other_model: str,
) -> ConsensusResult:
    """Diff two extractions of the same document on the row key."""
    result = ConsensusResult(rows=primary_rows)
    primary_by_key = {normalize_key(r): r for r in primary_rows}
    other_by_key = {normalize_key(r): r for r in other_rows}

    shared = [k for k in primary_by_key if k in other_by_key]
    result.only_primary = [k for k in primary_by_key if k not in other_by_key]
    result.only_other = [k for k in other_by_key if k not in primary_by_key]
    result.compared_rows = len(shared)

    matches: dict[str, int] = {}
    totals: dict[str, int] = {}

    for key in shared:
        left = primary_by_key[key]
        right = other_by_key[key]
        for column in schema.names:
            a = left.get(column, "")
            b = right.get(column, "")
            if is_empty(a) and is_empty(b):
                continue
            totals[column] = totals.get(column, 0) + 1
            if _equivalent(a, b):
                matches[column] = matches.get(column, 0) + 1
            else:
                result.conflicts.append(
                    CellConflict(
                        key=key,
                        column=column,
                        primary_value=a,
                        other_value=b,
                        primary_model=primary_model,
                        other_model=other_model,
                    )
                )

    result.agreement_by_column = {
        column: round(matches.get(column, 0) / total * 100, 1)
        for column, total in sorted(totals.items())
        if total
    }
    return result


def _equivalent(a: str, b: str) -> bool:
    if is_empty(a) and is_empty(b):
        return True
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()
