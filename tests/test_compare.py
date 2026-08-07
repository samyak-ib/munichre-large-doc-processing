"""Comparing finished runs, offline.

The comparison exists to answer "did the change help", so the thing that has to
be right is *which table it scores*: the one that shipped, not the pre-merge rows
a re-score would rebuild. Getting that wrong measures the extraction and quietly
leaves out whatever stage is under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lossrun import compare
from lossrun.report import write_workbook
from lossrun.schema_loader import load_schema
from lossrun.telemetry import CallRecord

SCHEMA = load_schema()

GOLDEN_HEADER = ["Filename", "Claim ID", "Claimant Name", "Loss State", "Policy Number"]


def write_golden(path: Path, rows: list[list[str]]) -> Path:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(GOLDEN_HEADER)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def call(cost_in: float, cost_out: float, stage: str = "extract") -> CallRecord:
    return CallRecord(
        run_id="r", document="d.pdf", stage=stage, model="m", provider="openai",
        label="chunk 1/1", pages="1-2", attempt=1, effort="max", response_id="x",
        status="completed", latency_s=1.0, poll_count=1,
        input_tokens=1000, output_tokens=200, total_tokens=1200,
        cost_input_usd=cost_in, cost_output_usd=cost_out,
        cost_total_usd=round(cost_in + cost_out, 6),
    )


def make_run(
    run_dir: Path,
    *,
    final_state: str,
    raw_state: str,
    calls: list[CallRecord] | None = None,
    pages: int = 2,
) -> Path:
    """A run directory whose Final Table and Raw Rows deliberately disagree."""
    run_dir.mkdir(parents=True, exist_ok=True)
    calls = calls or [call(0.02, 0.08)]
    columns = list(SCHEMA.names)
    final = {c: "N/A" for c in columns}
    final.update(
        {"Claim Number": "C1", "Claimant Name": "A", "Loss State": final_state,
         "Policy Number": "P1"}
    )
    raw = {**final, "Loss State": raw_state, "model": "m", "chunk": "chunk 1/1", "pages": "1-2"}
    write_workbook(
        run_dir / "extraction.xlsx",
        final_rows=[final],
        final_columns=columns,
        raw_rows=[raw],
        raw_columns=["model", "chunk", "pages"] + columns,
        issues=[],
        calls=calls,
        summary={
            "document": "d.pdf",
            "pages": pages,
            "calls": len(calls),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "total_tokens": sum(c.total_tokens for c in calls),
            "cost_total_usd": round(sum(c.cost_total_usd for c in calls), 6),
            "wall_clock_s": 12.5,
            "models": "m",
        },
    )
    return run_dir


def test_the_shipped_table_is_what_gets_scored(tmp_path):
    """Final Table and Raw Rows disagree here, and only one of them shipped."""
    golden = write_golden(tmp_path / "g.xlsx", [["d.pdf", "C1", "A", "TX", "P1"]])
    run = make_run(tmp_path / "run", final_state="TX", raw_state="CA")

    group = compare.collect("qa", [run], SCHEMA, golden)

    assert len(group.documents) == 1
    result = group.documents[0].accuracy
    assert result.cell_accuracy == 100.0, "the corrected final table is the deliverable"
    assert result.rows_matched == 1


def test_costs_come_from_the_per_call_rows_not_a_token_split(tmp_path):
    """Output is priced several times input, so apportioning the total by token
    share understates it badly. The per-call rows carry the real split."""
    golden = write_golden(tmp_path / "g.xlsx", [["d.pdf", "C1", "A", "TX", "P1"]])
    run = make_run(
        tmp_path / "run", final_state="TX", raw_state="TX",
        calls=[call(0.01, 0.09), call(0.02, 0.18)],
    )

    row = compare.collect("qa", [run], SCHEMA, golden).documents[0].row()

    assert row["cost_input_usd"] == pytest.approx(0.03)
    assert row["cost_output_usd"] == pytest.approx(0.27)
    assert row["cost_total_usd"] == pytest.approx(0.30)
    # A token-share split would have put ~5/6 of the cost on input and read as
    # 0.25 in / 0.05 out — the opposite of the truth.


def test_per_page_and_per_row_costs_are_reported(tmp_path):
    golden = write_golden(tmp_path / "g.xlsx", [["d.pdf", "C1", "A", "TX", "P1"]])
    run = make_run(tmp_path / "run", final_state="TX", raw_state="TX", pages=4)

    row = compare.collect("qa", [run], SCHEMA, golden).documents[0].row()

    assert row["cost_per_page_usd"] == pytest.approx(0.025)  # 0.10 over 4 pages
    assert row["cost_per_row_usd"] == pytest.approx(0.10)  # one row extracted
    assert row["input_tokens_per_page"] == 250


def test_a_document_with_no_golden_entry_is_counted_but_not_scored(tmp_path):
    golden = write_golden(tmp_path / "g.xlsx", [["other.pdf", "C9", "Z", "NY", "P9"]])
    run = make_run(tmp_path / "run", final_state="TX", raw_state="TX")

    group = compare.collect("qa", [run], SCHEMA, golden)
    totals = group.totals()

    assert group.documents[0].accuracy is None
    assert totals["documents"] == 1
    assert totals["documents_scored"] == 0
    assert totals["cost_total_usd"] == pytest.approx(0.10), "it still cost money"


def test_totals_pool_cells_across_documents(tmp_path):
    golden = write_golden(
        tmp_path / "g.xlsx",
        [["a.pdf", "C1", "A", "TX", "P1"], ["b.pdf", "C1", "A", "TX", "P1"]],
    )
    root = tmp_path / "runs"
    for name, state in (("a", "TX"), ("b", "CA")):
        run = make_run(root / name, final_state=state, raw_state=state)
        _rename_document(run / "extraction.xlsx", f"{name}.pdf")

    totals = compare.collect("qa", [root], SCHEMA, golden).totals()

    assert totals["documents"] == 2
    assert totals["rows_matched"] == 2
    # One document right, one wrong, on the single column golden constrains.
    assert 0 < totals["cell_accuracy_pct"] < 100


def test_run_dirs_are_found_under_a_folder_or_named_directly(tmp_path):
    root = tmp_path / "runs"
    make_run(root / "one", final_state="TX", raw_state="TX")
    make_run(root / "two", final_state="TX", raw_state="TX")

    assert len(compare.run_dirs_under(root)) == 2
    assert compare.run_dirs_under(root / "one") == [root / "one"]


def test_the_summary_block_stops_before_the_call_table(tmp_path):
    """The two are stacked in one sheet; reading past the boundary would pick up
    the call header as if it were a summary key."""
    run = make_run(tmp_path / "run", final_state="TX", raw_state="TX")
    summary = compare.read_summary(run / "extraction.xlsx")

    assert summary["document"] == "d.pdf"
    assert summary["pages"] == 2
    assert "run_id" not in summary, "that is the call table's first column"


def test_deltas_report_the_difference_between_two_groups(tmp_path):
    golden = write_golden(tmp_path / "g.xlsx", [["d.pdf", "C1", "A", "TX", "P1"]])
    before = make_run(tmp_path / "before", final_state="CA", raw_state="CA")
    after = make_run(
        tmp_path / "after", final_state="TX", raw_state="CA", calls=[call(0.005, 0.02)]
    )

    a = compare.collect("before", [before], SCHEMA, golden)
    b = compare.collect("after", [after], SCHEMA, golden)
    out = compare.write_comparison([a, b], tmp_path / "cmp.xlsx")

    from openpyxl import load_workbook

    book = load_workbook(out)
    assert book.sheetnames == ["Summary", "By Document", "Deltas"]
    sheet = book["Deltas"]
    header = [c.value for c in sheet[5]]
    row = dict(zip(header, next(sheet.iter_rows(min_row=6, values_only=True))))
    assert row["cell_accuracy_delta_pp"] > 0, "the second group fixed the cell"
    assert row["cost_delta_usd"] == pytest.approx(-0.075)


def _rename_document(workbook_path: Path, document: str) -> None:
    """Rewrite the summary block's document name, so one fixture serves many."""
    from openpyxl import load_workbook

    book = load_workbook(workbook_path)
    sheet = book["Telemetry"]
    for row in sheet.iter_rows(max_col=2):
        if str(row[0].value).strip() == "document":
            row[1].value = document
            break
    book.save(workbook_path)
