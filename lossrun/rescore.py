"""Re-score a finished run from the workbook it already wrote.

Scoring lives inside the extraction path, so changing a scoring rule used to
mean paying for every API call again — over an hour and a couple of dollars for
the scanned document. Nothing about scoring needs the network: a run directory
holds each model's per-chunk rows in `Raw Rows` and its layout in
`layout-<model>.json`, which is everything the final table was built from.

This module rebuilds those tables through the same `finalize_rows` +
`clean_table` the pipeline uses, so a re-score under the run's own policy
reproduces its recorded numbers exactly. That equality is what makes a re-score
under a *different* policy trustworthy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from .accuracy import DEFAULT_POLICY, AccuracyResult, ScoringPolicy, load_golden, score
from .cleaning import clean_table
from .extract import RawRow
from .merge import finalize_rows
from .schema_loader import TableSchema

RAW_SHEET = "Raw Rows"
LAYOUT_GLOB = "layout-*.json"


@dataclass
class RunRecord:
    """One document's extraction, reconstructed from disk."""

    run_dir: Path
    document: str
    pages: int
    rows_by_model: dict[str, list[dict[str, str]]]

    def score_all(
        self,
        golden: list[dict[str, str]],
        schema: TableSchema,
        policy: ScoringPolicy = DEFAULT_POLICY,
    ) -> dict[str, AccuracyResult]:
        return {
            model: score(rows, golden, schema, model, policy)
            for model, rows in self.rows_by_model.items()
        }


def load_run(run_dir: Path, schema: TableSchema) -> RunRecord:
    """Rebuild every model's final table from a run directory."""
    workbook_path = run_dir / "extraction.xlsx"
    if not workbook_path.exists():
        raise ValueError(f"{run_dir} has no extraction.xlsx")

    raw_by_model = _read_raw_rows(workbook_path, schema)
    layouts = _read_layouts(run_dir)

    rows_by_model: dict[str, list[dict[str, str]]] = {}
    for model, raw in raw_by_model.items():
        layout = layouts.get(model, {})
        rows = finalize_rows(
            raw,
            schema,
            document_values=layout.get("document_values") or {},
            block_header_policy=layout.get("policy_number_placement") == "block_header",
        )
        rows_by_model[model] = clean_table(rows, schema)

    return RunRecord(
        run_dir=run_dir,
        document=_document_name(run_dir),
        pages=_pages(workbook_path),
        rows_by_model=rows_by_model,
    )


def policy_influence(
    records: list[RunRecord],
    golden_path: Path,
    schema: TableSchema,
    flags,
) -> list[dict[str, object]]:
    """Accuracy with every assumption on, then once per assumption switched off.

    The difference is what that assumption is worth, in percentage points of
    cell accuracy across the whole set.

    Rows matched travels with each result because two of these flags change
    which rows are comparable at all, not just how their cells are judged.
    Dropping hard rows out of the match raises cell accuracy on the easy ones
    that remain, so a delta read without the row count reads backwards.
    """
    goldens = {r.document: load_golden(golden_path, r.document) for r in records}

    def overall(policy: ScoringPolicy) -> tuple[int, int, int, int]:
        compared = correct = matched = golden_rows = 0
        for record in records:
            golden = goldens.get(record.document)
            if not golden:
                continue
            for result in record.score_all(golden, schema, policy).values():
                compared += result.cells_compared
                correct += result.cells_correct
                matched += result.rows_matched
                golden_rows += result.rows_golden
        return compared, correct, matched, golden_rows

    base_compared, base_correct, base_matched, golden_total = overall(DEFAULT_POLICY)
    base_accuracy = _pct(base_correct, base_compared)

    rows = []
    for flag, label in flags:
        compared, correct, matched, _ = overall(DEFAULT_POLICY.without(flag))
        rows.append(
            {
                "assumption": label,
                "flag": flag,
                "cells_scored_with": base_compared,
                "cells_scored_without": compared,
                "rows_matched_with": base_matched,
                "rows_matched_without": matched,
                "rows_golden": golden_total,
                "accuracy_with_pct": round(base_accuracy, 1),
                "accuracy_without_pct": round(_pct(correct, compared), 1),
                "influence_pp": round(base_accuracy - _pct(correct, compared), 1),
                "rows_influence": matched - base_matched,
            }
        )
    return rows


def _pct(part: int, whole: int) -> float:
    return part / whole * 100 if whole else 0.0


def _read_raw_rows(path: Path, schema: TableSchema) -> dict[str, list[RawRow]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if RAW_SHEET not in workbook.sheetnames:
            raise ValueError(f"{path} has no '{RAW_SHEET}' sheet")
        rows = list(workbook[RAW_SHEET].iter_rows(values_only=True))
    finally:
        workbook.close()

    if not rows:
        return {}
    header = [str(c) if c is not None else "" for c in rows[0]]
    index = {name: i for i, name in enumerate(header)}
    names = set(schema.names)

    by_model: dict[str, list[RawRow]] = {}
    for raw in rows[1:]:
        if not raw or not raw[index.get("model", 0)]:
            continue
        values = {
            name: _text(raw[position])
            for name, position in index.items()
            if name in names and position < len(raw)
        }
        by_model.setdefault(str(raw[index["model"]]), []).append(
            RawRow(
                values=values,
                model=str(raw[index["model"]]),
                chunk=_text(raw[index["chunk"]]) if "chunk" in index else "",
                pages=_text(raw[index["pages"]]) if "pages" in index else "",
            )
        )
    return by_model


def _read_layouts(run_dir: Path) -> dict[str, dict]:
    """Each model's layout.json, keyed by the model that produced it."""
    layouts: dict[str, dict] = {}
    for path in sorted(run_dir.glob(LAYOUT_GLOB)):
        # `layout-openai_gpt-5.6-luna.json` -> the model that wrote it. The
        # filename is path-safe, so match it back against the models on hand
        # rather than trying to reverse the escaping.
        stem = path.stem[len("layout-") :]
        try:
            layouts[stem] = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return {_model_for(stem): data for stem, data in layouts.items()}


def _model_for(stem: str) -> str:
    """`openai_gpt-5.6-luna` -> `openai/gpt-5.6-luna`."""
    return stem.replace("_", "/", 1)


def _pages(path: Path) -> int:
    """Page count from the run's own telemetry summary, or 0 when absent."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if "Telemetry" not in workbook.sheetnames:
            return 0
        for row in workbook["Telemetry"].iter_rows(max_col=2, values_only=True):
            if row and str(row[0]).strip() == "pages":
                return int(row[1] or 0)
    except (TypeError, ValueError):
        return 0
    finally:
        workbook.close()
    return 0


def _document_name(run_dir: Path) -> str:
    """The document this run covered, read from its own telemetry summary."""
    workbook = load_workbook(run_dir / "extraction.xlsx", read_only=True, data_only=True)
    try:
        if "Telemetry" in workbook.sheetnames:
            for row in workbook["Telemetry"].iter_rows(max_col=2, values_only=True):
                if row and str(row[0]).strip() == "document":
                    return str(row[1])
    finally:
        workbook.close()
    return run_dir.name


def _text(value: object) -> str:
    return "" if value is None else str(value)
