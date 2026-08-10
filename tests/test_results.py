"""The shareable results workbook's per-document summary and headline totals."""

from __future__ import annotations

from pathlib import Path

from lossrun.rescore import RunRecord
from lossrun.results import BatchResults
from lossrun.schema_loader import load_schema
from lossrun.telemetry import CALL_COLUMNS

SCHEMA = load_schema()

GOLDEN_ROW = {
    "Claim Number": "C003",
    "Claimant Name": "McAllister, John",
    "Policy Number": "P-1",
    "Loss State": "TX",
}


def make_record(document: str, model: str = "m") -> RunRecord:
    return RunRecord(
        run_dir=Path("unused"),
        document=document,
        pages=10,
        rows_by_model={model: [dict(GOLDEN_ROW)]},
    )


def make_golden(tmp_path, document: str) -> Path:
    from openpyxl import Workbook

    path = tmp_path / "golden.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["Filename", "Claim ID", "Claimant Name", "Policy Number", "Loss State"])
    sheet.append([document, "C003", "McAllister, John", "P-1", "TX"])
    book.save(path)
    return path


RUN_ROW = {
    "batch_id": "b1",
    "document": "doc.pdf",
    "run_id": "r1",
    "pages": 10,
    "input_tokens": 1000,
    "output_tokens": 200,
    "input_tokens_per_page": 100,
    "output_tokens_per_page": 20,
    "cost_input_usd": 0.0002,
    "cost_output_usd": 0.00016,
    "cost_total_usd": 0.00036,
}


def test_document_rows_carries_row_level_accuracy_and_telemetry(tmp_path):
    golden_path = make_golden(tmp_path, "doc.pdf")
    results = BatchResults(
        batch_id="b1",
        records=[make_record("doc.pdf")],
        schema=SCHEMA,
        golden_path=golden_path,
        run_rows=[RUN_ROW],
    )
    rows = results.document_rows()
    assert len(rows) == 1
    row = rows[0]

    assert row["rows_golden"] == 1
    assert row["rows_extracted"] == 1
    assert row["rows_matched"] == 1
    assert row["row_accuracy_matched_pct"] == 100.0
    assert row["row_accuracy_overall_pct"] == 100.0
    assert row["cell_accuracy_pct"] == 100.0
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["input_tokens_per_page"] == 100
    assert row["output_tokens_per_page"] == 20
    assert row["cost_input_usd"] == 0.0002
    assert row["cost_output_usd"] == 0.00016
    assert row["cost_total_usd"] == 0.00036


def test_a_document_with_no_run_row_gets_blank_telemetry_not_a_crash(tmp_path):
    golden_path = make_golden(tmp_path, "doc.pdf")
    results = BatchResults(
        batch_id="b1",
        records=[make_record("doc.pdf")],
        schema=SCHEMA,
        golden_path=golden_path,
        run_rows=[],
    )
    row = results.document_rows()[0]
    assert row["input_tokens"] == ""
    assert row["cost_total_usd"] == ""


def call_row(document: str, stage: str, latency_s: float) -> list:
    values = {c: "" for c in CALL_COLUMNS}
    values.update(document=document, stage=stage, latency_s=latency_s)
    return [values[c] for c in CALL_COLUMNS]


def test_document_rows_sums_wall_clock_time_per_stage(tmp_path):
    """A chunked extraction and a QA review split into parts are each more
    than one call — the reported time is the stage's total, not one call's."""
    golden_path = make_golden(tmp_path, "doc.pdf")
    calls = [
        call_row("doc.pdf", "layout", 3.0),
        call_row("doc.pdf", "extract", 12.5),
        call_row("doc.pdf", "extract", 8.5),  # a second chunk
        call_row("doc.pdf", "qa", 4.0),
        call_row("doc.pdf", "qa", 2.0),  # a QA batch split for budget
    ]
    results = BatchResults(
        batch_id="b1",
        records=[make_record("doc.pdf")],
        schema=SCHEMA,
        golden_path=golden_path,
        run_rows=[RUN_ROW],
        call_rows=calls,
    )
    row = results.document_rows()[0]
    assert row["extract_time_s"] == 21.0
    assert row["qa_time_s"] == 6.0


def test_headline_totals_cost_over_every_run_not_only_scored_documents(tmp_path):
    """A document with no golden entry (e.g. Loss-3.pdf) still cost real money
    and must count toward the batch total, even though it contributes no rows
    to document_rows()."""
    golden_path = make_golden(tmp_path, "doc.pdf")
    unscored_run = {**RUN_ROW, "document": "no-golden.pdf", "cost_total_usd": 0.005,
                    "cost_input_usd": 0.003, "cost_output_usd": 0.002}
    results = BatchResults(
        batch_id="b1",
        records=[make_record("doc.pdf")],
        schema=SCHEMA,
        golden_path=golden_path,
        run_rows=[RUN_ROW, unscored_run],
    )
    headline = results.headline()
    assert headline["documents_run"] == 2
    assert headline["documents"] == 1, "only the scored document appears in document_rows"
    assert headline["cost_total_usd_total"] == round(0.00036 + 0.005, 4)
    assert headline["cost_input_usd_total"] == round(0.0002 + 0.003, 4)
    assert headline["cost_output_usd_total"] == round(0.00016 + 0.002, 4)
