"""Excel output: the per-document workbook and the cumulative telemetry ledger."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .accuracy import ACCURACY_COLUMNS, COLUMN_SCORE_COLUMNS, MISMATCH_COLUMNS
from .telemetry import CALL_COLUMNS, SUMMARY_COLUMNS, CallRecord

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
MAX_COLUMN_WIDTH = 60
ISSUE_COLUMNS = ("severity", "category", "detail", "row_key", "column", "value", "source")


def write_workbook(
    path: Path,
    *,
    final_rows: Sequence[dict[str, Any]],
    final_columns: Sequence[str],
    raw_rows: Sequence[dict[str, Any]],
    raw_columns: Sequence[str],
    issues: Sequence[dict[str, Any]],
    calls: Sequence[CallRecord],
    summary: dict[str, Any],
    accuracy: Sequence[dict[str, Any]] = (),
    column_scores: Sequence[dict[str, Any]] = (),
    mismatches: Sequence[dict[str, Any]] = (),
) -> None:
    """One workbook per document: the table, its audit trail, and this run's cost."""
    workbook = Workbook()
    workbook.remove(workbook.active)

    _write_sheet(
        workbook.create_sheet("Final Table"),
        list(final_columns),
        [[row.get(c, "") for c in final_columns] for row in final_rows],
    )
    _write_sheet(
        workbook.create_sheet("Raw Rows"),
        list(raw_columns),
        [[row.get(c, "") for c in raw_columns] for row in raw_rows],
    )
    _write_sheet(
        workbook.create_sheet("Issues"),
        list(ISSUE_COLUMNS),
        [[issue.get(c, "") for c in ISSUE_COLUMNS] for issue in issues],
    )

    if accuracy:
        sheet = workbook.create_sheet("Accuracy")
        _write_sheet(
            sheet,
            list(ACCURACY_COLUMNS),
            [[row.get(c, "") for c in ACCURACY_COLUMNS] for row in accuracy],
        )
        start = len(accuracy) + 3
        _write_sheet(
            sheet,
            list(COLUMN_SCORE_COLUMNS),
            [[row.get(c, "") for c in COLUMN_SCORE_COLUMNS] for row in column_scores],
            start_row=start,
        )
        _write_sheet(
            workbook.create_sheet("Accuracy Mismatches"),
            list(MISMATCH_COLUMNS),
            [[row.get(c, "") for c in MISMATCH_COLUMNS] for row in mismatches],
        )

    telemetry_sheet = workbook.create_sheet("Telemetry")
    _write_summary_block(telemetry_sheet, summary)
    start_row = len(summary) + 3
    _write_sheet(
        telemetry_sheet,
        list(CALL_COLUMNS),
        [call.as_row() for call in calls],
        start_row=start_row,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def append_ledger(
    path: Path,
    summary: dict[str, Any],
    calls: Sequence[CallRecord],
    accuracy: Sequence[dict[str, Any]] = (),
    column_scores: Sequence[dict[str, Any]] = (),
) -> None:
    """Append this run to the cross-run ledger, creating it on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        workbook = load_workbook(path)
        runs = workbook["Runs"]
        call_sheet = workbook["Calls"]
        # A ledger written before a column was added still carries the old
        # header. Appending the new row shape under it would shift every value
        # past the insertion point, corrupting exactly the cross-run comparison
        # this file exists for.
        _migrate_header(runs, list(SUMMARY_COLUMNS))
        _migrate_header(call_sheet, list(CALL_COLUMNS))
        acc_sheet = _ensure_sheet(workbook, "Accuracy", list(ACCURACY_COLUMNS))
        col_sheet = _ensure_sheet(workbook, "Accuracy By Column", list(COLUMN_SCORE_COLUMNS))
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)
        runs = workbook.create_sheet("Runs")
        _write_header(runs, list(SUMMARY_COLUMNS))
        call_sheet = workbook.create_sheet("Calls")
        _write_header(call_sheet, list(CALL_COLUMNS))
        acc_sheet = _ensure_sheet(workbook, "Accuracy", list(ACCURACY_COLUMNS))
        col_sheet = _ensure_sheet(workbook, "Accuracy By Column", list(COLUMN_SCORE_COLUMNS))

    runs.append([summary.get(c, "") for c in SUMMARY_COLUMNS])
    for call in calls:
        call_sheet.append(call.as_row())
    for row in accuracy:
        acc_sheet.append([row.get(c, "") for c in ACCURACY_COLUMNS])
    for row in column_scores:
        col_sheet.append([row.get(c, "") for c in COLUMN_SCORE_COLUMNS])

    _autosize(acc_sheet)
    _autosize(col_sheet)
    _autosize(runs)
    _autosize(call_sheet)
    workbook.save(path)


def read_batch_telemetry(
    path: Path, batch_ids: str | Iterable[str]
) -> tuple[list[dict[str, Any]], list[list[Any]], list[dict[str, Any]]]:
    """Accuracy rows, API calls, and per-run summaries for one or more batches.

    The ledger accumulates every run ever made; a results workbook covers the
    runs a reader cares about. Several batch ids are accepted because a batch
    that failed part-way is finished by a second invocation, and the report
    should still cover the whole set. Calls are matched through the run ids
    those batches produced, because a CallRecord carries a run id, not a batch.

    Run ids come from `Runs`, not `Accuracy` — a document with no golden entry
    (e.g. one sample with no ground truth) never appears in `Accuracy` at all,
    and its calls would otherwise be silently dropped from the ledger.

    The run summaries are the ledger's `Runs` sheet, one row per document —
    already carrying `SUMMARY_COLUMNS`' per-page token rates and per-document
    cost, computed once at run time by `Telemetry.summary`.
    """
    wanted = {batch_ids} if isinstance(batch_ids, str) else set(batch_ids)
    wanted.discard("")
    if not path.exists() or not wanted:
        return [], [], []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        accuracy = [
            row
            for row in _rows_of(workbook, "Accuracy")
            if str(row.get("batch_id", "")) in wanted
        ]
        runs = [
            row
            for row in _rows_of(workbook, "Runs")
            if str(row.get("batch_id", "")) in wanted
        ]
        run_ids = {str(row.get("run_id", "")) for row in runs}
        calls = [
            [row.get(c, "") for c in CALL_COLUMNS]
            for row in _rows_of(workbook, "Calls")
            if str(row.get("run_id", "")) in run_ids
        ]
    finally:
        workbook.close()
    return accuracy, calls, runs


def batch_ids_for(path: Path, run_dirs: Sequence[Path]) -> set[str]:
    """Batch ids covering the given run directories, matched on document name."""
    if not path.exists():
        return set()
    wanted = {d.name for d in run_dirs}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        found = set()
        for row in _rows_of(workbook, "Runs"):
            batch = str(row.get("batch_id", "") or "")
            if not batch:
                continue
            # Run directories are named <safe stem>_<timestamp>; the ledger
            # holds the original filename, so match on the stem it was built
            # from rather than on the whole name.
            document = str(row.get("document", ""))
            if any(name.rsplit("_", 1)[0] in _safe(document) for name in wanted):
                found.add(batch)
        return found
    finally:
        workbook.close()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("_")


def _rows_of(workbook, title: str) -> list[dict[str, Any]]:
    if title not in workbook.sheetnames:
        return []
    rows = list(workbook[title].iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c) if c is not None else "" for c in rows[0]]
    return [
        dict(zip(header, row)) for row in rows[1:] if any(v is not None for v in row)
    ]


def _ensure_sheet(workbook, title: str, header: list[str]) -> Worksheet:
    """Fetch a ledger sheet, creating it if this file predates it."""
    if title in workbook.sheetnames:
        sheet = workbook[title]
        _migrate_header(sheet, header)
        return sheet
    sheet = workbook.create_sheet(title)
    _write_header(sheet, header)
    return sheet


def _migrate_header(sheet: Worksheet, expected: list[str]) -> None:
    """Rewrite a ledger sheet under the current header, preserving its rows.

    Existing rows are re-keyed by their own header names, so a column added
    since the file was written appears blank on old rows instead of shifting
    every later value one place left.
    """
    header = [c.value for c in sheet[1]]
    while header and header[-1] is None:
        header.pop()
    if header == expected:
        return

    preserved = [
        dict(zip(header, row))
        for row in sheet.iter_rows(min_row=2, values_only=True)
        if any(v is not None for v in row)
    ]
    sheet.delete_rows(1, sheet.max_row)
    _write_header(sheet, expected)
    for row in preserved:
        sheet.append([row.get(column, "") for column in expected])


def _write_sheet(
    sheet: Worksheet,
    header: list[str],
    rows: Sequence[Sequence[Any]],
    *,
    start_row: int = 1,
) -> None:
    _write_header(sheet, header, row=start_row)
    for offset, row in enumerate(rows, start=start_row + 1):
        for col, value in enumerate(row, start=1):
            sheet.cell(row=offset, column=col, value=value)
    sheet.freeze_panes = sheet.cell(row=start_row + 1, column=1)
    _autosize(sheet)


def _write_header(sheet: Worksheet, header: list[str], *, row: int = 1) -> None:
    for col, name in enumerate(header, start=1):
        cell = sheet.cell(row=row, column=col, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")


def _write_summary_block(sheet: Worksheet, summary: dict[str, Any]) -> None:
    bold = Font(bold=True)
    for index, (key, value) in enumerate(summary.items(), start=1):
        label = sheet.cell(row=index, column=1, value=key)
        label.font = bold
        sheet.cell(row=index, column=2, value=value)


def _autosize(sheet: Worksheet) -> None:
    widths: dict[int, int] = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            length = min(len(str(cell.value)), MAX_COLUMN_WIDTH)
            widths[cell.column] = max(widths.get(cell.column, 0), length)
    for column, width in widths.items():
        sheet.column_dimensions[get_column_letter(column)].width = width + 2
