"""Side-by-side comparison of two or more sets of finished runs.

A route's ledger accumulates runs; the results workbook covers one batch. Neither
answers "did changing the pipeline help", which needs the same documents scored
under two configurations and put next to each other with their costs.

Everything here reads the workbooks the runs already wrote, so a comparison costs
no API calls and can be rebuilt whenever the scoring rules change.

Scoring targets the **shipped** `Final Table`, not the pre-merge `Raw Rows` that
`rescore` rebuilds from. That distinction is the whole point: QA corrections live
only in the final table, so rebuilding would score the extraction and silently
leave out the stage under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook, load_workbook

from .accuracy import AccuracyResult, load_golden, score
from .report import _write_sheet, _write_summary_block
from .schema_loader import TableSchema

FINAL_SHEET = "Final Table"
TELEMETRY_SHEET = "Telemetry"

# Per-document columns, in the order a reader wants them: what came back, how
# right it was, then what it cost.
DOCUMENT_COLUMNS = (
    "label",
    "document",
    "pages",
    "rows_golden",
    "rows_extracted",
    "rows_matched",
    "rows_missing",
    "rows_extra",
    "row_recall_pct",
    "row_precision_pct",
    "row_accuracy_matched_pct",
    "row_accuracy_overall_pct",
    "cell_accuracy_pct",
    "cells_correct",
    "cells_compared",
    "exact_row_pct",
    "calls",
    "input_tokens",
    "output_tokens",
    "input_tokens_per_page",
    "output_tokens_per_page",
    "cost_input_usd",
    "cost_output_usd",
    "cost_total_usd",
    "cost_per_page_usd",
    "cost_per_row_usd",
    "wall_clock_s",
)

# One row per API call: the finest telemetry there is, normalized by the pages
# that call actually carried rather than by the document's total.
CALL_DETAIL_COLUMNS = (
    "label",
    "document",
    "stage",
    "model",
    "call_label",
    "pages",
    "page_count",
    "status",
    "latency_s",
    "input_tokens",
    "output_tokens",
    "input_tokens_per_page",
    "output_tokens_per_page",
    "cost_input_usd",
    "cost_output_usd",
    "cost_total_usd",
)

# What a delta row reports. Rates are percentage-point differences; counts and
# dollars are plain differences.
DELTA_COLUMNS = (
    "document",
    "pages",
    "rows_golden",
    "rows_matched_a",
    "rows_matched_b",
    "row_recall_delta_pp",
    "row_accuracy_delta_pp",
    "cell_accuracy_a_pct",
    "cell_accuracy_b_pct",
    "cell_accuracy_delta_pp",
    "cells_correct_delta",
    "cost_a_usd",
    "cost_b_usd",
    "cost_delta_usd",
    "cost_delta_pct",
    "calls_a",
    "calls_b",
)


@dataclass
class DocumentComparison:
    """One document under one configuration."""

    label: str
    document: str
    run_dir: Path
    pages: int
    telemetry: dict[str, Any]
    accuracy: AccuracyResult | None

    def row(self) -> dict[str, Any]:
        rows_extracted = self.accuracy.rows_extracted if self.accuracy else 0
        cost = _number(self.telemetry.get("cost_total_usd"))
        out: dict[str, Any] = {
            "label": self.label,
            "document": self.document,
            "pages": self.pages,
            "calls": _number(self.telemetry.get("calls")),
            "input_tokens": _number(self.telemetry.get("input_tokens")),
            "output_tokens": _number(self.telemetry.get("output_tokens")),
            "cost_input_usd": round(_cost_input(self.telemetry), 6),
            "cost_output_usd": round(_cost_output(self.telemetry), 6),
            "cost_total_usd": round(cost, 6),
            "wall_clock_s": _number(self.telemetry.get("wall_clock_s")),
            "rows_extracted": rows_extracted,
        }
        if self.pages:
            out["input_tokens_per_page"] = round(out["input_tokens"] / self.pages)
            out["output_tokens_per_page"] = round(out["output_tokens"] / self.pages)
            out["cost_per_page_usd"] = round(cost / self.pages, 6)
        # Cost per row extracted, which is the figure that transfers to a
        # document nobody has scored: pages vary wildly in how many claims
        # they carry.
        if rows_extracted:
            out["cost_per_row_usd"] = round(cost / rows_extracted, 6)
        if self.accuracy is not None:
            out.update(
                {
                    "rows_golden": self.accuracy.rows_golden,
                    "rows_matched": self.accuracy.rows_matched,
                    "rows_missing": len(self.accuracy.missing_keys),
                    "rows_extra": len(self.accuracy.extra_keys),
                    "row_recall_pct": round(self.accuracy.row_recall, 1),
                    "row_precision_pct": round(self.accuracy.row_precision, 1),
                    "row_accuracy_matched_pct": round(self.accuracy.row_accuracy, 1),
                    "row_accuracy_overall_pct": round(self.accuracy.row_accuracy_overall, 1),
                    "cell_accuracy_pct": round(self.accuracy.cell_accuracy, 1),
                    "cells_correct": self.accuracy.cells_correct,
                    "cells_compared": self.accuracy.cells_compared,
                    "exact_row_pct": round(self.accuracy.exact_row_rate, 1),
                }
            )
        return out


@dataclass
class Group:
    label: str
    documents: list[DocumentComparison] = field(default_factory=list)

    def totals(self) -> dict[str, Any]:
        scored = [d for d in self.documents if d.accuracy is not None]
        cells = sum(d.accuracy.cells_compared for d in scored)
        correct = sum(d.accuracy.cells_correct for d in scored)
        golden = sum(d.accuracy.rows_golden for d in scored)
        matched = sum(d.accuracy.rows_matched for d in scored)
        extracted = sum(d.accuracy.rows_extracted for d in scored)
        exact = sum(d.accuracy.exact_rows for d in scored)
        # Pooled across every matched row in the set, so one document's 127 rows
        # do not weigh the same as another's single row.
        per_row = [a for d in scored for a in d.accuracy.row_accuracies]
        cost = sum(_number(d.telemetry.get("cost_total_usd")) for d in self.documents)
        pages = sum(d.pages for d in self.documents)
        return {
            "label": self.label,
            "documents": len(self.documents),
            "documents_scored": len(scored),
            "pages": pages,
            "rows_golden": golden,
            "rows_extracted": extracted,
            "rows_matched": matched,
            "row_recall_pct": round(matched / golden * 100, 1) if golden else 0.0,
            "row_precision_pct": round(matched / extracted * 100, 1) if extracted else 0.0,
            "row_accuracy_matched_pct": round(sum(per_row) / len(per_row), 1) if per_row else 0.0,
            "row_accuracy_overall_pct": round(sum(per_row) / golden, 1) if golden else 0.0,
            "cells_compared": cells,
            "cells_correct": correct,
            "cell_accuracy_pct": round(correct / cells * 100, 2) if cells else 0.0,
            "exact_row_pct": round(exact / golden * 100, 1) if golden else 0.0,
            "calls": sum(_number(d.telemetry.get("calls")) for d in self.documents),
            "input_tokens": sum(_number(d.telemetry.get("input_tokens")) for d in self.documents),
            "output_tokens": sum(_number(d.telemetry.get("output_tokens")) for d in self.documents),
            "cost_input_usd": round(sum(_cost_input(d.telemetry) for d in self.documents), 4),
            "cost_output_usd": round(sum(_cost_output(d.telemetry) for d in self.documents), 4),
            "cost_total_usd": round(cost, 4),
            "cost_per_page_usd": round(cost / pages, 6) if pages else 0.0,
            "cost_per_row_usd": round(cost / extracted, 6) if extracted else 0.0,
            "wall_clock_min": round(
                sum(_number(d.telemetry.get("wall_clock_s")) for d in self.documents) / 60, 1
            ),
        }


def run_dirs_under(path: Path) -> list[Path]:
    """Every run directory beneath `path`, or `path` itself if it is one."""
    if (path / "extraction.xlsx").exists():
        return [path]
    return sorted(p.parent for p in path.glob("*/extraction.xlsx"))


def collect(
    label: str, paths: Sequence[Path], schema: TableSchema, golden_path: Path
) -> Group:
    """Score every run under `paths` as it shipped, and read what it cost."""
    group = Group(label=label)
    for path in paths:
        for run_dir in run_dirs_under(path):
            workbook_path = run_dir / "extraction.xlsx"
            telemetry = read_summary(workbook_path)
            document = str(telemetry.get("document") or run_dir.name)
            costs = read_call_costs(workbook_path)
            if costs is not None:
                telemetry["cost_input_usd"], telemetry["cost_output_usd"] = costs
            rows = read_shipped_rows(workbook_path)
            golden = load_golden(golden_path, document, schema)
            model = str(telemetry.get("models") or "").split(",")[0].strip()
            group.documents.append(
                DocumentComparison(
                    label=label,
                    document=document,
                    run_dir=run_dir,
                    pages=int(_number(telemetry.get("pages"))),
                    telemetry=telemetry,
                    accuracy=score(rows, golden, schema, model) if golden else None,
                )
            )
    group.documents.sort(key=lambda d: d.document)
    return group


def read_shipped_rows(workbook_path: Path) -> list[dict[str, str]]:
    """The `Final Table` exactly as it was delivered."""
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if FINAL_SHEET not in workbook.sheetnames:
            return []
        rows = list(workbook[FINAL_SHEET].iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows:
        return []
    header = [str(c) if c is not None else "" for c in rows[0]]
    return [
        {name: ("" if value is None else str(value)) for name, value in zip(header, raw)}
        for raw in rows[1:]
        if any(v is not None for v in raw)
    ]


def read_calls(workbook_path: Path) -> list[dict[str, Any]]:
    """Every API call the run made, from the table under its summary block.

    The per-call rows are the finest telemetry there is: one row per request,
    carrying the stage that issued it, the pages it covered and its own token
    counts and cost.
    """
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if TELEMETRY_SHEET not in workbook.sheetnames:
            return []
        rows = list(workbook[TELEMETRY_SHEET].iter_rows(values_only=True))
    finally:
        workbook.close()

    for index, raw in enumerate(rows):
        header = [str(c) if c is not None else "" for c in raw]
        if "cost_input_usd" not in header or "cost_output_usd" not in header:
            continue
        return [
            dict(zip(header, call))
            for call in rows[index + 1 :]
            if call and any(v is not None for v in call)
        ]
    return []


def read_call_costs(workbook_path: Path) -> tuple[float, float] | None:
    """Exact input and output cost, summed from the run's own per-call rows.

    Every run records `cost_input_usd` and `cost_output_usd` per call, including
    runs made before the summary block carried the split — so a comparison can
    report a real breakdown rather than apportioning the total by token share,
    which would understate output at four times the input price.
    """
    calls = read_calls(workbook_path)
    if not calls:
        return None
    return (
        sum(_number(c.get("cost_input_usd")) for c in calls),
        sum(_number(c.get("cost_output_usd")) for c in calls),
    )


def call_rows(groups: list[Group]) -> list[dict[str, Any]]:
    """Per-call telemetry across every run, labelled and normalized per page.

    `pages` on a call is the page window it covered (`1-50`), so the per-page
    figures here divide by the pages that call actually carried rather than by
    the document's total — a layout call reading 5 header pages and an
    extraction call reading 50 are not the same unit.
    """
    out: list[dict[str, Any]] = []
    for group in groups:
        for document in group.documents:
            for call in read_calls(document.run_dir / "extraction.xlsx"):
                pages = _page_span(call.get("pages"))
                row = {
                    "label": group.label,
                    "document": document.document,
                    "stage": call.get("stage", ""),
                    "model": call.get("model", ""),
                    "call_label": call.get("label", ""),
                    "pages": call.get("pages", ""),
                    "page_count": pages,
                    "status": call.get("status", ""),
                    "latency_s": _number(call.get("latency_s")),
                    "input_tokens": int(_number(call.get("input_tokens"))),
                    "output_tokens": int(_number(call.get("output_tokens"))),
                    "cost_input_usd": round(_number(call.get("cost_input_usd")), 6),
                    "cost_output_usd": round(_number(call.get("cost_output_usd")), 6),
                    "cost_total_usd": round(_number(call.get("cost_total_usd")), 6),
                }
                if pages:
                    row["input_tokens_per_page"] = round(row["input_tokens"] / pages)
                    row["output_tokens_per_page"] = round(row["output_tokens"] / pages)
                out.append(row)
    return out


def _page_span(value: object) -> int:
    """How many pages a call covered, from its `start-end` label.

    A text-source call carries `text` rather than a range, and reports 0 —
    a per-page figure would be meaningless for it, not zero.
    """
    text = str(value or "").strip()
    if not text or "-" not in text:
        return 0
    start, _, end = text.partition("-")
    try:
        return max(0, int(end) - int(start) + 1)
    except ValueError:
        return 0


def read_summary(workbook_path: Path) -> dict[str, Any]:
    """The run's telemetry summary block, as a dict."""
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        if TELEMETRY_SHEET not in workbook.sheetnames:
            return {}
        summary: dict[str, Any] = {}
        for raw in workbook[TELEMETRY_SHEET].iter_rows(max_col=2, values_only=True):
            # The summary block runs from row 1 with no gaps, and the per-call
            # table follows it after blank rows. The first blank is the boundary
            # — reading past it picks up the call header as a summary key.
            if not raw or raw[0] is None or str(raw[0]).strip() == "":
                break
            summary[str(raw[0]).strip()] = raw[1]
        return summary
    finally:
        workbook.close()


def write_comparison(groups: list[Group], out_path: Path) -> Path:
    """Write the comparison workbook and return its path."""
    workbook = Workbook()
    workbook.remove(workbook.active)

    totals = [g.totals() for g in groups]
    summary = workbook.create_sheet("Summary")
    header = list(totals[0]) if totals else []
    _write_sheet(summary, header, [[t.get(c, "") for c in header] for t in totals])

    documents = workbook.create_sheet("By Document")
    rows = [d.row() for g in groups for d in g.documents]
    _write_sheet(
        documents,
        list(DOCUMENT_COLUMNS),
        [[r.get(c, "") for c in DOCUMENT_COLUMNS] for r in rows],
    )

    calls = call_rows(groups)
    if calls:
        _write_sheet(
            workbook.create_sheet("Calls"),
            list(CALL_DETAIL_COLUMNS),
            [[c.get(k, "") for k in CALL_DETAIL_COLUMNS] for c in calls],
        )

    if len(groups) == 2:
        deltas = _deltas(groups[0], groups[1])
        sheet = workbook.create_sheet("Deltas")
        _write_summary_block(
            sheet,
            {
                "A": groups[0].label,
                "B": groups[1].label,
                "reading": "positive delta means B is better / costs more",
            },
        )
        _write_sheet(
            sheet,
            list(DELTA_COLUMNS),
            [[d.get(c, "") for c in DELTA_COLUMNS] for d in deltas],
            start_row=5,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(out_path)
    return out_path


def _deltas(a: Group, b: Group) -> list[dict[str, Any]]:
    """Per-document differences, for documents both groups actually scored."""
    left = {d.document: d for d in a.documents}
    right = {d.document: d for d in b.documents}
    out: list[dict[str, Any]] = []
    for document in sorted(set(left) & set(right)):
        x, y = left[document], right[document]
        cost_a = _number(x.telemetry.get("cost_total_usd"))
        cost_b = _number(y.telemetry.get("cost_total_usd"))
        row: dict[str, Any] = {
            "document": document,
            "pages": x.pages,
            "cost_a_usd": round(cost_a, 6),
            "cost_b_usd": round(cost_b, 6),
            "cost_delta_usd": round(cost_b - cost_a, 6),
            "cost_delta_pct": round((cost_b - cost_a) / cost_a * 100, 1) if cost_a else "",
            "calls_a": _number(x.telemetry.get("calls")),
            "calls_b": _number(y.telemetry.get("calls")),
        }
        if x.accuracy is not None and y.accuracy is not None:
            row.update(
                {
                    "rows_golden": x.accuracy.rows_golden,
                    "rows_matched_a": x.accuracy.rows_matched,
                    "rows_matched_b": y.accuracy.rows_matched,
                    "row_recall_delta_pp": round(
                        y.accuracy.row_recall - x.accuracy.row_recall, 1
                    ),
                    "row_accuracy_delta_pp": round(
                        y.accuracy.row_accuracy - x.accuracy.row_accuracy, 1
                    ),
                    "cell_accuracy_a_pct": round(x.accuracy.cell_accuracy, 1),
                    "cell_accuracy_b_pct": round(y.accuracy.cell_accuracy, 1),
                    "cell_accuracy_delta_pp": round(
                        y.accuracy.cell_accuracy - x.accuracy.cell_accuracy, 1
                    ),
                    "cells_correct_delta": y.accuracy.cells_correct - x.accuracy.cells_correct,
                }
            )
        out.append(row)
    return out


def _cost_input(telemetry: dict[str, Any]) -> float:
    """Input cost, falling back to the token split for runs written before the
    ledger carried the breakdown."""
    value = telemetry.get("cost_input_usd")
    if value not in (None, ""):
        return _number(value)
    return _apportioned(telemetry, "input_tokens")


def _cost_output(telemetry: dict[str, Any]) -> float:
    value = telemetry.get("cost_output_usd")
    if value not in (None, ""):
        return _number(value)
    return _apportioned(telemetry, "output_tokens")


def _apportioned(telemetry: dict[str, Any], field_name: str) -> float:
    """Split a total by token share.

    A lower bound on the truth: input and output are priced differently, so this
    understates output cost. Runs written after the split was recorded do not go
    through here — this only keeps an older run from reading as zero.
    """
    total = _number(telemetry.get("cost_total_usd"))
    tokens = _number(telemetry.get("total_tokens"))
    if not total or not tokens:
        return 0.0
    return total * _number(telemetry.get(field_name)) / tokens


def _number(value: object) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
