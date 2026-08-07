"""Wires the stages together: ingest, profile, extract, merge, QA, verify, score."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .accuracy import AccuracyResult, load_golden, score
from .cleaning import clean_table
from .config import Config
from .docprofile import DocProfile, Layout, discover_layout, page_texts, profile_pdf
from .extract import ExtractResult, RawRow, document_chunks, extract_pdf, extract_text
from .ingest import SourceDoc, ingest
from .merge import finalize_rows
from .qa import APPLIED, NO_CHANGE, UNMATCHED, UNVERIFIED, QAResult
from . import qa as qa_stage
from .report import append_ledger, write_workbook
from .schema_loader import TableSchema, load_schema
from .router import ModelRouter
from .telemetry import Telemetry
from .verify import NOT_FOUND, verify_keys

RAW_ROW_META = ("model", "chunk", "pages")

# The extended golden set: sheet 1 is the original five documents, sheet 2 the
# twenty-four added later. Both sheets are read — see `accuracy.load_golden`.
DEFAULT_GOLDEN_PATH = Path("goldens/Loss Runs GTs (1).xlsx")

# Loss-run filenames arrive with spaces, GUIDs and punctuation; keep them
# recognisable but path-safe, and short enough to stay clickable.
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_RUN_DIR_STEM = 80


@dataclass
class ModelRun:
    """The model's pass over the document."""

    model: str
    rows: list[dict[str, str]] = field(default_factory=list)
    raw: list[RawRow] = field(default_factory=list)
    result: ExtractResult | None = None
    profile: DocProfile | None = None
    layout: Layout | None = None
    error: str = ""


@dataclass
class RunOutcome:
    run_dir: Path
    workbook: Path
    ledger: Path
    rows: int
    qa_findings: int
    qa_applied: int
    unverified: int
    cost_usd: float
    route: str
    chunks: int
    accuracy: AccuracyResult | None = None


def run_document(
    input_path: Path,
    *,
    config: Config,
    out_dir: Path,
    schema_path: Path | None = None,
    golden_path: Path | None = None,
    batch_id: str = "",
    log=print,
) -> RunOutcome:
    """Extract one input file end to end and write its workbook."""
    schema = load_schema(schema_path)
    run_id = uuid.uuid4().hex[:12]
    telemetry = Telemetry(
        run_id=run_id, document=input_path.name, pricing=config.pricing, batch_id=batch_id
    )

    model = config.primary_model

    # Each run gets its own directory so re-running a document never overwrites
    # the previous result — comparing two model configurations is the point. Runs
    # are grouped by route because the routes are not measuring the same thing:
    # SuperApp totals carry agent-loop tokens a direct call never pays for, so
    # mixing them in one folder invites a comparison that is not valid.
    run_dir = (
        out_dir
        / f"{config.route_class([model])}_calls"
        / f"{_safe_stem(input_path)}_{time.strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    work_dir = run_dir / "source"
    ingested = ingest(input_path, work_dir)
    if not ingested.docs:
        raise ValueError(f"nothing extractable found in {input_path.name}")
    for note in ingested.notes:
        log(f"  note: {note}")

    source = _primary_source(ingested.docs)
    log(f"  source: {source.name} ({source.kind}, {source.pages or '—'} pages)")

    # Read once: the QA guard, the key check and the accuracy pass all want it,
    # and pulling it out of the PDF three times is pure waste.
    texts = page_texts(source.path) if source.kind == "pdf" else []

    issues: list[dict[str, Any]] = []
    qa_result: QAResult | None = None

    with ModelRouter(config=config, telemetry=telemetry) as client:
        run = _run_model(
            client,
            model=model,
            source=source,
            schema=schema,
            config=config,
            context_text=ingested.context_text,
            debug_dir=run_dir / "raw",
            log=log,
        )
        _collect_extraction_issues(run, issues)

        if config.qa_enabled:
            qa_result = _run_qa(
                client,
                run=run,
                source=source,
                schema=schema,
                config=config,
                texts=texts,
                issues=issues,
                log=log,
            )

    golden = load_golden(golden_path or DEFAULT_GOLDEN_PATH, input_path.name)
    accuracy_rows: list[dict[str, Any]] = []
    column_rows: list[dict[str, Any]] = []
    mismatch_rows: list[dict[str, Any]] = []
    primary_accuracy: AccuracyResult | None = None
    if golden and run.rows:
        # Page count travels with every accuracy row so a score can be read
        # against document size — the scanned 38-page file and the 2-page one
        # are not comparable without it.
        pages = run.profile.pages if run.profile else 0
        context = {
            "batch_id": batch_id,
            "run_id": run_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(telemetry.started_at)),
            "document": input_path.name,
            "pages": pages,
        }
        scored = score(run.rows, golden, schema, run.model)
        primary_accuracy = scored
        accuracy_rows.append(
            scored.summary_row(**context, effort=config.extract_effort_for(run.model) or "default")
        )
        column_rows.extend(
            {
                "batch_id": batch_id,
                "run_id": run_id,
                "document": input_path.name,
                "pages": pages,
                "model": run.model,
                "column": name,
                "compared": c.compared,
                "correct": c.correct,
                "accuracy_pct": round(c.accuracy, 1),
            }
            for name, c in scored.columns.items()
        )
        mismatch_rows.extend(scored.mismatches)
        log(
            f"  [{run.model}] accuracy vs golden: "
            f"{scored.rows_matched}/{scored.rows_golden} rows matched, "
            f"{scored.cell_accuracy:.1f}% cells correct, "
            f"{scored.exact_row_rate:.1f}% rows fully correct"
        )
    elif not golden:
        log("  accuracy: no golden entry for this document")

    unverified = 0
    if source.kind == "pdf":
        key_issues, checked = verify_keys(run.rows, texts)
        unverified = sum(1 for i in key_issues if i.verdict == NOT_FOUND)
        if checked:
            log(
                f"  key check: {checked} key values checked against the text layer, "
                f"{unverified} not found verbatim"
            )
            _collect_key_issues(key_issues, run.rows, issues)
        else:
            log("  key check: skipped, no text layer (scanned document)")
            issues.append(
                {
                    "severity": "warning",
                    "category": "verification",
                    "detail": "no text layer; keys could not be verified verbatim",
                    "source": source.name,
                }
            )

    workbook_path = run_dir / "extraction.xlsx"
    # One ledger per route, beside that route's runs. It accumulates across runs
    # so model and configuration experiments are comparable, while keeping runs
    # measured a different way out of the file entirely: a SuperApp total carries
    # agent-loop tokens a direct call never pays for, so the two do not belong in
    # one population. Comparing the routes means comparing two ledgers.
    ledger_path = run_dir.parent / "telemetry.xlsx"

    models = ", ".join(dict.fromkeys([model, config.qa_model] if config.qa_enabled else [model]))
    summary = telemetry.summary(
        models=models,
        providers=", ".join(
            f"{m.rsplit('/', 1)[-1]}->{config.provider_for(m)}" for m in config.routed_models
        ),
        route=run.profile.route if run.profile else "text",
        effort=f"{model.rsplit('/', 1)[-1]}={config.extract_effort_for(model) or 'default'}",
        pages=run.profile.pages if run.profile else 0,
        chunks=run.result.chunks if run.result else 0,
        rows=len(run.rows),
        qa_findings=len(qa_result.findings) if qa_result else 0,
        qa_applied=qa_result.applied if qa_result else 0,
        unverified_keys=unverified,
        status="ok" if run.rows else "no_rows",
    )

    # Keep the discovered layout beside the workbook: when a column comes back
    # wrong, the layout is usually where it went wrong.
    if run.layout and run.layout.data:
        layout_file = run_dir / f"layout-{run.model.replace('/', '_')}.json"
        layout_file.write_text(json.dumps(run.layout.data, indent=2))

    write_workbook(
        workbook_path,
        final_rows=run.rows,
        final_columns=schema.names,
        raw_rows=[{**r.values, **{k: getattr(r, k) for k in RAW_ROW_META}} for r in run.raw],
        raw_columns=list(RAW_ROW_META) + list(schema.names),
        issues=issues,
        calls=telemetry.calls,
        summary=summary,
        accuracy=accuracy_rows,
        column_scores=column_rows,
        mismatches=mismatch_rows,
    )
    append_ledger(ledger_path, summary, telemetry.calls, accuracy_rows, column_rows)

    if telemetry.calls_without_usage:
        log(
            f"  cost warning: {telemetry.calls_without_usage} of {len(telemetry.calls)} "
            f"calls reported no token counts; ${telemetry.total_cost_usd:.4f} is a lower bound"
        )

    return RunOutcome(
        run_dir=run_dir,
        workbook=workbook_path,
        ledger=ledger_path,
        rows=len(run.rows),
        qa_findings=len(qa_result.findings) if qa_result else 0,
        qa_applied=qa_result.applied if qa_result else 0,
        unverified=unverified,
        cost_usd=telemetry.total_cost_usd,
        route=run.profile.route if run.profile else "text",
        chunks=run.result.chunks if run.result else 0,
        accuracy=primary_accuracy,
    )


def _run_qa(
    client: ModelRouter,
    *,
    run: ModelRun,
    source: SourceDoc,
    schema: TableSchema,
    config: Config,
    texts: list[str],
    issues: list[dict[str, Any]],
    log,
) -> QAResult | None:
    """Review the extracted table against the pages it was read from.

    Skipped for a source with no PDF to re-attach: the review's whole value is
    that it looks at the page again, and a text source has already been sent to
    the model in full.
    """
    if not run.rows:
        return None
    if source.kind != "pdf" or run.profile is None:
        log("  qa: skipped, no PDF pages to review against")
        return None

    chunks = document_chunks(source.path, run.profile, config.chunking_for(run.model))
    result = qa_stage.review(
        run.rows,
        qa_stage.row_chunk_pages(run.raw),
        client=client,
        model=config.qa_model,
        schema=schema,
        chunks=chunks,
        effort=config.extract_effort_for(config.qa_model),
        max_workers=config.api.max_concurrent_calls,
        log=log,
    )
    result.verifiable = any(t.strip() for t in texts)
    applied = qa_stage.apply(run.rows, result.findings, texts, schema)

    log(
        f"  qa: {result.calls} review call(s), {len(result.findings)} corrections "
        f"proposed, {applied} applied, "
        f"{sum(1 for f in result.findings if f.verdict == UNVERIFIED)} unverified, "
        f"{len(result.missing)} row(s) reported missing"
    )
    if result.findings and not result.verifiable:
        log(
            "  qa: no text layer, so no correction could be confirmed against the "
            "document — every one is reported rather than applied"
        )
    _collect_qa_issues(result, issues)
    return result


def _run_model(
    client: ModelRouter,
    *,
    model: str,
    source: SourceDoc,
    schema: TableSchema,
    config: Config,
    context_text: str,
    debug_dir: Path,
    log,
) -> ModelRun:
    run = ModelRun(model=model)
    chunk_cfg = config.chunking_for(model)

    if source.kind == "text":
        run.layout = Layout(data={})
        run.result = extract_text(
            client,
            model=model,
            text=source.text,
            label=source.name,
            schema=schema,
            layout=run.layout,
            effort=config.extract_effort_for(model),
        )
    else:
        run.profile = profile_pdf(source.path, chunk_cfg)
        log(
            f"  [{model}] profile: {run.profile.pages} pages, "
            f"{run.profile.text_coverage:.0%} with a text layer -> {run.profile.route}"
        )
        run.layout = discover_layout(
            client,
            model=model,
            path=source.path,
            schema=schema,
            profile=run.profile,
            cfg=chunk_cfg,
            context_text=context_text,
            effort=config.reasoning.layout,
        )
        run.result = extract_pdf(
            client,
            model=model,
            path=source.path,
            schema=schema,
            profile=run.profile,
            layout=run.layout,
            cfg=chunk_cfg,
            debug_dir=debug_dir,
            effort=config.extract_effort_for(model),
        )

    run.raw = run.result.rows
    rows = finalize_rows(
        run.raw,
        schema,
        document_values=run.layout.document_values if run.layout else {},
        block_header_policy=bool(
            run.layout and run.layout.policy_number_placement == "block_header"
        ),
        log=lambda message: log(f"  [{model}]{message}"),
    )
    # Render into golden's representation before reviewing, scoring or writing.
    run.rows = clean_table(rows, schema)
    log(f"  [{model}] {len(run.raw)} raw rows -> {len(rows)} merged rows")
    return run


def _safe_stem(path: Path) -> str:
    """A path-safe, readable form of the input filename for the run directory."""
    stem = _UNSAFE_NAME_RE.sub("_", path.stem).strip("_")
    return (stem[:MAX_RUN_DIR_STEM].rstrip("_") or "document")


def _primary_source(docs: list[SourceDoc]) -> SourceDoc:
    """Prefer the largest PDF; fall back to the first text artifact."""
    pdfs = [d for d in docs if d.kind == "pdf"]
    if pdfs:
        return max(pdfs, key=lambda d: d.pages)
    return docs[0]


def _collect_extraction_issues(run: ModelRun, issues: list[dict[str, Any]]) -> None:
    if run.error:
        issues.append(
            {
                "severity": "error",
                "category": "model_run",
                "detail": run.error,
                "source": run.model,
            }
        )
    for event in run.result.events if run.result else []:
        issues.append(
            {
                "severity": event.level,
                "category": event.stage,
                "detail": event.detail,
                "source": f"{run.model} {event.chunk}".strip(),
                "value": event.pages,
            }
        )


def _collect_qa_issues(result: QAResult, issues: list[dict[str, Any]]) -> None:
    """Record every proposed correction and what became of it.

    A correction the document confirmed drops to info as an audit trail. One it
    would not confirm stays a warning: that is a cell where the reviewer and the
    extractor disagree and neither can be checked, which is exactly the cell a
    human should read.
    """
    for finding in result.findings:
        if finding.verdict == NO_CHANGE:
            continue
        if finding.verdict == APPLIED:
            severity, category, detail = (
                "info",
                "qa_corrected",
                f"replaced {finding.current_value!r} with {finding.proposed_value!r}: "
                f"{finding.reason}",
            )
        elif finding.verdict == UNMATCHED:
            severity, category, detail = (
                "warning",
                "qa_unmatched_row",
                f"correction names a row the table does not hold: {finding.reason}",
            )
        else:
            severity, category, detail = (
                "warning",
                "qa_unverified",
                f"proposed {finding.proposed_value!r} in place of "
                f"{finding.current_value!r}, but that value does not appear in the "
                f"document text — kept the extracted value: {finding.reason}",
            )
        issues.append(
            {
                "severity": severity,
                "category": category,
                "detail": detail,
                "row_key": " | ".join(finding.key),
                "column": finding.column,
                "value": finding.proposed_value,
                "source": f"qa {finding.pages}".strip(),
            }
        )

    for row in result.missing:
        issues.append(
            {
                "severity": "warning",
                "category": "qa_row_missing",
                "detail": (
                    "the review found this claim on the page but not in the table; "
                    f"it is NOT in the final table: {row.reason}"
                ),
                "row_key": " | ".join(row.key),
                "source": f"qa {row.pages}".strip(),
            }
        )

    for error in result.errors:
        issues.append(
            {
                "severity": "warning",
                "category": "qa_call_failed",
                "detail": f"a review call did not return a usable answer: {error}",
                "source": "qa",
            }
        )

    if result.findings and not result.verifiable:
        issues.append(
            {
                "severity": "warning",
                "category": "qa_unverifiable",
                "detail": (
                    "this document has no text layer, so no proposed correction "
                    "could be confirmed against it; every one is reported rather "
                    "than applied"
                ),
                "source": "qa",
            }
        )


def _collect_key_issues(key_issues, rows, issues: list[dict[str, Any]]) -> None:
    from .merge import normalize_key

    for issue in key_issues:
        row = rows[issue.row_index] if issue.row_index < len(rows) else {}
        issues.append(
            {
                "severity": "error" if issue.verdict == NOT_FOUND else "warning",
                "category": f"key_{issue.verdict}",
                "detail": (
                    "value does not appear verbatim in the document text — "
                    "possible hallucination"
                    if issue.verdict == NOT_FOUND
                    else "value matches the document only when case is ignored"
                ),
                "row_key": " | ".join(normalize_key(row)) if row else "",
                "column": issue.column,
                "value": issue.value,
            }
        )
